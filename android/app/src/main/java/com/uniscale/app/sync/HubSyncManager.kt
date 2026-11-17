package com.uniscale.app.sync

import android.util.Log
import com.uniscale.app.config.AppConfigManager
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.CounterDailyRecord
import com.uniscale.app.model.CounterState
import com.uniscale.app.model.DwellPlace
import com.uniscale.app.model.EcgData
import com.uniscale.app.model.SleepData
import com.uniscale.app.model.WatchActivityData
import com.uniscale.app.model.WeightData
import com.uniscale.app.model.WorkoutData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.TimeUnit

class HubSyncManager(
    private val context: android.content.Context,
    private val configManager: AppConfigManager
) {

    private var cachedClient: OkHttpClient? = null
    private var cachedAlias: String? = null
    private val syncPrefs = context.getSharedPreferences("hub_sync_state", android.content.Context.MODE_PRIVATE)

    init {
        pruneSyncState()
    }

    // Uploads are remembered per hub + identity (token / client cert), so pointing the app at
    // another hub or user uploads everything again. Value = when it was uploaded.
    private fun syncKey(item: String): String {
        val c = configManager.config.value
        val identity = "${c.hubUrl.trimEnd('/')}|${c.apiToken}|${if (c.enableMtls) c.clientCertAlias.orEmpty() else ""}"
        val digest = java.security.MessageDigest.getInstance("SHA-256").digest(identity.toByteArray())
        return digest.take(8).joinToString("") { "%02x".format(it) } + "_" + item
    }

    private fun isSynced(key: String) = syncPrefs.contains(key)

    private fun markSynced(key: String) = syncPrefs.edit().putLong(key, System.currentTimeMillis()).apply()

    // Local readings are kept 90 days and the watch export is read back ~30, so older entries never come up again.
    private fun pruneSyncState(maxAgeDays: Long = 120) {
        val cutoff = System.currentTimeMillis() - maxAgeDays * 86_400_000L
        val stale = syncPrefs.all.filter { (_, v) -> v !is Long || v < cutoff }.keys
        if (stale.isNotEmpty()) {
            syncPrefs.edit().apply { stale.forEach { remove(it) } }.apply()
        }
    }

    @Synchronized
    fun getClient(overrideAlias: String? = null): OkHttpClient {
        val config = configManager.config.value
        val effectiveAlias = if (overrideAlias != null) {
            overrideAlias
        } else if (config.enableMtls) {
            config.clientCertAlias
        } else {
            null
        }

        if (cachedClient != null && cachedAlias == effectiveAlias) {
            return cachedClient!!
        }
        val (newClient, hasCert) = KeyChainKeyManager.createOkHttpClient(context, effectiveAlias)
        // A client built before the app was allowed to use the certificate has none: don't keep it,
        // so the next request (e.g. after picking the certificate) tries the key again.
        if (hasCert || effectiveAlias.isNullOrBlank()) {
            cachedClient = newClient
            cachedAlias = effectiveAlias
        }
        return newClient
    }

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    suspend fun testConnection(
        targetUrl: String,
        token: String? = null,
        enableMtls: Boolean = false,
        certAlias: String? = null
    ): Result<Long> = withContext(Dispatchers.IO) {
        val base = targetUrl.trim().trimEnd('/')
        if (base.isEmpty()) return@withContext Result.failure(Exception("Hub URL cannot be blank"))

        // /api/config needs the token when one is set, so a wrong token fails the test.
        val url = "$base/api/config"
        val start = System.currentTimeMillis()
        try {
            val reqBuilder = Request.Builder().url(url).get()
            if (!token.isNullOrBlank()) {
                reqBuilder.addHeader("Authorization", "Bearer $token")
            }
            val effectiveAlias = if (enableMtls) certAlias else null
            getClient(effectiveAlias).newCall(reqBuilder.build()).execute().use { resp ->
                val elapsed = System.currentTimeMillis() - start
                if (resp.isSuccessful) {
                    Result.success(elapsed)
                } else {
                    Result.failure(Exception("HTTP ${resp.code}: ${resp.message}"))
                }
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun uploadWeight(weight: WeightData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        val syncKey = syncKey("w_${weight.timestamp}")
        if (isSynced(syncKey)) return@withContext true

        try {
            val json = JSONObject()
            json.put("weight_kg", weight.weightKg)
            json.put("weight_lb", weight.weightLb)
            if (weight.bodyFatPercent != null) json.put("body_fat_percent", weight.bodyFatPercent)
            if (weight.bmi != null) json.put("bmi", weight.bmi)
            if (weight.impedance != null) json.put("impedance", weight.impedance)
            if (weight.muscleMassLb != null) json.put("muscle_mass_lb", weight.muscleMassLb)
            if (weight.waterPercent != null) json.put("water_percent", weight.waterPercent)
            if (weight.boneMassLb != null) json.put("bone_mass_lb", weight.boneMassLb)
            if (weight.visceralFat != null) json.put("visceral_fat", weight.visceralFat)
            if (weight.bmrKcal != null) json.put("bmr_kcal", weight.bmrKcal)
            json.put("device_name", weight.deviceName)
            json.put("device_address", weight.deviceAddress)
            json.put("timestamp", weight.timestamp.toString())

            val success = postJson("${config.hubUrl.trimEnd('/')}/api/sync/weight", json, config.apiToken)
            if (success) {
                markSynced(syncKey)
            }
            success
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading weight: ${e.message}")
            false
        }
    }

    suspend fun uploadBloodPressure(bp: BloodPressureData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        val syncKey = syncKey("bp_${bp.timestamp}")
        if (isSynced(syncKey)) return@withContext true

        try {
            val json = JSONObject()
            json.put("systolic", bp.systolic)
            json.put("diastolic", bp.diastolic)
            json.put("pulse", bp.pulse)
            json.put("device_name", bp.deviceName)
            json.put("device_address", bp.deviceAddress)
            json.put("timestamp", bp.timestamp.toString())

            val success = postJson("${config.hubUrl.trimEnd('/')}/api/sync/bp", json, config.apiToken)
            if (success) {
                markSynced(syncKey)
            }
            success
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading BP: ${e.message}")
            false
        }
    }

    suspend fun uploadEcg(ecg: EcgData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        try {
            val json = JSONObject()
            json.put("heart_rate", ecg.heartRate)
            json.put("sample_rate_hz", ecg.sampleRateHz)
            json.put("duration_seconds", ecg.durationSeconds)
            json.put("rhythm_status", ecg.rhythmStatus)
            json.put("device_name", ecg.deviceName)
            json.put("device_address", ecg.deviceAddress)
            json.put("timestamp", ecg.timestamp.toString())

            val samplesArr = JSONArray()
            ecg.samplesMv.forEach { samplesArr.put(it) }
            json.put("samples_mv", samplesArr)

            postJson("${config.hubUrl.trimEnd('/')}/api/sync/ecg", json, config.apiToken)
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading ECG: ${e.message}")
            false
        }
    }

    suspend fun uploadWatchActivity(activity: WatchActivityData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        // Today's running total goes up on every sync; a finished day's final total only once.
        val finished = activity.date.isBefore(java.time.LocalDate.now())
        val key = syncKey("act_${activity.date}")
        if (finished && isSynced(key)) return@withContext true

        try {
            val json = JSONObject()
            json.put("date", activity.date.toString())
            json.put("steps", activity.steps)
            json.put("distance_km", activity.distanceKm)
            json.put("source", activity.deviceName)

            val success = postJson("${config.hubUrl.trimEnd('/')}/api/sync/activity", json, config.apiToken)
            if (success && finished) markSynced(key)
            success
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading watch activity: ${e.message}")
            false
        }
    }

    suspend fun uploadWorkout(w: WorkoutData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        // v2: workouts now carry splits, HR zones and HR samples; re-send the ones already uploaded without them.
        val syncKey = syncKey("workout2_${w.startTime}")
        if (isSynced(syncKey)) return@withContext true

        try {
            val json = JSONObject()
            json.put("sport", w.sport)
            json.put("timestamp", w.startTime.toString())
            json.put("duration_seconds", w.durationSeconds)
            json.put("distance_meters", w.distanceMeters)
            w.calories?.let { json.put("calories", it) }
            w.avgHeartRate?.let { json.put("avg_heart_rate", it) }
            w.maxHeartRate?.let { json.put("max_heart_rate", it) }
            w.avgCadence?.let { json.put("avg_cadence", it) }
            w.elevationGainM?.let { json.put("elevation_gain_m", it) }
            w.steps?.let { json.put("steps", it) }
            w.avgPower?.let { json.put("avg_power", it) }
            w.maxPower?.let { json.put("max_power", it) }
            w.aerobicTrainingEffect?.let { json.put("aerobic_training_effect", it) }
            w.vo2max?.let { json.put("vo2max", it) }
            json.put("device_name", w.deviceName)
            w.extras.forEach { (k, v) -> json.put(k, v) }
            if (w.hrZones.isNotEmpty()) json.put("hr_zones", JSONArray(w.hrZones.map { z ->
                JSONObject().put("zone", z.zone).put("name", z.name).put("min_bpm", z.minBpm ?: JSONObject.NULL)
                    .put("seconds", z.seconds)
            }))
            if (w.splits.isNotEmpty()) json.put("splits", JSONArray(w.splits.map { s ->
                JSONObject().put("distance_m", s.distanceMeters).put("seconds", s.seconds)
                    .put("avg_hr", s.avgHeartRate ?: JSONObject.NULL)
            }))
            if (w.hrSamples.isNotEmpty()) json.put("hr_samples", JSONArray(w.hrSamples.map { (t, bpm) ->
                JSONArray().put(t).put(bpm)
            }))

            val success = postJson("${config.hubUrl.trimEnd('/')}/api/sync/workout", json, config.apiToken)
            if (success) markSynced(syncKey)
            success
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading workout: ${e.message}")
            false
        }
    }

    suspend fun uploadSleepSession(sleep: SleepData): Boolean = withContext(Dispatchers.IO) {
        val config = configManager.config.value
        if (!config.autoUploadHub || config.hubUrl.isBlank()) return@withContext false

        val syncKey = syncKey("sleep_${sleep.date}_${sleep.durationSeconds}_${sleep.deviceAddress}")
        if (isSynced(syncKey)) return@withContext true

        try {
            val json = JSONObject()
            json.put("date", sleep.date)
            json.put("timestamp", sleep.timestamp.toString())
            json.put("duration_seconds", sleep.durationSeconds)
            json.put("deep_sleep_seconds", sleep.deepSleepSeconds)
            json.put("rem_sleep_seconds", sleep.remSleepSeconds)
            json.put("light_sleep_seconds", sleep.lightSleepSeconds)
            json.put("awake_seconds", sleep.awakeSeconds)
            if (sleep.estimatedSleepScore != null) {
                json.put("estimated_sleep_score", sleep.estimatedSleepScore)
            }
            sleep.deviceSleepScore?.let { json.put("sleep_score", it) }
            sleep.hrvRmssdMs?.let { json.put("hrv_rmssd_ms", it.toDouble()) }
            sleep.hrvStatus?.let { json.put("hrv_status", it) }
            sleep.avgSpo2?.let { json.put("avg_spo2", it.toDouble()) }
            sleep.lowestSpo2?.let { json.put("lowest_spo2", it.toDouble()) }
            sleep.avgRespiration?.let { json.put("avg_respiration", it.toDouble()) }
            json.put("device_name", sleep.deviceName)
            json.put("type", "sleep")

            val success = postJson("${config.hubUrl.trimEnd('/')}/api/sync/sleep", json, config.apiToken)
            if (success) {
                markSynced(syncKey)
            }
            success
        } catch (e: Exception) {
            Log.e("HubSync", "Failed uploading sleep session: ${e.message}")
            false
        }
    }

    /**
     * One request to the hub for the interactive features (supplement checklist, Dr. Gemma); unlike
     * the uploads above these don't depend on Auto Upload. Returns the JSON body, or null on any failure.
     * [readTimeoutSeconds] lets a slow call (an LLM reply) wait longer than the default 15 s.
     */
    suspend fun request(path: String, body: JSONObject? = null, readTimeoutSeconds: Long? = null): JSONObject? =
        withContext(Dispatchers.IO) {
            val config = configManager.config.value
            if (config.hubUrl.isBlank()) return@withContext null
            val builder = Request.Builder().url(config.hubUrl.trimEnd('/') + path)
            if (body != null) builder.post(body.toString().toRequestBody(jsonMedia))
            if (config.apiToken.isNotBlank()) builder.addHeader("Authorization", "Bearer ${config.apiToken}")
            val client = readTimeoutSeconds?.let { getClient().newBuilder().readTimeout(it, TimeUnit.SECONDS).build() }
                ?: getClient()
            try {
                client.newCall(builder.build()).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        Log.w("HubSync", "$path failed: HTTP ${resp.code}")
                        return@withContext null
                    }
                    JSONObject(resp.body?.string().orEmpty().ifBlank { "{}" })
                }
            } catch (e: Exception) {
                Log.w("HubSync", "$path failed: ${e.message}")
                null
            }
        }

    /** The hub's newest weigh-ins, newest first; empty when the hub can't be reached. */
    suspend fun fetchWeights(limit: Int = 60): List<WeightData> =
        historyRecords("weight", limit).mapNotNull { runCatching { LocalReadings.weightFrom(it) }.getOrNull() }

    /** The hub's newest blood-pressure readings, newest first; empty when the hub can't be reached. */
    suspend fun fetchBloodPressures(limit: Int = 60): List<BloodPressureData> =
        historyRecords("blood_pressure", limit).mapNotNull { runCatching { LocalReadings.bpFrom(it) }.getOrNull() }

    private suspend fun historyRecords(type: String, limit: Int): List<JSONObject> {
        val records = request("/api/history?record_type=$type&limit=$limit")?.optJSONArray("records") ?: return emptyList()
        return (0 until records.length()).mapNotNull { records.optJSONObject(it) }
    }

    private fun postJson(url: String, json: JSONObject, token: String?): Boolean {
        val reqBuilder = Request.Builder()
            .url(url)
            .post(json.toString().toRequestBody(jsonMedia))

        if (!token.isNullOrBlank()) {
            reqBuilder.addHeader("Authorization", "Bearer $token")
        }

        getClient().newCall(reqBuilder.build()).execute().use { resp ->
            return if (resp.isSuccessful) {
                Log.i("HubSync", "Uploaded successfully to $url [${resp.code}]")
                true
            } else {
                Log.w("HubSync", "Upload failed [$url]: HTTP ${resp.code}")
                false
            }
        }
    }

    private fun parseCounterState(json: JSONObject): CounterState {
        val historyArr = json.optJSONArray("daily_history")
        val daily = mutableListOf<CounterDailyRecord>()
        if (historyArr != null) {
            for (i in 0 until historyArr.length()) {
                val item = historyArr.optJSONObject(i) ?: continue
                daily.add(
                    CounterDailyRecord(
                        date = item.optString("date", ""),
                        dOut = item.optInt("d_out", 0),
                        dIn = item.optInt("d_in", 0)
                    )
                )
            }
        }
        return CounterState(
            dOut = json.optInt("d_out", 0),
            dIn = json.optInt("d_in", 0),
            totalDOut = json.optInt("total_d_out", 0),
            totalDIn = json.optInt("total_d_in", 0),
            date = json.optString("date", ""),
            dailyHistory = daily
        )
    }

    suspend fun fetchCounter(date: String? = null): CounterState? {
        val path = "/api/counter" + if (date != null) "?date=$date" else ""
        val json = request(path) ?: return null
        return parseCounterState(json)
    }

    suspend fun adjustCounter(item: String, delta: Int, date: String? = null): CounterState? {
        val body = JSONObject().apply {
            put("item", item)
            put("delta", delta)
            if (date != null) put("date", date)
        }
        val json = request("/api/counter", body) ?: return null
        return parseCounterState(json)
    }

    suspend fun fetchPlaces(day: String = "previous", date: String? = null): List<DwellPlace> {
        val path = "/api/places?day=$day" + if (date != null) "&date=$date" else ""
        val json = request(path) ?: return emptyList()
        val arr = json.optJSONArray("places") ?: return emptyList()
        val list = mutableListOf<DwellPlace>()
        for (i in 0 until arr.length()) {
            val p = arr.optJSONObject(i) ?: continue
            val cPois = mutableListOf<String>()
            val pArr = p.optJSONArray("candidate_pois")
            if (pArr != null) {
                for (j in 0 until pArr.length()) {
                    cPois.add(pArr.optString(j))
                }
            }
            list.add(
                DwellPlace(
                    id = p.optString("id"),
                    date = p.optString("date"),
                    avgLat = p.optDouble("avg_lat", 0.0),
                    avgLon = p.optDouble("avg_lon", 0.0),
                    confidenceRange = p.optDouble("confidence_range", 50.0),
                    startTime = p.optString("start_time"),
                    endTime = p.optString("end_time"),
                    durationSeconds = p.optLong("duration_seconds", 0L),
                    dwellFormatted = p.optString("dwell_formatted", "1h"),
                    name = p.optString("name").takeIf { it.isNotBlank() && it != "null" },
                    poiType = p.optString("poi_type").takeIf { it.isNotBlank() && it != "null" },
                    candidatePois = cPois,
                    isDeleted = p.optBoolean("is_deleted", false)
                )
            )
        }
        return list
    }

    suspend fun updatePlace(
        placeId: String,
        name: String? = null,
        poiType: String? = null,
        candidatePois: List<String>? = null,
        isDeleted: Boolean? = null
    ): Boolean {
        val body = JSONObject()
        name?.let { body.put("name", it) }
        poiType?.let { body.put("poi_type", it) }
        candidatePois?.let { body.put("candidate_pois", JSONArray(it)) }
        isDeleted?.let { body.put("is_deleted", it) }

        val res = request("/api/places/$placeId", body)
        return res != null
    }

    suspend fun deletePlace(placeId: String): Boolean = updatePlace(placeId, isDeleted = true)
}
