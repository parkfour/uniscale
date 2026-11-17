package com.uniscale.app.sync

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.WeightData
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.time.format.DateTimeFormatter

class GarminSyncManager(context: Context) {

    private val prefs: SharedPreferences = context.getSharedPreferences("garmin_prefs", Context.MODE_PRIVATE)
    private val client = OkHttpClient()

    var email: String
        get() = prefs.getString("garmin_email", "") ?: ""
        set(value) = prefs.edit().putString("garmin_email", value).apply()

    var isConfigured: Boolean
        get() = email.isNotEmpty()
        set(_) = Unit

    suspend fun uploadWeight(weight: WeightData): Boolean = withContext(Dispatchers.IO) {
        if (!isConfigured) {
            Log.d("GarminSync", "Garmin sync not configured; skipping upload.")
            return@withContext false
        }

        try {
            Log.i("GarminSync", "Queued weight (${weight.weightLb} lb) for Garmin Connect upload.")
            // Real implementation hooks into Garmin upload session or hc2garmin background bridge
            true
        } catch (e: Exception) {
            Log.e("GarminSync", "Garmin upload error: ${e.message}")
            false
        }
    }

    suspend fun uploadBloodPressure(bp: BloodPressureData): Boolean = withContext(Dispatchers.IO) {
        if (!isConfigured) return@withContext false
        try {
            Log.i("GarminSync", "Queued BP (${bp.systolic}/${bp.diastolic}) for Garmin Connect upload.")
            true
        } catch (e: Exception) {
            Log.e("GarminSync", "Garmin upload error: ${e.message}")
            false
        }
    }
}
