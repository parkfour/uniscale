package com.uniscale.app.config

import android.content.Context
import android.os.Environment
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import java.io.File
import java.io.FileNotFoundException

data class AppConfig(
    val hubUrl: String = "https://health.example.com",
    val apiToken: String = "",
    val enableMtls: Boolean = false,
    val clientCertAlias: String? = null,
    val autoUploadHub: Boolean = true,
    val autoSyncHealthConnect: Boolean = true,
    val forceYunmaiPounds: Boolean = true,
    val preferredUnit: String = "lb",
    val enableYunmai: Boolean = true,
    val enableFitdays: Boolean = true,
    val enableVesync: Boolean = true,
    val enableViatomBp2: Boolean = true,
    val enableOmron: Boolean = true,
    val userHeightCm: Float = 175.0f,
    val userAge: Int = 40,
    val userSex: String = "male",
    val userIsAthlete: Boolean = false,
    val themeMode: String = "system",
    // How often the watch export is synced to the hub in the background; 0 = off.
    val watchSyncMinutes: Int = 60,
    // Supplement reminders: a notification at each slot's time while that slot has unticked doses.
    val supplementReminders: Boolean = false,
    val slotTimes: Map<String, String> = DEFAULT_SLOT_TIMES,
    // Sound when a scale, BP or ECG reading finishes: a ringtone URI, "" = silent.
    val doneSound: String = DEFAULT_DONE_SOUND,
    // Google Maps API key (optional fallback for Points of Interest within dwell places).
    val googleMapsApiKey: String = ""
) {
    fun toJson(): JSONObject {
        val json = JSONObject()
        json.put("hub_url", hubUrl)
        json.put("api_token", apiToken)
        json.put("enable_mtls", enableMtls)
        if (!clientCertAlias.isNullOrBlank()) {
            json.put("client_cert_alias", clientCertAlias)
        }
        json.put("auto_upload_hub", autoUploadHub)
        json.put("auto_sync_health_connect", autoSyncHealthConnect)
        json.put("force_yunmai_pounds", forceYunmaiPounds)
        json.put("preferred_unit", preferredUnit)
        json.put("enable_yunmai", enableYunmai)
        json.put("enable_fitdays", enableFitdays)
        json.put("enable_vesync", enableVesync)
        json.put("enable_viatom_bp2", enableViatomBp2)
        json.put("enable_omron", enableOmron)
        json.put("user_height_cm", userHeightCm.toDouble())
        json.put("user_age", userAge)
        json.put("user_sex", userSex)
        json.put("user_is_athlete", userIsAthlete)
        json.put("theme_mode", themeMode)
        json.put("watch_sync_minutes", watchSyncMinutes)
        json.put("supplement_reminders", supplementReminders)
        json.put("slot_times", JSONObject(slotTimes))
        json.put("done_sound", doneSound)
        if (googleMapsApiKey.isNotBlank()) {
            json.put("google_maps_api_key", googleMapsApiKey)
        }
        return json
    }

    companion object {
        // Settings.System.DEFAULT_NOTIFICATION_URI, spelled out so plain JVM tests can load AppConfig.
        const val DEFAULT_DONE_SOUND = "content://settings/system/notification_sound"

        val DEFAULT_SLOT_TIMES = linkedMapOf(
            "wake" to "07:00", "breakfast" to "08:00", "lunch" to "12:30", "dinner" to "18:30", "bed" to "22:00"
        )

        /** "7:30" or "07:30" to a time; null when it isn't one. */
        fun parseSlotTime(text: String): java.time.LocalTime? = runCatching {
            val (h, m) = text.trim().split(":").map { it.toInt() }
            java.time.LocalTime.of(h, m)
        }.getOrNull()

        fun fromJson(json: JSONObject): AppConfig {
            val certAlias = if (json.has("client_cert_alias")) {
                val str = json.optString("client_cert_alias")
                if (str.isNotBlank()) str else null
            } else null
            val mtlsDefault = !certAlias.isNullOrBlank()

            return AppConfig(
                hubUrl = json.optString("hub_url", "https://health.example.com"),
                apiToken = json.optString("api_token", ""),
                enableMtls = json.optBoolean("enable_mtls", mtlsDefault),
                clientCertAlias = certAlias,
                autoUploadHub = json.optBoolean("auto_upload_hub", true),
                autoSyncHealthConnect = json.optBoolean("auto_sync_health_connect", true),
                forceYunmaiPounds = json.optBoolean("force_yunmai_pounds", true),
                preferredUnit = json.optString("preferred_unit", "lb"),
                enableYunmai = json.optBoolean("enable_yunmai", true),
                enableFitdays = json.optBoolean("enable_fitdays", true),
                enableVesync = json.optBoolean("enable_vesync", true),
                enableViatomBp2 = json.optBoolean("enable_viatom_bp2", true),
                enableOmron = json.optBoolean("enable_omron", true),
                userHeightCm = json.optDouble("user_height_cm", 175.0).toFloat(),
                userAge = json.optInt("user_age", 40),
                userSex = json.optString("user_sex", "male"),
                userIsAthlete = json.optBoolean("user_is_athlete", false),
                themeMode = json.optString("theme_mode", "system"),
                watchSyncMinutes = json.optInt("watch_sync_minutes", 60),
                supplementReminders = json.optBoolean("supplement_reminders", false),
                doneSound = json.optString("done_sound", DEFAULT_DONE_SOUND),
                slotTimes = DEFAULT_SLOT_TIMES.mapValues { (slot, def) ->
                    json.optJSONObject("slot_times")?.optString(slot)?.takeIf { parseSlotTime(it) != null } ?: def
                },
                googleMapsApiKey = json.optString("google_maps_api_key", "")
            )
        }
    }
}

