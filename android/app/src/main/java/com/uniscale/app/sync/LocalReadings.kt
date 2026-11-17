package com.uniscale.app.sync

import android.content.Context
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.WeightData
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/**
 * The scale and blood-pressure readings this phone took, kept in the app's private storage so they
 * show at once and are uploaded again until the hub has them (HubSyncManager skips ones it already
 * sent). Only the newest [KEEP] of each, from the last [MAX_AGE_DAYS] days, are kept; the hub holds
 * the full history. The age limit stays under HubSyncManager's 120-day memory of what it sent, so an
 * old reading can't be uploaded twice.
 */
class LocalReadings(context: Context) {

    private val file = File(context.filesDir, "readings.json")

    @Synchronized
    fun weights(): List<WeightData> = load().optJSONArray("weights").objects().map(::weightFrom)

    @Synchronized
    fun bloodPressures(): List<BloodPressureData> = load().optJSONArray("bp").objects().map(::bpFrom)

    @Synchronized
    fun add(w: WeightData) = save(weights().plus(w).newest { it.timestamp }, bloodPressures())

    @Synchronized
    fun add(b: BloodPressureData) = save(weights(), bloodPressures().plus(b).newest { it.timestamp })

    /** Drops today's readings (local time). */
    @Synchronized
    fun clearToday(zone: ZoneId = ZoneId.systemDefault()) {
        val start = LocalDate.now(zone).atStartOfDay(zone).toInstant()
        save(weights().filter { it.timestamp < start }, bloodPressures().filter { it.timestamp < start })
    }

    private fun <T> List<T>.newest(time: (T) -> Instant): List<T> {
        val cutoff = Instant.now().minus(java.time.Duration.ofDays(MAX_AGE_DAYS))
        return filter { time(it) >= cutoff }.distinctBy(time).sortedByDescending(time).take(KEEP)
    }

    private fun load(): JSONObject = try {
        if (file.exists()) JSONObject(file.readText()) else JSONObject()
    } catch (e: Exception) {
        JSONObject()
    }

    private fun save(weights: List<WeightData>, bps: List<BloodPressureData>) {
        val json = JSONObject()
            .put("weights", JSONArray(weights.map(::weightJson)))
            .put("bp", JSONArray(bps.map(::bpJson)))
        val tmp = File(file.path + ".tmp")
        tmp.writeText(json.toString())
        tmp.renameTo(file)  // whole file or nothing, even if the app dies mid-write
    }

    companion object {
        const val KEEP = 200
        const val MAX_AGE_DAYS = 90L

        private fun JSONArray?.objects(): List<JSONObject> =
            if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONObject(it) }

        private fun JSONObject.floatOrNull(k: String) = if (has(k) && !isNull(k)) getDouble(k).toFloat() else null
        private fun JSONObject.intOrNull(k: String) = if (has(k) && !isNull(k)) getInt(k) else null

        fun weightJson(w: WeightData): JSONObject = JSONObject()
            .put("weight_kg", w.weightKg.toDouble()).put("weight_lb", w.weightLb.toDouble())
            .put("impedance", w.impedance).put("body_fat_percent", w.bodyFatPercent?.toDouble())
            .put("bmi", w.bmi?.toDouble()).put("muscle_mass_lb", w.muscleMassLb?.toDouble())
            .put("water_percent", w.waterPercent?.toDouble()).put("bone_mass_lb", w.boneMassLb?.toDouble())
            .put("visceral_fat", w.visceralFat).put("bmr_kcal", w.bmrKcal)
            .put("timestamp", w.timestamp.toString())
            .put("device_name", w.deviceName).put("device_address", w.deviceAddress)

        /** Also reads the hub's /api/history weight records, which use the same field names. */
        fun weightFrom(o: JSONObject) = WeightData(
            weightKg = o.getDouble("weight_kg").toFloat(),
            weightLb = o.floatOrNull("weight_lb") ?: (o.getDouble("weight_kg") * 2.20462262).toFloat(),
            impedance = o.intOrNull("impedance"),
            bodyFatPercent = o.floatOrNull("body_fat_percent"),
            bmi = o.floatOrNull("bmi"),
            muscleMassLb = o.floatOrNull("muscle_mass_lb"),
            waterPercent = o.floatOrNull("water_percent"),
            boneMassLb = o.floatOrNull("bone_mass_lb"),
            visceralFat = o.intOrNull("visceral_fat"),
            bmrKcal = o.intOrNull("bmr_kcal"),
            timestamp = parseInstant(o.getString("timestamp")),
            deviceName = o.optString("device_name", "Scale"),
            deviceAddress = o.optString("device_address", "")
        )

        fun bpJson(b: BloodPressureData): JSONObject = JSONObject()
            .put("systolic", b.systolic).put("diastolic", b.diastolic).put("pulse", b.pulse)
            .put("timestamp", b.timestamp.toString())
            .put("device_name", b.deviceName).put("device_address", b.deviceAddress)

        fun bpFrom(o: JSONObject) = BloodPressureData(
            systolic = o.getInt("systolic"),
            diastolic = o.getInt("diastolic"),
            pulse = o.optInt("pulse", 0),
            timestamp = parseInstant(o.getString("timestamp")),
            deviceName = o.optString("device_name", "BP monitor"),
            deviceAddress = o.optString("device_address", "")
        )

        /** ISO instants, with or without an offset (the hub may store either). */
        fun parseInstant(s: String): Instant = try {
            Instant.parse(s)
        } catch (e: Exception) {
            try {
                java.time.OffsetDateTime.parse(s).toInstant()
            } catch (e2: Exception) {
                java.time.LocalDateTime.parse(s).atZone(ZoneId.systemDefault()).toInstant()
            }
        }
    }
}
