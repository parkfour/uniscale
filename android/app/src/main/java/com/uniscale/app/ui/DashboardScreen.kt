package com.uniscale.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.filled.Bedtime
import androidx.compose.material.icons.filled.Bluetooth
import androidx.compose.material.icons.filled.DirectionsRun
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.MonitorWeight
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.uniscale.app.ble.BleManager
import com.uniscale.app.config.AppConfigManager
import com.uniscale.app.model.CounterState
import com.uniscale.app.model.DiscoveredDevice
import com.uniscale.app.model.DwellPlace
import com.uniscale.app.model.WatchActivityData
import com.uniscale.app.sync.Dose
import com.uniscale.app.sync.HubSyncManager

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    bleManager: BleManager,
    configManager: AppConfigManager,
    hubSyncManager: HubSyncManager,
    watchActivity: WatchActivityData? = null,
    latestSleep: com.uniscale.app.model.SleepData? = null,
    historyWeights: List<com.uniscale.app.model.WeightData> = emptyList(),
    historyBp: List<com.uniscale.app.model.BloodPressureData> = emptyList(),
    onImportHistory: () -> Unit = {},
    onSyncWatch: () -> Unit = {},
    onRequestClientCertificate: ((String?) -> Unit) -> Unit = {},
    onPickSound: (current: String, onPicked: (String) -> Unit) -> Unit = { _, _ -> },
    doses: List<Dose> = emptyList(),
    onToggleDose: (Dose, Boolean) -> Unit = { _, _ -> },
    assistantName: String? = null,
    showChat: Boolean = false,
    onShowChat: (Boolean) -> Unit = {},
    counter: CounterState = CounterState(),
    dwellPlaces: List<DwellPlace> = emptyList(),
    onAdjustCounter: (String, Int) -> Unit = { _, _ -> },
    onAssignPoi: (DwellPlace, String) -> Unit = { _, _ -> },
    onDeletePlace: (DwellPlace) -> Unit = {},
    onLookupPois: (DwellPlace) -> Unit = {},
    onRefreshPlaces: () -> Unit = {},
    onStartScan: () -> Unit,
    onStopScan: () -> Unit
) {
    val weight by bleManager.latestWeight.collectAsState()
    val bp by bleManager.latestBp.collectAsState()
    val ecg by bleManager.latestEcg.collectAsState()
    val isScanning by bleManager.isScanning.collectAsState()
    val statusText by bleManager.statusText.collectAsState()
    val devices by bleManager.discoveredDevices.collectAsState()

    var showSettings by remember { mutableStateOf(false) }

    val config by configManager.config.collectAsState()
    var expandedTop by remember { mutableStateOf<Tile?>(null) }
    var expandedMid by remember { mutableStateOf<Tile?>(null) }
    var expandedBottom by remember { mutableStateOf<Tile?>(null) }
    var ecgExpanded by remember { mutableStateOf(false) }
    var showProtocol by remember { mutableStateOf(false) }
    val shownWeight = weight ?: historyWeights.maxByOrNull { it.timestamp }
    val shownBp = bp ?: historyBp.maxByOrNull { it.timestamp }
    // The reading before the shown one. A minute's gap skips the live reading's own stored copy.
    val prevWeight = shownWeight?.let { s -> historyWeights.filter { it.timestamp < s.timestamp.minusSeconds(60) }.maxByOrNull { it.timestamp } }
    val prevBp = shownBp?.let { s -> historyBp.filter { it.timestamp < s.timestamp.minusSeconds(60) }.maxByOrNull { it.timestamp } }
    val bpMeasuring = statusText.takeIf { it.startsWith("Measuring BP") }

    // A measurement in progress expands its tile, which stays open until tapped. Within a minute of
    // auto-expanding, further live frames don't reopen a tile the user just closed.
    var lastAutoExpand by remember { mutableStateOf(emptyMap<Tile, Long>()) }
    fun autoExpand(tile: Tile) {
        val now = System.currentTimeMillis()
        if (now - (lastAutoExpand[tile] ?: 0L) < AUTO_EXPAND_QUIET_MS) return
        lastAutoExpand = lastAutoExpand + (tile to now)
        when (tile) {
            Tile.WEIGHT, Tile.BP -> expandedMid = tile
            Tile.ECG -> ecgExpanded = true
            else -> Unit
        }
    }
    LaunchedEffect(weight) { if (weight != null) autoExpand(Tile.WEIGHT) }
    LaunchedEffect(bp, bpMeasuring != null) { if (bp != null || bpMeasuring != null) autoExpand(Tile.BP) }
    LaunchedEffect(ecg) { if (ecg != null) autoExpand(Tile.ECG) }

    if (showProtocol) {
        ProtocolScreen(doses = doses, onToggle = onToggleDose, onDone = { showProtocol = false })
        return
    }

    if (showChat && assistantName != null) {
        GemmaChatDialog(assistantName = assistantName, onDismiss = { onShowChat(false) })
        return
    }

    if (showSettings) {
        SettingsDialog(
            configManager = configManager,
            hubSyncManager = hubSyncManager,
            onRequestClientCertificate = onRequestClientCertificate,
            onPickSound = onPickSound,
            onSyncWatch = onSyncWatch,
            onDismiss = { showSettings = false }
        )
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("uniscale", fontWeight = FontWeight.Bold, fontSize = 24.sp)
                        Spacer(modifier = Modifier.width(8.dp))
                        Badge(containerColor = MaterialTheme.colorScheme.primaryContainer) {
                            Text("Health Hub", color = MaterialTheme.colorScheme.onPrimaryContainer)
                        }
                    }
                },
                actions = {
                    IconButton(onClick = { if (isScanning) onStopScan() else onStartScan() }) {
                        Icon(
                            imageVector = if (isScanning) Icons.Default.Refresh else Icons.Default.Bluetooth,
                            contentDescription = "Scan Toggle",
                            tint = if (isScanning) MaterialTheme.colorScheme.primary else Color.Gray
                        )
                    }
                    // Only when the hub has a chat assistant (GET /api/assistant).
                    if (assistantName != null) {
                        IconButton(onClick = { onShowChat(true) }) {
                            Icon(
                                imageVector = Icons.AutoMirrored.Filled.Chat,
                                contentDescription = "Ask $assistantName",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                    IconButton(onClick = { showSettings = true }) {
                        Icon(
                            imageVector = Icons.Default.Settings,
                            contentDescription = "Settings",
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            )
        }
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // Status Banner
            item {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
                ) {
                    Row(
                        modifier = Modifier.padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Box(
                            modifier = Modifier
                                .size(12.dp)
                                .background(if (isScanning) Color(0xFF10B981) else Color(0xFF64748B), CircleShape)
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Text(statusText, fontSize = 14.sp, fontWeight = FontWeight.Medium)
                    }
                }
            }

            // Activity | Sleep
            item {
                TileRow(
                    Tile.WATCH, Tile.SLEEP, expandedTop, { expandedTop = if (expandedTop == it) null else it },
                    half = { t, m -> if (t == Tile.WATCH) WatchHalf(watchActivity, m) else SleepHalf(latestSleep, m) },
                    full = { t, m -> if (t == Tile.WATCH) WatchFull(watchActivity, m) else SleepFull(latestSleep, m) }
                )
            }

            // Weight | Blood pressure: the live reading, else the latest one on the phone
            item {
                TileRow(
                    Tile.WEIGHT, Tile.BP, expandedMid, { expandedMid = if (expandedMid == it) null else it },
                    half = { t, m ->
                        if (t == Tile.WEIGHT) WeightHalf(shownWeight, config.preferredUnit, m) else BpHalf(shownBp, bpMeasuring, m)
                    },
                    full = { t, m -> if (t == Tile.WEIGHT) WeightFull(shownWeight, m, prevWeight) else BpFull(shownBp, m, prevBp) }
                )
            }

            // ECG | Protocol (the protocol opens as its own page: it can be longer than the screen)
            item {
                if (ecgExpanded) {
                    EcgFull(ecg, tall = true, modifier = Modifier.clickable { ecgExpanded = false })
                } else {
                    Row(
                        modifier = Modifier.fillMaxWidth().height(IntrinsicSize.Min),
                        horizontalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        EcgHalf(ecg, Modifier.weight(1f).fillMaxHeight().clickable { ecgExpanded = true })
                        ProtocolHalf(doses, Modifier.weight(1f).fillMaxHeight().clickable { showProtocol = true })
                    }
                }
            }

            // D Tracker | Dwell Places (>1 hr)
            item {
                TileRow(
                    Tile.COUNTER, Tile.PLACES, expandedBottom, { expandedBottom = if (expandedBottom == it) null else it },
                    half = { t, m ->
                        if (t == Tile.COUNTER) CounterHalf(counter, m) else PlacesHalf(dwellPlaces, m)
                    },
                    full = { t, m ->
                        if (t == Tile.COUNTER) {
                            CounterFull(
                                counter = counter,
                                onIncDOut = { onAdjustCounter("d_out", 1) },
                                onDecDOut = { onAdjustCounter("d_out", -1) },
                                onIncDIn = { onAdjustCounter("d_in", 1) },
                                onDecDIn = { onAdjustCounter("d_in", -1) },
                                modifier = m
                            )
                        } else {
                            PlacesFull(
                                places = dwellPlaces,
                                onAddPoi = onAssignPoi,
                                onDeletePlace = onDeletePlace,
                                onLookupPois = onLookupPois,
                                onRefresh = onRefreshPlaces,
                                modifier = m
                            )
                        }
                    }
                )
            }

            // Imported History Header
            item {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("Imported History", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
                    TextButton(onClick = onImportHistory) {
                        Text("Sync All")
                    }
                }
            }

            if (historyWeights.isEmpty() && historyBp.isEmpty()) {
                item {
                    Text(
                        "No readings yet: step on the scale or take a BP reading. Past readings come from the hub when it's reachable.",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.outline
                    )
                }
            } else {
                items(historyWeights.take(15)) { w ->
                    val dateStr = java.time.format.DateTimeFormatter.ofPattern("MMM dd, yyyy HH:mm")
                        .withZone(java.time.ZoneId.systemDefault())
                        .format(w.timestamp)
                    ListItem(
                        headlineContent = { Text("${w.weightLb} lb (${w.weightKg} kg)", fontWeight = FontWeight.Medium) },
                        supportingContent = { Text("${w.deviceName} • $dateStr") },
                        leadingContent = { Icon(Icons.Default.MonitorWeight, contentDescription = null, tint = MaterialTheme.colorScheme.primary) }
                    )
                }
                items(historyBp.take(15)) { b ->
                    val dateStr = java.time.format.DateTimeFormatter.ofPattern("MMM dd, yyyy HH:mm")
                        .withZone(java.time.ZoneId.systemDefault())
                        .format(b.timestamp)
                    ListItem(
                        headlineContent = { Text("${b.systolic}/${b.diastolic} mmHg (P: ${b.pulse})", fontWeight = FontWeight.Medium) },
                        supportingContent = { Text("${b.deviceName} • $dateStr") },
                        leadingContent = { Icon(Icons.Default.Speed, contentDescription = null, tint = Color(0xFFEF4444)) }
                    )
                }
            }

            // Discovered Devices Header
            item {
                Text("Discovered Devices", fontWeight = FontWeight.SemiBold, fontSize = 18.sp, modifier = Modifier.padding(top = 8.dp))
            }

            if (devices.isEmpty()) {
                item {
                    Text(
                        "No health devices actively transmitting. Step on your scale or turn on your cuff.",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.outline
                    )
                }
            } else {
                items(devices) { dev ->
                    ListItem(
                        headlineContent = { Text(dev.name, fontWeight = FontWeight.Medium) },
                        supportingContent = { Text("${dev.category.name} • ${dev.address}") },
                        trailingContent = { Text("${dev.rssi} dBm", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline) }
                    )
                }
            }

            item {
                Spacer(modifier = Modifier.height(30.dp))
            }
        }
    }
}

@Composable
fun BiaStatBadge(label: String, value: String, color: Color) {
    Surface(
        shape = RoundedCornerShape(8.dp),
        color = color.copy(alpha = 0.12f)
    ) {
        Column(
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 6.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(value, fontWeight = FontWeight.Bold, fontSize = 13.sp, color = color)
            Text(label, fontSize = 10.sp, color = MaterialTheme.colorScheme.outline)
        }
    }
}

private const val AUTO_EXPAND_QUIET_MS = 60_000L
