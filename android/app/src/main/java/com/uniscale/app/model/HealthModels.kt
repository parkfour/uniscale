package com.uniscale.app.model

import org.json.JSONArray
import org.json.JSONObject

import java.time.Instant

enum class DeviceCategory {
    YUNMAI_SCALE,
    FITDAYS_SCALE,
    ETEKCITY_SCALE,
    VIATOM_BP2,
    OMRON_BP,
    UNKNOWN
}

data class BodyComposition(
    val bodyFatPercent: Float,
    val muscleMassKg: Float,
    val muscleMassLb: Float,
    val waterPercent: Float,
    val boneMassKg: Float,
    val boneMassLb: Float,
    val visceralFat: Int,
    val bmrKcal: Int,
    val bmi: Float
)

data class WeightData(
    val weightKg: Float,
    val weightLb: Float = (weightKg * 2.20462262f).let { Math.round(it * 10f) / 10f },
    val impedance: Int? = null,
    val bodyFatPercent: Float? = null,
    val bmi: Float? = null,
    val muscleMassLb: Float? = null,
    val waterPercent: Float? = null,
    val boneMassLb: Float? = null,
    val visceralFat: Int? = null,
    val bmrKcal: Int? = null,
    val timestamp: Instant = Instant.now(),
    val deviceName: String,
    val deviceAddress: String = ""
)

object BiaCalculator {
    fun calculate(
        weightKg: Float,
        impedance: Int,
        heightCm: Float,
        age: Int,
        isMale: Boolean = true,
        isAthlete: Boolean = false
    ): BodyComposition {
        val hM = heightCm / 100f
        val bmi = Math.round((weightKg / (hM * hM)) * 10f) / 10f

        val biaIndex = (heightCm * heightCm) / impedance.toFloat().coerceAtLeast(100f)
        val athleteFactor = if (isAthlete) 1.04f else 1.0f

        val baseFfm = if (isMale) {
            (0.485f * biaIndex + 0.338f * weightKg + 5.32f) * athleteFactor
        } else {
            (0.475f * biaIndex + 0.295f * weightKg + 5.49f) * athleteFactor
        }
        val ffmKg = baseFfm.coerceIn(weightKg * 0.40f, weightKg * 0.95f)

        val fatMassKg = (weightKg - ffmKg).coerceAtLeast(0f)
        val bodyFatPercent = Math.round(((fatMassKg / weightKg) * 100f) * 10f) / 10f

        val boneMassKg = Math.round((0.05f * ffmKg) * 10f) / 10f
        val boneMassLb = Math.round((boneMassKg * 2.20462f) * 10f) / 10f

        val muscleMassKg = Math.round((ffmKg - boneMassKg) * 10f) / 10f
        val muscleMassLb = Math.round((muscleMassKg * 2.20462f) * 10f) / 10f

        val tbwKg = ffmKg * 0.732f
        val waterPercent = Math.round(((tbwKg / weightKg) * 100f) * 10f) / 10f

        val rawVf = (0.12f * bmi) + (0.15f * bodyFatPercent) + (0.05f * age) - 3.5f
        val visceralFat = Math.round(rawVf).coerceIn(1, 30)

        val bmr = if (isMale) {
            (10f * weightKg + 6.25f * heightCm - 5f * age + 5f).toInt()
        } else {
            (10f * weightKg + 6.25f * heightCm - 5f * age - 161f).toInt()
        }

        return BodyComposition(
            bodyFatPercent = bodyFatPercent,
            muscleMassKg = muscleMassKg,
            muscleMassLb = muscleMassLb,
            waterPercent = waterPercent,
            boneMassKg = boneMassKg,
            boneMassLb = boneMassLb,
            visceralFat = visceralFat,
            bmrKcal = bmr,
            bmi = bmi
        )
    }
}

data class BloodPressureData(
    val systolic: Int,
    val diastolic: Int,
    val pulse: Int,
    val timestamp: Instant = Instant.now(),
    val deviceName: String,
    val deviceAddress: String = ""
)

data class EcgData(
    val heartRate: Int,
    val sampleRateHz: Int = 125,
    val samplesMv: List<Float> = emptyList(),
    val durationSeconds: Float = 30f,
    val rhythmStatus: String = "Normal Sinus Rhythm",
    val timestamp: Instant = Instant.now(),
    val deviceName: String,
    val deviceAddress: String
)

data class WatchActivityData(
    val steps: Long = 0,
    val distanceKm: Float = 0f,
    val calories: Long = 0,
    val deviceName: String = "Garmin",
    val timestamp: Instant = Instant.now(),
    val date: java.time.LocalDate = java.time.LocalDate.now()
)

