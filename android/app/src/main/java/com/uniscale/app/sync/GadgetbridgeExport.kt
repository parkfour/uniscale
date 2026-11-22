package com.uniscale.app.sync

import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteException
import android.net.Uri
import android.util.Log
import com.uniscale.app.model.SleepData
import com.uniscale.app.model.WatchActivityData
import com.uniscale.app.model.WorkoutData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import kotlin.math.roundToInt
import java.time.LocalDate
import java.time.ZoneId

/**
 * Reads the database file Gadgetbridge writes with Settings → Auto export. The user picks that file
 * once (system file picker); we keep read access to it across reboots. It's the app's only source of
 * watch data: sleep, workouts, steps and distance.
 */
/** What one read of the export yields, newest first. */
data class GadgetbridgeData(
    val nights: List<SleepData>,
    val workouts: List<WorkoutData>,
    /** Steps, distance and heart rate for today and yesterday (when the watch recorded any), today first. */
    val activity: List<WatchActivityData> = emptyList(),
    /** The last day's heart rate, about one sample a minute: (epoch seconds, bpm), oldest first. */
    val heartRate: List<Pair<Long, Int>> = emptyList()
)

class GadgetbridgeExport(private val context: Context) {

    private val prefs = context.getSharedPreferences("gadgetbridge_export", Context.MODE_PRIVATE)

    val exportUri: Uri?
        get() = prefs.getString(KEY_URI, null)?.let(Uri::parse)

    fun setExportFile(uri: Uri) {
        context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        prefs.edit().putString(KEY_URI, uri.toString()).apply()
    }