class AppConfigManager(private val context: Context) {

    private val prefs = context.getSharedPreferences("uniscale_settings", Context.MODE_PRIVATE)

    private val _config = MutableStateFlow(loadConfig())
    val config = _config.asStateFlow()

    fun loadConfig(): AppConfig {
        // Priority 1: User preferences in SharedPreferences
        if (prefs.contains("config_json")) {
            try {
                val jsonStr = prefs.getString("config_json", null)
                if (!jsonStr.isNullOrBlank()) {
                    return AppConfig.fromJson(JSONObject(jsonStr))
                }
            } catch (e: Exception) {
                Log.w("AppConfig", "Error parsing saved config: ${e.message}")
            }
        }

        // Priority 2: External configuration file on SDCard / Downloads
        val extConfigFile = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "uniscale_config.json")
        if (extConfigFile.exists()) {
            try {
                val jsonStr = extConfigFile.readText()
                val parsed = AppConfig.fromJson(JSONObject(jsonStr))
                Log.i("AppConfig", "Loaded configuration from ${extConfigFile.absolutePath}")
                return parsed
            } catch (e: Exception) {
                Log.w("AppConfig", "Error reading external config: ${e.message}")
            }
        }

        // Priority 3: Bundled assets/uniscale_config.json
        try {
            context.assets.open("uniscale_config.json").use { stream ->
                val jsonStr = stream.bufferedReader().use { it.readText() }
                return AppConfig.fromJson(JSONObject(jsonStr))
            }
        } catch (e: Exception) {
            Log.w("AppConfig", "No assets config found: ${e.message}")
        }

        return AppConfig()
    }

    fun saveConfig(newConfig: AppConfig) {
        prefs.edit().putString("config_json", newConfig.toJson().toString()).apply()
        _config.value = newConfig
        Log.i("AppConfig", "Saved updated config to SharedPreferences")
    }

    fun exportConfigToFile(): File? {
        return try {
            val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            val file = File(downloadDir, "uniscale_config.json")
            // Downloads is shared storage: the API token stays in the app's private prefs.
            val json = _config.value.toJson().apply { remove("api_token") }
            file.writeText(json.toString(2))
            Log.i("AppConfig", "Exported configuration to ${file.absolutePath}")
            file
        } catch (e: Exception) {
            Log.e("AppConfig", "Failed exporting config: ${e.message}")
            null
        }
    }

    fun importConfigFromFile(): Result<AppConfig> {
        return try {
            val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            val file = File(downloadDir, "uniscale_config.json")
            if (!file.exists()) {
                Result.failure(FileNotFoundException("File uniscale_config.json not found in Downloads folder"))
            } else {
                val jsonStr = file.readText()
                val imported = AppConfig.fromJson(JSONObject(jsonStr))
                val parsed = imported.copy(apiToken = imported.apiToken.ifBlank { _config.value.apiToken })
                saveConfig(parsed)
                Result.success(parsed)
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
