package com.uniscale.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.lifecycleScope
import com.uniscale.app.ble.BleManager
import com.uniscale.app.config.AppConfigManager
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.CounterState
import com.uniscale.app.model.DwellPlace
import com.uniscale.app.model.SleepData
import com.uniscale.app.model.WeightData
import com.uniscale.app.sync.GadgetbridgeExport
import com.uniscale.app.sync.DoneSound
import com.uniscale.app.sync.GarminSyncManager
import com.uniscale.app.sync.LocalReadings
import com.uniscale.app.sync.HubSyncManager
import com.uniscale.app.sync.PoiLookupService
import com.uniscale.app.sync.WatchSyncWorker
import com.uniscale.app.sync.GemmaChat
import com.uniscale.app.sync.Notifications
import com.uniscale.app.sync.SupplementProtocol
import com.uniscale.app.sync.SupplementReminderWorker
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import com.uniscale.app.ui.DashboardScreen
import com.uniscale.app.ui.theme.UniScaleTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private lateinit var configManager: AppConfigManager
    private lateinit var hubSyncManager: HubSyncManager
    private lateinit var bleManager: BleManager
    private lateinit var localReadings: LocalReadings
    private lateinit var garminSync: GarminSyncManager
    private lateinit var gadgetbridge: GadgetbridgeExport
    private lateinit var protocol: SupplementProtocol
    private val showChat = mutableStateOf(false)
    // Reminders and Dr. Gemma's replies are notifications; nothing else needs this permission.
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val historyWeights = mutableStateOf<List<WeightData>>(emptyList())
    private val historyBp = mutableStateOf<List<BloodPressureData>>(emptyList())
    private val historySleep = mutableStateOf<List<SleepData>>(emptyList())
    private val watchActivity = mutableStateOf<com.uniscale.app.model.WatchActivityData?>(null)
    private val counterState = mutableStateOf(CounterState())
    private val dwellPlaces = mutableStateOf<List<DwellPlace>>(emptyList())

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val allGranted = permissions.values.all { it }
        if (allGranted) {
            bleManager.startScan()
        }
    }

    // Gadgetbridge Settings → Auto export writes its database to a file the user picks; pick the same file here.
    // Android's ringtone picker; the result goes to whoever opened it (the Settings dialog).
    private var onSoundPicked: ((String) -> Unit)? = null
    private val soundPicker = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == RESULT_OK) onSoundPicked?.invoke(DoneSound.picked(result.data))
        onSoundPicked = null
    }

    private val gadgetbridgeFilePicker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            gadgetbridge.setExportFile(uri)
            syncWatch()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // The app scans for scales/BP cuffs whenever it's on screen, and Android stops delivering
        // unfiltered BLE scan results once the screen turns off, so keep it on while we're visible.
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        configManager = AppConfigManager(this)
        hubSyncManager = HubSyncManager(this, configManager)
        bleManager = BleManager(this, configManager)
        localReadings = LocalReadings(this)
        garminSync = GarminSyncManager(this)
        gadgetbridge = GadgetbridgeExport(this)
        protocol = SupplementProtocol(this, hubSyncManager)
        showChat.value = intent?.getBooleanExtra(Notifications.EXTRA_OPEN_CHAT, false) == true
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        // Wire auto-sync pipelines
        bleManager.onNewWeight = { weight ->
            DoneSound.play(this, configManager.config.value.doneSound, "weight")
            lifecycleScope.launch {
                localReadings.add(weight)
                if (configManager.config.value.autoUploadHub) {
                    hubSyncManager.uploadWeight(weight)
                }
                garminSync.uploadWeight(weight)
                loadHistory()
            }
        }

        bleManager.onNewBp = { bp ->
            DoneSound.play(this, configManager.config.value.doneSound, "bp")
            lifecycleScope.launch {
                localReadings.add(bp)
                if (configManager.config.value.autoUploadHub) {
                    hubSyncManager.uploadBloodPressure(bp)
                }
                garminSync.uploadBloodPressure(bp)
                loadHistory()
            }
        }

        bleManager.onNewEcg = { ecg ->
            DoneSound.play(this, configManager.config.value.doneSound, "ecg")
            lifecycleScope.launch {
                if (configManager.config.value.autoUploadHub) {
                    hubSyncManager.uploadEcg(ecg)
                }
            }
        }

        // (Re)schedule the background watch sync whenever its interval setting changes.
        lifecycleScope.launch {
            configManager.config.map { it.watchSyncMinutes }.distinctUntilChanged().collect {
                WatchSyncWorker.schedule(applicationContext, it)
            }
        }

        // Reschedule supplement reminders whenever they're switched on/off or a slot time changes.
        lifecycleScope.launch {
            configManager.config.map { it.supplementReminders to it.slotTimes }.distinctUntilChanged().collect {
                SupplementReminderWorker.schedule(applicationContext, configManager.config.value)
            }
        }

        checkAndRequestPermissions()
        loadHistory()

        setContent {
            val config by configManager.config.collectAsState()
            val doses by protocol.doses.collectAsState()
            val assistantName by GemmaChat.assistant(this@MainActivity).collectAsState()
            UniScaleTheme(themeMode = config.themeMode) {
                DashboardScreen(
                    bleManager = bleManager,
                    configManager = configManager,
                    hubSyncManager = hubSyncManager,
                    watchActivity = watchActivity.value,
                    latestSleep = historySleep.value.firstOrNull(),
                    historyWeights = historyWeights.value,
                    historyBp = historyBp.value,
                    onImportHistory = { loadHistory() },
                    onSyncWatch = { syncWatch() },
                    onRequestClientCertificate = { onSelected ->
                        selectClientCertificate(onSelected)
                    },
                    onPickSound = { current, onPicked ->
                        onSoundPicked = onPicked
                        soundPicker.launch(DoneSound.pickerIntent(current))
                    },
                    doses = doses,
                    onToggleDose = { dose, taken -> lifecycleScope.launch { protocol.setTaken(dose, taken) } },
                    assistantName = assistantName,
                    showChat = showChat.value,
                    onShowChat = { showChat.value = it },
                    counter = counterState.value,
                    dwellPlaces = dwellPlaces.value,
                    onAdjustCounter = { item, delta -> adjustCounter(item, delta) },
                    onAssignPoi = { place, poi -> assignPoi(place, poi) },
                    onDeletePlace = { place -> deletePlace(place) },
                    onLookupPois = { place -> lookupPoisForPlace(place) },
                    onRefreshPlaces = { loadCounterAndPlaces() },
                    onStartScan = { checkAndRequestPermissions(forceRestart = true) },
                    onStopScan = { bleManager.stopScan() }
                )
            }
        }
    }

    private fun selectClientCertificate(onSelected: (String?) -> Unit) {
        android.security.KeyChain.choosePrivateKeyAlias(
            this,
            { alias ->
                runOnUiThread {
                    onSelected(alias)
                }
            },
            arrayOf("RSA", "EC"),
            null,
            null,
            -1,
            null
        )
    }

    override fun onResume() {
        super.onResume()
        loadHistory()
        checkAndRequestPermissions(forceRestart = true)
    }

    private fun loadHistory() {
        lifecycleScope.launch { protocol.refresh() }
        lifecycleScope.launch { GemmaChat.refreshAssistant(this@MainActivity, hubSyncManager) }
        loadCounterAndPlaces()
        lifecycleScope.launch {
            try {
                // Readings taken on this phone (kept until the hub has them) plus the hub's history.
                val localWeights = localReadings.weights()
                val localBps = localReadings.bloodPressures()
                val watch = gadgetbridge.read()
                historyWeights.value = localWeights
                historyBp.value = localBps
                historySleep.value = watch?.nights.orEmpty()
                watchActivity.value = watch?.activity?.firstOrNull { it.date == java.time.LocalDate.now() }
                if (configManager.config.value.autoUploadHub) {
                    localWeights.forEach { w -> hubSyncManager.uploadWeight(w) }
                    localBps.forEach { b -> hubSyncManager.uploadBloodPressure(b) }
                    watch?.activity?.forEach { a -> hubSyncManager.uploadWatchActivity(a) }
                    watch?.nights?.forEach { s -> hubSyncManager.uploadSleepSession(s) }
                    watch?.workouts?.forEach { w -> hubSyncManager.uploadWorkout(w) }
                }
                historyWeights.value = mergeByTime(localWeights, hubSyncManager.fetchWeights()) { it.timestamp }
                historyBp.value = mergeByTime(localBps, hubSyncManager.fetchBloodPressures()) { it.timestamp }
            } catch (e: Exception) {
                android.util.Log.e("MainActivity", "Error loading history: ${e.message}")
            }
        }
    }

    /** Newest first, one per second (the hub's copy of a local reading has the same timestamp). */
    private fun <T> mergeByTime(local: List<T>, hub: List<T>, time: (T) -> java.time.Instant): List<T> =
        (local + hub).distinctBy { time(it).epochSecond }.sortedByDescending(time)

    private fun syncWatch() {
        if (gadgetbridge.exportUri == null) {
            gadgetbridgeFilePicker.launch(arrayOf("*/*"))
            return
        }
        lifecycleScope.launch {
            val data = gadgetbridge.read()
            if (data == null) {
                toast("Can't read the Gadgetbridge export. Pick its auto-export file again.")
                gadgetbridgeFilePicker.launch(arrayOf("*/*"))
            } else {
                toast("Gadgetbridge: ${data.nights.size} nights, ${data.workouts.size} workouts in the last 30 days")
                loadHistory()
            }
        }
    }

    private fun loadCounterAndPlaces() {
        lifecycleScope.launch {
            val c = hubSyncManager.fetchCounter()
            if (c != null) {
                counterState.value = c
            }
        }
        lifecycleScope.launch {
            val places = hubSyncManager.fetchPlaces(date = "all")
            // Name suggestions are looked up on request only (the POI button), not for every place on each open.
            if (places.isNotEmpty()) dwellPlaces.value = places
        }
    }

    private fun adjustCounter(item: String, delta: Int) {
        val cur = counterState.value
        val n = ((cur.counts[item] ?: 0) + delta).coerceAtLeast(0)
        counterState.value = cur.copy(counts = cur.counts + (item to n))  // shown at once; the hub's answer replaces it
        lifecycleScope.launch {
            val updated = hubSyncManager.adjustCounter(item, delta)
            if (updated != null) {
                counterState.value = updated
            }
        }
    }

    private fun assignPoi(place: DwellPlace, poi: String) {
        dwellPlaces.value = dwellPlaces.value.map {
            if (it.id == place.id) it.copy(name = poi) else it
        }
        lifecycleScope.launch {
            hubSyncManager.updatePlace(place.id, name = poi)
        }
    }

    private fun deletePlace(place: DwellPlace) {
        dwellPlaces.value = dwellPlaces.value.filter { it.id != place.id }
        lifecycleScope.launch {
            hubSyncManager.deletePlace(place.id)
        }
    }

    private fun lookupPoisForPlace(place: DwellPlace) {
        lifecycleScope.launch {
            val candidates = PoiLookupService.queryPointsOfInterest(
                place.avgLat,
                place.avgLon,
                place.confidenceRange,
                configManager.config.value.googleMapsApiKey
            )
            if (candidates.isNotEmpty()) {
                val candidateNames = candidates.map { it.name }
                dwellPlaces.value = dwellPlaces.value.map {
                    if (it.id == place.id) it.copy(candidatePois = candidateNames) else it
                }
                hubSyncManager.updatePlace(place.id, candidatePois = candidateNames)
            }
        }
    }

    private fun toast(text: String) =
        android.widget.Toast.makeText(this, text, android.widget.Toast.LENGTH_LONG).show()

    private fun checkAndRequestPermissions(forceRestart: Boolean = false) {
        val permissions = mutableListOf<String>()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            permissions.add(Manifest.permission.BLUETOOTH_SCAN)
            permissions.add(Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            permissions.add(Manifest.permission.ACCESS_FINE_LOCATION)
        }

        val needed = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (needed.isNotEmpty()) {
            permissionLauncher.launch(needed.toTypedArray())
        } else {
            bleManager.startScan(force = forceRestart)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(Notifications.EXTRA_OPEN_CHAT, false)) showChat.value = true
    }


    override fun onDestroy() {
        super.onDestroy()
        bleManager.stopScan()
    }
}