data class SleepData(
    val date: String,
    val timestamp: Instant = Instant.now(),
    val durationSeconds: Long = 0,
    val deepSleepSeconds: Long = 0,
    val remSleepSeconds: Long = 0,
    val lightSleepSeconds: Long = 0,
    val awakeSeconds: Long = 0,
    val estimatedSleepScore: Int? = null, // computed on the phone, not reported by the watch
    val deviceSleepScore: Int? = null, // reported by the watch (Garmin, via Gadgetbridge)
    val hrvRmssdMs: Float? = null,
    val hrvStatus: String? = null,
    val avgSpo2: Float? = null,
    val lowestSpo2: Float? = null,
    val avgRespiration: Float? = null,
    val deviceName: String = "Garmin",
    val deviceAddress: String = ""
) {
    val durationHours: Float
        get() = ((durationSeconds / 3600f) * 10f).let { Math.round(it) / 10f }
    val deepSleepMinutes: Long
        get() = Math.round(deepSleepSeconds / 60.0)
    val remSleepMinutes: Long
        get() = Math.round(remSleepSeconds / 60.0)
    val lightSleepMinutes: Long
        get() = Math.round(lightSleepSeconds / 60.0)
    val awakeMinutes: Long
        get() = Math.round(awakeSeconds / 60.0)
}

/** A watch workout summary (e.g. a run), as stored by Gadgetbridge. */
data class WorkoutData(
    val sport: String,
    val startTime: Instant,
    val durationSeconds: Double,
    val distanceMeters: Double = 0.0,
    val calories: Int? = null,
    val avgHeartRate: Int? = null,
    val maxHeartRate: Int? = null,
    val avgCadence: Int? = null, // FIT convention: strides/min for running (steps/min ÷ 2)
    val elevationGainM: Double? = null,
    val steps: Int? = null,
    val avgPower: Int? = null,
    val maxPower: Int? = null,
    val aerobicTrainingEffect: Double? = null,
    val vo2max: Double? = null,
    val deviceName: String = "Garmin",
    /** Further numeric metrics (training load, running form, ...), keyed by the server's field name. */
    val extras: Map<String, Double> = emptyMap(),
    val hrZones: List<HrZone> = emptyList(),
    val splits: List<WorkoutSplit> = emptyList(),
    /** Heart rate during the workout: (seconds from start, bpm), roughly one per minute. */
    val hrSamples: List<Pair<Int, Int>> = emptyList()
)

/** Time spent in one heart-rate zone (1 = warm up ... 5 = maximum) and the bpm where the zone starts. */
data class HrZone(val zone: Int, val name: String, val minBpm: Int?, val seconds: Double)

/** One lap / auto-split (usually 1 km). */
data class WorkoutSplit(val distanceMeters: Double, val seconds: Double, val avgHeartRate: Int?)

data class DiscoveredDevice(
    val name: String,
    val address: String,
    val category: DeviceCategory,
    val rssi: Int,
    val lastSeen: Instant = Instant.now()
)

/** One button of the counter tile; [key] is how the hub stores it ("D(out)" -> "d_out"). */
data class CounterElement(val key: String, val label: String)

data class CounterDailyRecord(
    val date: String,
    val counts: Map<String, Int> = emptyMap(),
    val total: Int = 0
)

/** The counter set from the hub profile (no elements: none set up) and its counts. */
data class CounterState(
    val title: String? = null,
    val elements: List<CounterElement> = emptyList(),
    val date: String = "",
    val counts: Map<String, Int> = emptyMap(),
    val totals: Map<String, Int> = emptyMap(),
    val dailyHistory: List<CounterDailyRecord> = emptyList()
) {
    companion object {
        private fun JSONObject?.intMap(): Map<String, Int> =
            this?.keys()?.asSequence()?.associateWith { optInt(it, 0) } ?: emptyMap()

        private fun JSONArray?.objects(): List<JSONObject> =
            if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONObject(it) }

        /** From the hub's /api/counter response. */
        fun fromJson(json: JSONObject) = CounterState(
            title = json.optString("title").takeIf { it.isNotBlank() && it != "null" },
            elements = json.optJSONArray("elements").objects().map { CounterElement(it.optString("key"), it.optString("label")) },
            date = json.optString("date", ""),
            counts = json.optJSONObject("counts").intMap(),
            totals = json.optJSONObject("totals").intMap(),
            dailyHistory = json.optJSONArray("daily_history").objects().map {
                CounterDailyRecord(it.optString("date", ""), it.optJSONObject("counts").intMap(), it.optInt("total", 0))
            }
        )
    }
}

/** Correlations the hub found between counters, places and health; `lines` are ready to show. */
data class PatternsState(
    val days: Int = 90,
    val loggedDays: Int = 0,
    val lines: List<String> = emptyList()
) {
    companion object {
        /** From the hub's /api/patterns response. */
        fun fromJson(json: JSONObject): PatternsState {
            val arr = json.optJSONArray("patterns")
            val lines = if (arr == null) emptyList() else
                (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.optString("text")?.takeIf(String::isNotBlank) }
            return PatternsState(json.optInt("days", 90), json.optInt("logged_days", 0), lines)
        }
    }
}

data class DwellPlace(
    val id: String,
    val date: String,
    val avgLat: Double,
    val avgLon: Double,
    val confidenceRange: Double,
    val startTime: String,
    val endTime: String,
    val durationSeconds: Long,
    val dwellFormatted: String,
    val name: String? = null,
    val poiType: String? = null,
    val candidatePois: List<String> = emptyList(),
    val isDeleted: Boolean = false
)

data class PoiCandidate(
    val name: String,
    val type: String? = null,
    val distanceMeters: Double? = null
)

