package com.uniscale.app.sync

import android.content.Context
import android.util.Log
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.feature.ExperimentalFeatureAvailabilityApi
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.BloodPressureRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.health.connect.client.units.Mass
import androidx.health.connect.client.units.Pressure
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.SleepData
import com.uniscale.app.model.WatchActivityData
import com.uniscale.app.model.WeightData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.math.roundToInt

class HealthConnectManager(private val context: Context) {

    private val healthConnectClient: HealthConnectClient? by lazy {
        if (HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE) {
            HealthConnectClient.getOrCreate(context)
        } else {
            null
        }
    }

    val isAvailable: Boolean
        get() = healthConnectClient != null

    /** Health Connect permissions still to be granted (must match the manifest). */
    @OptIn(ExperimentalFeatureAvailabilityApi::class)
    suspend fun missingPermissions(): Set<String> {
        val client = healthConnectClient ?: return emptySet()
        return try {
            // Lets the background sync read steps, weight and BP while the app is closed (Android 14+).
            val wanted = if (client.features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND) ==
                HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
            ) PERMISSIONS + HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND else PERMISSIONS
            wanted - client.permissionController.getGrantedPermissions()
        } catch (e: Exception) {
            Log.e("HealthConnect", "Could not check permissions: ${e.message}")
            emptySet()
        }
    }

    companion object {
        val PERMISSIONS: Set<String> = listOf(
            "READ_WEIGHT", "WRITE_WEIGHT", "READ_BLOOD_PRESSURE", "WRITE_BLOOD_PRESSURE",
            "READ_HEART_RATE", "WRITE_HEART_RATE", "READ_STEPS", "READ_DISTANCE", "READ_SLEEP",
            "READ_HEART_RATE_VARIABILITY", "READ_TOTAL_CALORIES_BURNED"
        ).map { "android.permission.health.$it" }.toSet()
    }

    suspend fun writeWeight(data: WeightData): Boolean = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext false
        try {
            val record = WeightRecord(
                weight = Mass.kilograms(data.weightKg.toDouble()),
                time = data.timestamp,
                zoneOffset = ZoneOffset.UTC
            )
            client.insertRecords(listOf(record))
            Log.i("HealthConnect", "Inserted weight record: ${data.weightLb} lb into Health Connect")
            true
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to insert weight: ${e.message}")
            false
        }
    }

    suspend fun writeBloodPressure(data: BloodPressureData): Boolean = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext false
        try {
            val record = BloodPressureRecord(
                systolic = Pressure.millimetersOfMercury(data.systolic.toDouble()),
                diastolic = Pressure.millimetersOfMercury(data.diastolic.toDouble()),
                time = data.timestamp,
                zoneOffset = ZoneOffset.UTC
            )
            client.insertRecords(listOf(record))
            Log.i("HealthConnect", "Inserted BP record: ${data.systolic}/${data.diastolic} into Health Connect")
            true
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to insert BP: ${e.message}")
            false
        }
    }

    suspend fun readWeightHistory(limit: Int = 100): List<WeightData> = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext emptyList()
        try {
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = WeightRecord::class,
                    timeRangeFilter = TimeRangeFilter.after(Instant.EPOCH),
                    pageSize = limit
                )
            )
            val list = response.records.map { r ->
                val kg = r.weight.inKilograms.toFloat()
                val lb = ((kg * 2.20462f) * 10f).roundToInt() / 10f
                val origin = r.metadata.dataOrigin.packageName
                WeightData(
                    weightKg = ((kg * 10f).roundToInt() / 10f),
                    weightLb = lb,
                    deviceName = formatOrigin(origin),
                    deviceAddress = origin,
                    timestamp = r.time
                )
            }.sortedByDescending { it.timestamp }
            Log.i("HealthConnect", "Read ${list.size} historical weight records from Health Connect.")
            list
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to read weight history: ${e.message}")
            emptyList()
        }
    }

    suspend fun readBloodPressureHistory(limit: Int = 100): List<BloodPressureData> = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext emptyList()
        try {
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = BloodPressureRecord::class,
                    timeRangeFilter = TimeRangeFilter.after(Instant.EPOCH),
                    pageSize = limit
                )
            )
            val list = response.records.map { r ->
                val origin = r.metadata.dataOrigin.packageName
                BloodPressureData(
                    systolic = r.systolic.inMillimetersOfMercury.toInt(),
                    diastolic = r.diastolic.inMillimetersOfMercury.toInt(),
                    pulse = 0,
                    deviceName = formatOrigin(origin),
                    deviceAddress = origin,
                    timestamp = r.time
                )
            }.sortedByDescending { it.timestamp }
            Log.i("HealthConnect", "Read ${list.size} historical BP records from Health Connect.")
            list
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to read BP history: ${e.message}")
            emptyList()
        }
    }

    suspend fun readTodayWatchActivity(): WatchActivityData? = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext null
        try {
            val startOfDay = LocalDate.now().atStartOfDay(ZoneId.systemDefault()).toInstant()
            // Query specifically for Gadgetbridge records with dedicated DataOrigin filter
            val gbOrigin = androidx.health.connect.client.records.metadata.DataOrigin("nodomain.freeyourgadget.gadgetbridge")
            val gbStepsResp = client.readRecords(
                ReadRecordsRequest(
                    recordType = StepsRecord::class,
                    timeRangeFilter = TimeRangeFilter.after(startOfDay),
                    dataOriginFilter = setOf(gbOrigin)
                )
            )
            val gbSteps = gbStepsResp.records.sumOf { it.count }

            val gbDistResp = client.readRecords(
                ReadRecordsRequest(
                    recordType = DistanceRecord::class,
                    timeRangeFilter = TimeRangeFilter.after(startOfDay),
                    dataOriginFilter = setOf(gbOrigin)
                )
            )
            val gbDistMeters = gbDistResp.records.sumOf { it.distance.inMeters }

            Log.i("HealthConnectGBDirect", "Direct GB query: $gbSteps steps (in ${gbStepsResp.records.size} recs), $gbDistMeters meters (in ${gbDistResp.records.size} recs)")
            for (r in gbDistResp.records) {
                Log.i("HealthConnectGBDirect", "GB Dist sample: ${r.distance.inMeters}m [${r.startTime} -> ${r.endTime}]")
            }

            // Prefer Gadgetbridge metrics if available
            val totalSteps: Long
            val totalMeters: Double

            if (gbStepsResp.records.isNotEmpty()) {
                totalSteps = gbSteps
                if (gbDistMeters > 0) {
                    totalMeters = gbDistMeters
                } else {
                    // In Garmin / Gadgetbridge, all-day walking distance is stride length (~0.855m) * steps
                    // 14010 steps * 0.8558m = 11.99 km
                    totalMeters = totalSteps * 0.85582
                }
            } else {
                val fallbackSteps = client.readRecords(
                    ReadRecordsRequest(
                        recordType = StepsRecord::class,
                        timeRangeFilter = TimeRangeFilter.after(startOfDay)
                    )
                )
                totalSteps = fallbackSteps.records.sumOf { it.count }
                totalMeters = totalSteps * 0.85582
            }

            val km = (totalMeters / 1000.0 * 100).roundToInt() / 100f // 2 decimal places e.g. 11.99 km

            if (totalSteps > 0 || km > 0) {
                Log.i("HealthConnect", "Read watch activity: $totalSteps steps, $km km")
                WatchActivityData(
                    steps = totalSteps,
                    distanceKm = km,
                    deviceName = "Garmin",
                    timestamp = Instant.now()
                )
            } else {
                null
            }
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to read watch activity: ${e.message}")
            null
        }
    }

    suspend fun readSleepHistory(days: Int = 30): List<SleepData> = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext emptyList()
        try {
            val startTime = Instant.now().minus(java.time.Duration.ofDays(days.toLong()))
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = SleepSessionRecord::class,
                    timeRangeFilter = TimeRangeFilter.after(startTime),
                    pageSize = 100
                )
            )

            val list = response.records.map { r ->
                val origin = r.metadata.dataOrigin.packageName
                val totalDuration = java.time.Duration.between(r.startTime, r.endTime).seconds
                var deepSec = 0L
                var remSec = 0L
                var lightSec = 0L
                var awakeSec = 0L

                for (stage in r.stages) {
                    val stageDur = java.time.Duration.between(stage.startTime, stage.endTime).seconds
                    when (stage.stage) {
                        SleepSessionRecord.STAGE_TYPE_DEEP -> deepSec += stageDur
                        SleepSessionRecord.STAGE_TYPE_REM -> remSec += stageDur
                        SleepSessionRecord.STAGE_TYPE_LIGHT,
                        SleepSessionRecord.STAGE_TYPE_SLEEPING -> lightSec += stageDur
                        SleepSessionRecord.STAGE_TYPE_AWAKE,
                        SleepSessionRecord.STAGE_TYPE_OUT_OF_BED,
                        SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED -> awakeSec += stageDur
                        else -> lightSec += stageDur
                    }
                }

                if (deepSec == 0L && remSec == 0L && lightSec == 0L && totalDuration > 0) {
                    lightSec = (totalDuration - awakeSec).coerceAtLeast(0L)
                }

                val localDate = r.endTime.atZone(ZoneId.systemDefault()).toLocalDate().toString()
                val score = calculateSleepScore(totalDuration, deepSec, remSec, awakeSec)
                val hrv = readHrvForSleepWindow(r.startTime, r.endTime)

                SleepData(
                    date = localDate,
                    timestamp = r.endTime,
                    durationSeconds = totalDuration,
                    deepSleepSeconds = deepSec,
                    remSleepSeconds = remSec,
                    lightSleepSeconds = lightSec,
                    awakeSeconds = awakeSec,
                    estimatedSleepScore = score,
                    hrvRmssdMs = hrv,
                    deviceName = formatOrigin(origin),
                    deviceAddress = origin
                )
            }.sortedByDescending { it.timestamp }

            Log.i("HealthConnect", "Read ${list.size} historical sleep records from Health Connect.")
            list
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed to read sleep history: ${e.message}")
            emptyList()
        }
    }

    private suspend fun readHrvForSleepWindow(start: Instant, end: Instant): Float? {
        val client = healthConnectClient ?: return null
        return try {
            val resp = client.readRecords(
                ReadRecordsRequest(
                    recordType = HeartRateVariabilityRmssdRecord::class,
                    timeRangeFilter = TimeRangeFilter.between(start, end),
                    pageSize = 100
                )
            )
            if (resp.records.isNotEmpty()) {
                val avg = resp.records.map { it.heartRateVariabilityMillis }.average()
                ((avg * 10).roundToInt() / 10f)
            } else {
                null
            }
        } catch (e: Exception) {
            null
        }
    }

    // Rough estimate from stages, sent as estimated_sleep_score; null when it would be meaningless.
    private fun calculateSleepScore(durationSec: Long, deepSec: Long, remSec: Long, awakeSec: Long): Int? {
        val totalAsleepSec = (durationSec - awakeSec).coerceAtLeast(0L)
        if (totalAsleepSec < 1800 || deepSec + remSec == 0L) return null // nap, or no stage data

        // 1. Duration score (max 50 pts) - optimal 7.5h (27000s)
        val durHours = totalAsleepSec / 3600.0
        val durScore = when {
            durHours in 7.0..9.0 -> 50.0
            durHours in 6.0..7.0 -> 40.0 + (durHours - 6.0) * 10.0
            durHours in 5.0..6.0 -> 28.0 + (durHours - 5.0) * 12.0
            durHours in 4.0..5.0 -> 15.0 + (durHours - 4.0) * 13.0
            durHours > 9.0 -> (50.0 - (durHours - 9.0) * 5.0).coerceAtLeast(35.0)
            else -> (durHours / 4.0) * 15.0
        }

        // 2. Deep sleep score (max 25 pts) - optimal 15-25%
        val deepPct = if (totalAsleepSec > 0) (deepSec.toDouble() / totalAsleepSec) * 100.0 else 0.0
        val deepScore = when {
            deepPct >= 15.0 -> 25.0
            deepPct >= 10.0 -> 18.0 + (deepPct - 10.0) * 1.4
            deepPct >= 5.0 -> 10.0 + (deepPct - 5.0) * 1.6
            else -> deepPct * 2.0
        }

        // 3. REM sleep score (max 25 pts) - optimal 20-25%
        val remPct = if (totalAsleepSec > 0) (remSec.toDouble() / totalAsleepSec) * 100.0 else 0.0
        val remScore = when {
            remPct >= 20.0 -> 25.0
            remPct >= 15.0 -> 18.0 + (remPct - 15.0) * 1.4
            remPct >= 8.0 -> 10.0 + (remPct - 8.0) * 1.14
            else -> remPct * 1.25
        }

        // 4. Awake penalty (deduct up to 10 pts)
        val awakeMin = awakeSec / 60.0
        val awakePenalty = when {
            awakeMin > 60.0 -> 10.0
            awakeMin > 40.0 -> 5.0
            awakeMin > 20.0 -> 2.0
            else -> 0.0
        }

        val total = (durScore + deepScore + remScore - awakePenalty).roundToInt()
        return total.coerceIn(0, 100)
    }

    private suspend fun <T : androidx.health.connect.client.records.Record> readAllRecords(
        client: HealthConnectClient,
        recordType: kotlin.reflect.KClass<T>,
        timeFilter: TimeRangeFilter
    ): List<T> {
        val list = mutableListOf<T>()
        var pageToken: String? = null
        do {
            val req = ReadRecordsRequest(
                recordType = recordType,
                timeRangeFilter = timeFilter,
                pageSize = 1000,
                pageToken = pageToken
            )
            val resp = client.readRecords(req)
            list.addAll(resp.records)
            pageToken = resp.pageToken
        } while (pageToken != null)
        return list
    }

    suspend fun exportAllHealthConnectData(): String = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext "Health Connect client not available"
        try {
            val root = org.json.JSONObject()
            val startTime = Instant.EPOCH
            val endTime = Instant.now()
            val timeFilter = TimeRangeFilter.between(startTime, endTime)

            // 1. Weights
            val weightRecords = readAllRecords(client, WeightRecord::class, timeFilter)
            val weightsArr = org.json.JSONArray()
            for (r in weightRecords) {
                val obj = org.json.JSONObject()
                obj.put("weight_kg", r.weight.inKilograms)
                obj.put("weight_lb", (r.weight.inKilograms * 2.20462 * 10).roundToInt() / 10.0)
                obj.put("time", r.time.toString())
                obj.put("origin", r.metadata.dataOrigin.packageName)
                weightsArr.put(obj)
            }
            root.put("weights", weightsArr)

            // 2. Blood Pressure
            val bpRecords = readAllRecords(client, BloodPressureRecord::class, timeFilter)
            val bpArr = org.json.JSONArray()
            for (r in bpRecords) {
                val obj = org.json.JSONObject()
                obj.put("systolic", r.systolic.inMillimetersOfMercury.roundToInt())
                obj.put("diastolic", r.diastolic.inMillimetersOfMercury.roundToInt())
                obj.put("time", r.time.toString())
                obj.put("origin", r.metadata.dataOrigin.packageName)
                bpArr.put(obj)
            }
            root.put("blood_pressure", bpArr)

            // 3. Steps
            val stepRecords = readAllRecords(client, StepsRecord::class, timeFilter)
            val stepsArr = org.json.JSONArray()
            for (r in stepRecords) {
                val obj = org.json.JSONObject()
                obj.put("count", r.count)
                obj.put("start_time", r.startTime.toString())
                obj.put("end_time", r.endTime.toString())
                obj.put("origin", r.metadata.dataOrigin.packageName)
                stepsArr.put(obj)
            }
            root.put("steps", stepsArr)

            // 4. Distance
            val distRecords = readAllRecords(client, DistanceRecord::class, timeFilter)
            val distArr = org.json.JSONArray()
            for (r in distRecords) {
                val obj = org.json.JSONObject()
                obj.put("distance_meters", r.distance.inMeters)
                obj.put("start_time", r.startTime.toString())
                obj.put("end_time", r.endTime.toString())
                obj.put("origin", r.metadata.dataOrigin.packageName)
                distArr.put(obj)
            }
            root.put("distance", distArr)

            // 5. Sleep Sessions
            val sleepRecords = readAllRecords(client, SleepSessionRecord::class, timeFilter)
            val sleepArr = org.json.JSONArray()
            for (r in sleepRecords) {
                val obj = org.json.JSONObject()
                obj.put("start_time", r.startTime.toString())
                obj.put("end_time", r.endTime.toString())
                obj.put("origin", r.metadata.dataOrigin.packageName)
                val stagesArr = org.json.JSONArray()
                for (s in r.stages) {
                    val sObj = org.json.JSONObject()
                    sObj.put("stage", s.stage)
                    sObj.put("start_time", s.startTime.toString())
                    sObj.put("end_time", s.endTime.toString())
                    stagesArr.put(sObj)
                }
                obj.put("stages", stagesArr)
                sleepArr.put(obj)
            }
            root.put("sleep", sleepArr)

            // 6. Heart Rate
            var hrCount = 0
            try {
                val hrRecords = readAllRecords(client, HeartRateRecord::class, timeFilter)
                val hrArr = org.json.JSONArray()
                for (r in hrRecords) {
                    val obj = org.json.JSONObject()
                    obj.put("start_time", r.startTime.toString())
                    obj.put("end_time", r.endTime.toString())
                    obj.put("origin", r.metadata.dataOrigin.packageName)
                    val samplesArr = org.json.JSONArray()
                    for (s in r.samples) {
                        val sObj = org.json.JSONObject()
                        sObj.put("time", s.time.toString())
                        sObj.put("bpm", s.beatsPerMinute)
                        samplesArr.put(sObj)
                    }
                    obj.put("samples", samplesArr)
                    hrArr.put(obj)
                }
                root.put("heart_rate", hrArr)
                hrCount = hrRecords.size
            } catch (e: Exception) {
                Log.w("HealthConnectExport", "HeartRate query failed: ${e.message}")
            }

            // 7. Heart Rate Variability
            var hrvCount = 0
            try {
                val hrvRecords = readAllRecords(client, HeartRateVariabilityRmssdRecord::class, timeFilter)
                val hrvArr = org.json.JSONArray()
                for (r in hrvRecords) {
                    val obj = org.json.JSONObject()
                    obj.put("time", r.time.toString())
                    obj.put("hrv_rmssd", r.heartRateVariabilityMillis)
                    obj.put("origin", r.metadata.dataOrigin.packageName)
                    hrvArr.put(obj)
                }
                root.put("heart_rate_variability", hrvArr)
                hrvCount = hrvRecords.size
            } catch (e: Exception) {
                Log.w("HealthConnectExport", "HRV query failed: ${e.message}")
            }

            // 8. Calories Burned
            var calCount = 0
            try {
                val calRecords = readAllRecords(client, TotalCaloriesBurnedRecord::class, timeFilter)
                val calArr = org.json.JSONArray()
                for (r in calRecords) {
                    val obj = org.json.JSONObject()
                    obj.put("start_time", r.startTime.toString())
                    obj.put("end_time", r.endTime.toString())
                    obj.put("energy_kcal", r.energy.inKilocalories)
                    obj.put("origin", r.metadata.dataOrigin.packageName)
                    calArr.put(obj)
                }
                root.put("calories_burned", calArr)
                calCount = calRecords.size
            } catch (e: Exception) {
                Log.w("HealthConnectExport", "Calories query failed: ${e.message}")
            }

            val downloadDir = android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS)
            val exportFile = java.io.File(downloadDir, "uniscale_health_connect_export.json")
            exportFile.writeText(root.toString(2))
            val msg = "Exported ${weightRecords.size} weights, ${bpRecords.size} BPs, ${stepRecords.size} steps, ${sleepRecords.size} sleep, $hrCount HR, $hrvCount HRV, $calCount cal to ${exportFile.name}"
            Log.i("HealthConnectExport", msg)
            msg
        } catch (e: Exception) {
            Log.e("HealthConnectExport", "Export failed: ${e.message}")
            "Export error: ${e.message}"
        }
    }

    suspend fun deleteTodayRecords(): String = withContext(Dispatchers.IO) {
        val client = healthConnectClient ?: return@withContext "Health Connect not available"
        try {
            val startOfToday = java.time.LocalDate.now()
                .atStartOfDay(java.time.ZoneId.systemDefault())
                .toInstant()
            val timeFilter = TimeRangeFilter.after(startOfToday)
            val weightsResp = client.readRecords(ReadRecordsRequest(WeightRecord::class, timeFilter))
            val weightIds = weightsResp.records.map { it.metadata.id }
            if (weightIds.isNotEmpty()) {
                client.deleteRecords(WeightRecord::class, recordIdsList = weightIds, clientRecordIdsList = emptyList())
            }
            val bpResp = client.readRecords(ReadRecordsRequest(BloodPressureRecord::class, timeFilter))
            val bpIds = bpResp.records.map { it.metadata.id }
            if (bpIds.isNotEmpty()) {
                client.deleteRecords(BloodPressureRecord::class, recordIdsList = bpIds, clientRecordIdsList = emptyList())
            }
            val msg = "Deleted ${weightIds.size} weights and ${bpIds.size} BPs recorded today from Health Connect."
            Log.i("HealthConnect", msg)
            msg
        } catch (e: Exception) {
            Log.e("HealthConnect", "Failed deleting today records: ${e.message}")
            "Error: ${e.message}"
        }
    }

    private fun formatOrigin(pkg: String): String {
        return when {
            pkg.contains("fitdays") -> "Fitdays"
            pkg.contains("vesync") -> "VeSync"
            pkg.contains("fitness") -> "Google Fit"
            pkg.contains("healthsync") -> "Health Sync"
            pkg.contains("vihealth") -> "ViHealth"
            pkg.contains("omron") -> "Omron"
            pkg.contains("uniscale") -> "UniScale"
            else -> pkg.substringAfterLast('.')
        }
    }
}
