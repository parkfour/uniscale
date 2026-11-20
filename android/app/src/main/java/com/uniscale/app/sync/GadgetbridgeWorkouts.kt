package com.uniscale.app.sync

import com.uniscale.app.model.HrZone
import com.uniscale.app.model.WorkoutData
import com.uniscale.app.model.WorkoutSplit
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import kotlin.math.roundToInt

/**
 * Turns a row of Gadgetbridge's BASE_ACTIVITY_SUMMARY into a WorkoutData. SUMMARY_DATA is JSON like
 * {"distanceMeters": {"unit": "meters", "value": 6469.19}, ...}, decoded by Gadgetbridge from the watch's FIT file.
 */
object GadgetbridgeWorkouts {
    // Gadgetbridge ActivityKind codes; 16 (running) seen in a real export, the others from its source.
    private val SPORTS = mapOf(16 to "running", 32 to "walking", 64 to "swimming", 128 to "cycling")

    // SUMMARY_DATA key -> (server field, factor applied to the value)
    private val EXTRAS = mapOf(
        "anaerobicTrainingEffect" to ("anaerobic_training_effect" to 1.0),
        "training_load" to ("training_load" to 1.0),
        "recoveryTime" to ("recovery_time_hours" to 1 / 3600.0),
        "body_energy_at_start" to ("body_battery_start" to 1.0),
        "body_energy_at_end" to ("body_battery_end" to 1.0),
        "estimatedSweatLoss" to ("sweat_loss_ml" to 1.0),
        "avg_temperature" to ("avg_temperature_c" to 1.0),
        "ground_contact_time" to ("ground_contact_ms" to 1.0),
        "vertical_oscillation" to ("vertical_oscillation_mm" to 1.0),
        "vertical_ratio" to ("vertical_ratio_percent" to 1.0),
        "stepLengthAvg" to ("stride_length_m" to 1 / 1000.0),
        "ground_contact_balance" to ("ground_contact_balance" to 1.0),
        "ground_contact_time_balance" to ("ground_contact_balance" to 1.0),
        "stance_time_balance" to ("ground_contact_balance" to 1.0),
        "stanceTimeBalance" to ("ground_contact_balance" to 1.0),
        "groundContactTimeBalance" to ("ground_contact_balance" to 1.0),
        "activityTotalDescent" to ("elevation_loss_m" to 1.0),
        "normalized_power" to ("normalized_power" to 1.0),
        "active_calories" to ("active_calories" to 1.0),
    )

    // Garmin's five zones in SUMMARY_DATA: seconds per zone, and hrZoneBoundN = the bpm where zone N starts.
    private val ZONES = listOf("hrZoneWarmUp" to "Warm up", "hrZoneEasy" to "Easy", "hrZoneAerobic" to "Aerobic",
        "hrZoneThreshold" to "Threshold", "hrZoneMaximum" to "Maximum")

    fun fromSummary(startMs: Long, endMs: Long, kind: Int, name: String?, summaryJson: String?): WorkoutData {
        val s = summaryJson?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()
        fun num(key: String): Double? = s.optJSONObject(key)?.opt("value")?.let { (it as? Number)?.toDouble() }

        val sport = SPORTS[kind] ?: name?.trim()?.lowercase()?.ifEmpty { null } ?: "generic"
        // Gadgetbridge gives steps/min; the server stores the FIT convention (strides/min) for running.
        fun cadence(v: Double?) = v?.let { if (sport == "running") it / 2 else it }
        val extras = EXTRAS.mapNotNull { (key, target) ->
            num(key)?.let { target.first to Math.round(it * target.second * 100) / 100.0 }
        }.toMap() + listOfNotNull(cadence(num("maxCadence"))?.let { "max_cadence" to it.roundToInt().toDouble() })
        return WorkoutData(
            sport = sport,
            startTime = Instant.ofEpochMilli(startMs),
            durationSeconds = num("activeSeconds") ?: ((endMs - startMs) / 1000.0),
            distanceMeters = num("distanceMeters") ?: 0.0,
            calories = num("caloriesBurnt")?.roundToInt(),
            avgHeartRate = num("averageHR")?.roundToInt(),
            maxHeartRate = num("maxHR")?.roundToInt(),
            avgCadence = cadence(num("averageCadence"))?.roundToInt(),
            elevationGainM = num("activityTotalAscent"),
            steps = num("steps")?.roundToInt(),
            avgPower = num("average_power")?.roundToInt(),
            maxPower = num("max_power")?.roundToInt(),
            aerobicTrainingEffect = num("aerobicTrainingEffect"),
            vo2max = num("maximumOxygenUptake")?.let { (it * 10).roundToInt() / 10.0 },
            deviceName = GadgetbridgeNights.DEVICE_NAME,
            extras = extras,
            hrZones = ZONES.mapIndexedNotNull { i, (key, label) ->
                num(key)?.let { HrZone(i + 1, label, num("hrZoneBound${i + 1}")?.roundToInt(), it) }
            },
            splits = splits(s),
        )
    }

    /** How long after a workout its heart rate is read, for the recovery. */
    const val RECOVERY_WINDOW_S = 150L

    /** How long a workout waits for the heart rate after it before it's uploaded without the recovery. */
    const val RECOVERY_WAIT_S = 3 * 3600L

    /**
     * Heart rate recovery: how far the heart rate fell 1 and 2 minutes after the workout ended
     * ([end] in seconds from the start, like the samples). The watch samples about once a minute, so
     * each reading is the sample nearest the mark, within 30 s; a mark without one is left out.
     */
    fun hrRecovery(samples: List<Pair<Int, Int>>, end: Int): Map<String, Int> {
        val atEnd = samples.lastOrNull { (t, _) -> t in (end - 90)..end }?.second ?: return emptyMap()
        fun near(mark: Int) = samples.filter { (t, _) -> kotlin.math.abs(t - mark) <= 30 }
            .minByOrNull { (t, _) -> kotlin.math.abs(t - mark) }?.second
        return listOfNotNull(
            near(end + 60)?.let { "hr_recovery_1min" to atEnd - it },
            near(end + 120)?.let { "hr_recovery_2min" to atEnd - it },
        ).toMap()
    }

    /** interval_1..N rows; the columns are named by intervals_header (e.g. #, Distance, Pace, heart_rate, time). */
    private fun splits(s: JSONObject): List<WorkoutSplit> {
        val header = s.optJSONObject("intervals_header")?.optJSONArray("columns") ?: return emptyList()
        val names = (0 until header.length()).map { header.optJSONObject(it)?.optString("value").orEmpty() }
        val distance = names.indexOf("Distance")
        val time = names.indexOf("pref_header_time")
        val hr = names.indexOf("heart_rate")
        if (distance < 0 || time < 0) return emptyList()
        fun JSONArray.number(i: Int) = if (i < 0) null else optJSONObject(i)?.opt("value") as? Number
        val out = mutableListOf<WorkoutSplit>()
        var n = 1
        while (true) {
            val cols = s.optJSONObject("interval_$n")?.optJSONArray("columns") ?: break
            val d = cols.number(distance)?.toDouble()
            val t = cols.number(time)?.toDouble()
            if (d != null && t != null) out += WorkoutSplit(d, t, cols.number(hr)?.toInt())
            n++
        }
        return out
    }
}