    /** Nights and workouts from the last [days] days; null when no export file is set or it can't be read. */
    suspend fun read(days: Int = 30): GadgetbridgeData? = withContext(Dispatchers.IO) {
        val uri = exportUri ?: return@withContext null
        // SQLite needs a real file; the export is small (~1 MB), so copy it to the cache.
        // Unique name: the background sync and the app can read at the same time.
        val copy = File.createTempFile("gadgetbridge_export", ".db", context.cacheDir)
        try {
            val input = context.contentResolver.openInputStream(uri) ?: return@withContext null
            input.use { inp -> copy.outputStream().use { inp.copyTo(it) } }
            SQLiteDatabase.openDatabase(copy.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
                // Start a day early so a night that began before the window is still complete.
                val since = System.currentTimeMillis() - (days + 1) * 86_400_000L
                val nights = GadgetbridgeNights.build(
                    events = rows(db, "SELECT TIMESTAMP, EVENT_TYPE FROM GARMIN_EVENT_SAMPLE WHERE EVENT = 74 AND TIMESTAMP >= ?", since) {
                        GbSleepEvent(it.getLong(0), it.getInt(1))
                    },
                    stages = rows(db, "SELECT TIMESTAMP, STAGE FROM GARMIN_SLEEP_STAGE_SAMPLE WHERE TIMESTAMP >= ?", since) {
                        it.getLong(0) to it.getInt(1)
                    },
                    scores = rows(db, "SELECT TIMESTAMP, SLEEP_SCORE FROM GARMIN_SLEEP_STATS_SAMPLE WHERE TIMESTAMP >= ?", since) {
                        it.getLong(0) to it.getInt(1)
                    },
                    hrv = rows(db, "SELECT TIMESTAMP, LAST_NIGHT_AVERAGE, STATUS_NUM FROM GARMIN_HRV_SUMMARY_SAMPLE WHERE TIMESTAMP >= ?", since) {
                        GbHrvSummary(it.getLong(0), it.intOrNull(1), it.intOrNull(2))
                    },
                    spo2 = rows(db, "SELECT TIMESTAMP, SPO2 FROM GARMIN_SPO2_SAMPLE WHERE TIMESTAMP >= ?", since) {
                        it.getLong(0) to it.getInt(1)
                    },
                    respiration = rows(db, "SELECT TIMESTAMP, RESPIRATORY_RATE FROM GARMIN_RESPIRATORY_RATE_SAMPLE WHERE TIMESTAMP >= ?", since) {
                        it.getLong(0) to it.getFloat(1)
                    },
                    zone = ZoneId.systemDefault()
                )
                val workouts = rows(
                    db,
                    "SELECT START_TIME, END_TIME, ACTIVITY_KIND, NAME, SUMMARY_DATA FROM BASE_ACTIVITY_SUMMARY " +
                        "WHERE START_TIME >= ? ORDER BY START_TIME DESC",
                    since
                ) {
                    GadgetbridgeWorkouts.fromSummary(
                        it.getLong(0), it.getLong(1), it.getInt(2), it.getString(3), it.getString(4)
                    ) to it.getLong(1) / 1000
                }.map { (w, endS) ->
                    // Heart rate during the workout, and for a while after it to see how fast it falls,
                    // from the watch's activity samples (TIMESTAMP is in seconds).
                    val startS = w.startTime.epochSecond
                    val samples = rows(
                        db,
                        "SELECT TIMESTAMP, HEART_RATE FROM GARMIN_ACTIVITY_SAMPLE " +
                            "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE BETWEEN 30 AND 250 ORDER BY TIMESTAMP",
                        startS, endS + GadgetbridgeWorkouts.RECOVERY_WINDOW_S
                    ) { c -> (c.getLong(0) - startS).toInt() to c.getInt(1) }
                    val end = (endS - startS).toInt()
                    val recovery = GadgetbridgeWorkouts.hrRecovery(samples, end)
                    w.copy(
                        hrSamples = samples,
                        extras = w.extras + recovery.mapValues { it.value.toDouble() },
                        recoveryPending = "hr_recovery_2min" !in recovery &&
                            System.currentTimeMillis() / 1000 - endS < GadgetbridgeWorkouts.RECOVERY_WAIT_S
                    )
                }
                val zone = ZoneId.systemDefault()
                val today = LocalDate.now(zone)
                val activity = listOf(today, today.minusDays(1)).mapNotNull { day -> dayActivity(db, day, zone) }
                val heartRate = rows(
                    db,
                    "SELECT TIMESTAMP, HEART_RATE FROM GARMIN_ACTIVITY_SAMPLE " +
                        "WHERE TIMESTAMP >= ? AND HEART_RATE BETWEEN 30 AND 250 ORDER BY TIMESTAMP",
                    System.currentTimeMillis() / 1000 - HEART_RATE_WINDOW_S
                ) { it.getLong(0) to it.getInt(1) }
                GadgetbridgeData(nights, workouts, activity, heartRate)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Could not read Gadgetbridge export: ${e.message}")
            null
        } finally {
            copy.delete()
        }
    }

    /**
     * Garmin's STEPS and DISTANCE_CM are running totals for the day (-1 = no reading), and the
     * midnight sample still holds the day before's total, so a day is the largest value after its
     * midnight up to and including the next one.
     */
    private fun dayActivity(db: SQLiteDatabase, day: LocalDate, zone: ZoneId): WatchActivityData? {
        val start = day.atStartOfDay(zone).toEpochSecond()
        val end = day.plusDays(1).atStartOfDay(zone).toEpochSecond()
        val (steps, cm) = rows(
            db,
            "SELECT MAX(STEPS), MAX(DISTANCE_CM) FROM GARMIN_ACTIVITY_SAMPLE WHERE TIMESTAMP > ? AND TIMESTAMP <= ?",
            start, end
        ) { (it.longOrNull(0) ?: 0L) to (it.longOrNull(1) ?: 0L) }.firstOrNull() ?: (0L to 0L)
        val km = if (cm > 0) cm / 100_000.0 else steps * 0.85582 / 1000.0  // Garmin's stride when no distance
        val hr = rows(
            db,
            "SELECT AVG(HEART_RATE), MIN(HEART_RATE), MAX(HEART_RATE) FROM GARMIN_ACTIVITY_SAMPLE " +
                "WHERE TIMESTAMP > ? AND TIMESTAMP <= ? AND HEART_RATE BETWEEN 30 AND 250",
            start, end
        ) { Triple(it.intOrNull(0), it.intOrNull(1), it.intOrNull(2)) }.firstOrNull()
        if (steps <= 0 && hr?.first == null) return null  // nothing from the watch that day (yet)
        // These two tables keep their TIMESTAMP in milliseconds; the watch writes the day's resting rate several times.
        val resting = rows(
            db,
            "SELECT HEART_RATE FROM GARMIN_HEART_RATE_RESTING_SAMPLE WHERE TIMESTAMP >= ? AND TIMESTAMP < ? " +
                "AND HEART_RATE BETWEEN 25 AND 200 ORDER BY TIMESTAMP DESC LIMIT 1",
            start * 1000, end * 1000
        ) { it.getInt(0) }.firstOrNull()
        val activeMinutes = rows(
            db,
            "SELECT SUM(MODERATE) + SUM(VIGOROUS) FROM GARMIN_INTENSITY_MINUTES_SAMPLE WHERE TIMESTAMP >= ? AND TIMESTAMP < ?",
            start * 1000, end * 1000
        ) { it.intOrNull(0) }.firstOrNull()
        val stress = rows(
            db,
            "SELECT AVG(STRESS), MAX(STRESS) FROM GARMIN_STRESS_SAMPLE " +
                "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? AND STRESS BETWEEN 0 AND 100",  // -1/-2: couldn't measure
            start * 1000, end * 1000
        ) { (it.doubleOrNull(0)?.roundToInt()) to it.intOrNull(1) }.firstOrNull()
        val battery = rows(
            db,
            "SELECT MAX(ENERGY), MIN(ENERGY), (SELECT ENERGY FROM GARMIN_BODY_ENERGY_SAMPLE WHERE TIMESTAMP >= ?1 " +
                "AND TIMESTAMP < ?2 AND ENERGY BETWEEN 0 AND 100 ORDER BY TIMESTAMP DESC LIMIT 1) " +
                "FROM GARMIN_BODY_ENERGY_SAMPLE WHERE TIMESTAMP >= ?1 AND TIMESTAMP < ?2 AND ENERGY BETWEEN 0 AND 100",
            start * 1000, end * 1000
        ) { Triple(it.intOrNull(0), it.intOrNull(1), it.intOrNull(2)) }.firstOrNull()
        return WatchActivityData(
            steps = steps,
            distanceKm = (km * 100).roundToInt() / 100f,
            deviceName = "Garmin",
            date = day,
            restingHeartRate = resting,
            avgHeartRate = hr?.first,
            minHeartRate = hr?.second,
            maxHeartRate = hr?.third,
            activeMinutes = activeMinutes,
            avgStress = stress?.first,
            maxStress = stress?.second,
            bodyBattery = battery?.third,
            bodyBatteryHigh = battery?.first,
            bodyBatteryLow = battery?.second
        )
    }

    // A table that doesn't exist yet (e.g. no Garmin data so far) is just empty.
    private fun <T> rows(db: SQLiteDatabase, sql: String, vararg args: Long, map: (Cursor) -> T): List<T> = try {
        db.rawQuery(sql, args.map { it.toString() }.toTypedArray()).use { c ->
            buildList { while (c.moveToNext()) add(map(c)) }
        }
    } catch (e: SQLiteException) {
        Log.w(TAG, "Skipping query (${e.message})")
        emptyList()
    }

    private fun Cursor.intOrNull(i: Int): Int? = if (isNull(i)) null else getInt(i)
    private fun Cursor.longOrNull(i: Int): Long? = if (isNull(i)) null else getLong(i)
    private fun Cursor.doubleOrNull(i: Int): Double? = if (isNull(i)) null else getDouble(i)

    companion object {
        private const val KEY_URI = "export_uri"
        /** How far back the heart-rate samples sent to the hub go. */
        private const val HEART_RATE_WINDOW_S = 24 * 3600L
        private const val TAG = "GadgetbridgeExport"
    }
}
