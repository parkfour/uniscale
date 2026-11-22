package com.uniscale.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BrightnessAuto
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.CloudSync
import androidx.compose.material.icons.filled.DarkMode
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.FileDownload
import androidx.compose.material.icons.filled.FileUpload
import androidx.compose.material.icons.filled.LightMode
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.text.style.TextAlign
import com.uniscale.app.BuildConfig
import com.uniscale.app.config.AppConfig
import com.uniscale.app.config.AppConfigManager
import com.uniscale.app.sync.HubSyncManager
import com.uniscale.app.sync.WatchSyncWorker
import com.uniscale.app.sync.DoneSound
import androidx.compose.ui.platform.LocalContext
import kotlinx.coroutines.launch

@Composable
fun SettingsDialog(
    configManager: AppConfigManager,
    hubSyncManager: HubSyncManager,
    onRequestClientCertificate: ((String?) -> Unit) -> Unit = {},
    onPickSound: (current: String, onPicked: (String) -> Unit) -> Unit = { _, _ -> },
    onSyncWatch: () -> Unit = {},
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    val currentConfig by configManager.config.collectAsState()
    val scope = rememberCoroutineScope()

    var hubUrl by remember { mutableStateOf(currentConfig.hubUrl) }
    var apiToken by remember { mutableStateOf(currentConfig.apiToken) }
    var googleMapsApiKey by remember { mutableStateOf(currentConfig.googleMapsApiKey) }
    var enableMtls by remember { mutableStateOf(currentConfig.enableMtls) }
    var clientCertAlias by remember { mutableStateOf(currentConfig.clientCertAlias) }
    var themeMode by remember { mutableStateOf(currentConfig.themeMode) }
    var autoUploadHub by remember { mutableStateOf(currentConfig.autoUploadHub) }
    var watchSyncMinutes by remember { mutableStateOf(currentConfig.watchSyncMinutes) }
    var supplementReminders by remember { mutableStateOf(currentConfig.supplementReminders) }
    var doneSound by remember { mutableStateOf(currentConfig.doneSound) }
    var slotTimes by remember { mutableStateOf(currentConfig.slotTimes) }
    var forceYunmaiPounds by remember { mutableStateOf(currentConfig.forceYunmaiPounds) }
    var enableYunmai by remember { mutableStateOf(currentConfig.enableYunmai) }
    var enableFitdays by remember { mutableStateOf(currentConfig.enableFitdays) }
    var enableVesync by remember { mutableStateOf(currentConfig.enableVesync) }
    var enableViatomBp2 by remember { mutableStateOf(currentConfig.enableViatomBp2) }
    var enableOmron by remember { mutableStateOf(currentConfig.enableOmron) }

    var userHeightCm by remember { mutableStateOf(currentConfig.userHeightCm.toInt().toString()) }
    var userAge by remember { mutableStateOf(currentConfig.userAge.toString()) }
    var userSex by remember { mutableStateOf(currentConfig.userSex) }
    var userIsAthlete by remember { mutableStateOf(currentConfig.userIsAthlete) }

    var testStatus by remember { mutableStateOf<String?>(null) }
    var isTesting by remember { mutableStateOf(false) }
    var testSuccess by remember { mutableStateOf<Boolean?>(null) }
    var noticeMessage by remember { mutableStateOf<String?>(null) }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.94f)
                .fillMaxHeight(0.90f),
            shape = RoundedCornerShape(24.dp),
            color = MaterialTheme.colorScheme.surface,
            tonalElevation = 6.dp
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(20.dp)
            ) {
                // Header
                Text(
                    text = "UniScale Configuration",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "Configure upstream telemetry hub & hardware drivers",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline
                )

                Spacer(modifier = Modifier.height(16.dp))

                // Scrollable content
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    // Hub Server Settings
                    Text("Upstream Sync Hub", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)

                    OutlinedTextField(
                        value = hubUrl,
                        onValueChange = { hubUrl = it },
                        label = { Text("Hub URL") },
                        placeholder = { Text("https://health.example.com") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    OutlinedTextField(
                        value = apiToken,
                        onValueChange = { apiToken = it },
                        label = { Text("API Bearer Token (optional)") },
                        placeholder = { Text("your-token-here") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    OutlinedTextField(
                        value = googleMapsApiKey,
                        onValueChange = { googleMapsApiKey = it },
                        label = { Text("Google Maps API Key (Optional POI Fallback)") },
                        placeholder = { Text("AIzaSy...") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    // Mutual TLS (mTLS) Client Certificate
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f))
                    ) {
                        Column(modifier = Modifier.padding(14.dp)) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Icon(
                                        imageVector = Icons.Default.Lock,
                                        contentDescription = null,
                                        tint = if (enableMtls) Color(0xFF10B981) else MaterialTheme.colorScheme.onSurfaceVariant,
                                        modifier = Modifier.size(20.dp)
                                    )
                                    Spacer(modifier = Modifier.width(8.dp))
                                    Text("Mutual TLS (mTLS)", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                                }
                                Switch(
                                    checked = enableMtls,
                                    onCheckedChange = { enableMtls = it }
                                )
                            }
                            Spacer(modifier = Modifier.height(6.dp))
                            if (enableMtls) {
                                if (!clientCertAlias.isNullOrBlank()) {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        Badge(containerColor = Color(0xFF10B981).copy(alpha = 0.2f)) {
                                            Text("Active Cert", color = Color(0xFF10B981), fontWeight = FontWeight.Bold, fontSize = 11.sp)
                                        }
                                        Spacer(modifier = Modifier.width(8.dp))
                                        Text(
                                            clientCertAlias!!,
                                            fontSize = 12.sp,
                                            fontWeight = FontWeight.Medium,
                                            color = MaterialTheme.colorScheme.onSurface,
                                            maxLines = 1
                                        )
                                    }
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        OutlinedButton(
                                            onClick = {
                                                onRequestClientCertificate { selected ->
                                                    if (selected != null) {
                                                        clientCertAlias = selected
                                                    }
                                                }
                                            },
                                            modifier = Modifier.weight(1f)
                                        ) {
                                            Text("Change Cert", fontSize = 12.sp)
                                        }
                                        OutlinedButton(
                                            onClick = { clientCertAlias = null },
                                            modifier = Modifier.weight(1f)
                                        ) {
                                            Text("Clear", fontSize = 12.sp)
                                        }
                                    }
                                } else {
                                    Text(
                                        "Authenticate with self-hosted server using a client certificate installed in Android Keystore.",
                                        fontSize = 12.sp,
                                        color = MaterialTheme.colorScheme.outline
                                    )
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Button(
                                        onClick = {
                                            onRequestClientCertificate { selected ->
                                                if (selected != null) {
                                                    clientCertAlias = selected
                                                }
                                            }
                                        },
                                        modifier = Modifier.fillMaxWidth()
                                    ) {
                                        Icon(Icons.Default.Lock, contentDescription = null, modifier = Modifier.size(16.dp))
                                        Spacer(modifier = Modifier.width(8.dp))
                                        Text("Select Client Certificate", fontSize = 13.sp)
                                    }
                                }
                            } else {
                                Text(
                                    "Disabled. Standard HTTPS or bearer token authentication will be used.",
                                    fontSize = 12.sp,
                                    color = MaterialTheme.colorScheme.outline
                                )
                            }
                        }
                    }

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Auto Upload to Hub", fontWeight = FontWeight.Medium)
                            Text("Send measurements to self-hosted hub automatically", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = autoUploadHub,
                            onCheckedChange = { autoUploadHub = it }
                        )
                    }

                    // Background hub sync interval: a slider with fixed stops.
                    val stops = WatchSyncWorker.INTERVALS
                    val stopIndex = stops.indexOf(watchSyncMinutes).takeIf { it >= 0 } ?: stops.indexOf(60)
                    Column(modifier = Modifier.fillMaxWidth()) {
                        Text("Background Sync", fontWeight = FontWeight.Medium)
                        Text(
                            when {
                                watchSyncMinutes == 0 -> "Off: watch data uploads only when the app is open"
                                watchSyncMinutes < 60 -> "Every $watchSyncMinutes min, while the app is closed"
                                else -> "Every ${watchSyncMinutes / 60} h, while the app is closed"
                            },
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.outline
                        )
                        Slider(
                            value = stopIndex.toFloat(),
                            onValueChange = { watchSyncMinutes = stops[it.toInt().coerceIn(stops.indices)] },
                            valueRange = 0f..(stops.size - 1).toFloat(),
                            steps = stops.size - 2,
                            enabled = autoUploadHub
                        )
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            stops.forEach { m ->
                                Text(
                                    when { m == 0 -> "Off"; m < 60 -> "${m}m"; else -> "${m / 60}h" },
                                    fontSize = 11.sp,
                                    color = MaterialTheme.colorScheme.outline
                                )
                            }
                        }
                    }

                    // Supplement reminders: on/off plus the clock time of each slot.
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Measurement Done Sound", fontWeight = FontWeight.Medium)
                            Text(DoneSound.title(context, doneSound), fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        OutlinedButton(onClick = { onPickSound(doneSound) { doneSound = it } }) { Text("Choose") }
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Supplement Reminders", fontWeight = FontWeight.Medium)
                            Text("A notification at each slot time while doses are unticked", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(checked = supplementReminders, onCheckedChange = { supplementReminders = it })
                    }
                    if (supplementReminders) {
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            slotTimes.forEach { (slot, time) ->
                                OutlinedTextField(
                                    value = time,
                                    onValueChange = { slotTimes = slotTimes + (slot to it) },
                                    label = { Text(slot.replaceFirstChar { c -> c.uppercase() }, fontSize = 10.sp, maxLines = 1) },
                                    isError = AppConfig.parseSlotTime(time) == null,
                                    singleLine = true,
                                    textStyle = LocalTextStyle.current.copy(fontSize = 13.sp),
                                    modifier = Modifier.weight(1f)
                                )
                            }
                        }
                    }

                    // Test Hub Connection Button
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Button(
                            onClick = {
                                isTesting = true
                                testStatus = "Testing connection..."
                                testSuccess = null
                                scope.launch {
                                    val res = hubSyncManager.testConnection(hubUrl, apiToken, enableMtls, clientCertAlias)
                                    isTesting = false
                                    res.onSuccess { elapsed ->
                                        testSuccess = true
                                        testStatus = "Success! (${elapsed} ms latency)"
                                    }.onFailure { err ->
                                        testSuccess = false
                                        testStatus = "Failed: ${err.message}"
                                    }
                                }
                            },
                            enabled = !isTesting && hubUrl.isNotBlank()
                        ) {
                            if (isTesting) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    strokeWidth = 2.dp,
                                    color = MaterialTheme.colorScheme.onPrimary
                                )
                                Spacer(modifier = Modifier.width(8.dp))
                            }
                            Text("Test Hub Connection")
                        }
                    }

                    if (testStatus != null) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(
                                imageVector = if (testSuccess == true) Icons.Default.CheckCircle else Icons.Default.Error,
                                contentDescription = null,
                                tint = if (testSuccess == true) Color(0xFF10B981) else Color(0xFFEF4444),
                                modifier = Modifier.size(18.dp)
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(
                                text = testStatus!!,
                                fontSize = 13.sp,
                                color = if (testSuccess == true) Color(0xFF10B981) else Color(0xFFEF4444)
                            )
                        }
                    }

                    HorizontalDivider()

                    // Hardware Drivers Section
                    Text("Hardware BLE Drivers", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Yunmai Scales", fontWeight = FontWeight.Medium)
                            Text("Yunmai Mini 2, Color, Pro", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = enableYunmai,
                            onCheckedChange = { enableYunmai = it }
                        )
                    }

                    if (enableYunmai) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(start = 16.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text("Force LED Display to lb", fontSize = 14.sp)
                                Text("Sends unit command 0x02 on connection", fontSize = 11.sp, color = MaterialTheme.colorScheme.outline)
                            }
                            Switch(
                                checked = forceYunmaiPounds,
                                onCheckedChange = { forceYunmaiPounds = it }
                            )
                        }
                    }

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Fitdays / Leaone Scales", fontWeight = FontWeight.Medium)
                            Text("Broadcast protocol (0xFFB0 / 0xFFF0)", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = enableFitdays,
                            onCheckedChange = { enableFitdays = it }
                        )
                    }

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Etekcity / VeSync Scales", fontWeight = FontWeight.Medium)
                            Text("ESF / Scaly smart scales", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = enableVesync,
                            onCheckedChange = { enableVesync = it }
                        )
                    }

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Viatom BP2 (BP & ECG)", fontWeight = FontWeight.Medium)
                            Text("NIBP cuff and Lead-I ECG waveform stream", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = enableViatomBp2,
                            onCheckedChange = { enableViatomBp2 = it }
                        )
                    }

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Omron BP Monitors", fontWeight = FontWeight.Medium)
                            Text("Standard GATT Blood Pressure 0x1810", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = enableOmron,
                            onCheckedChange = { enableOmron = it }
                        )
                    }

                    HorizontalDivider()

                    // User Profile & BIA Suite Section
                    Text("User Profile & BIA Suite", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)
                    Text(
                        "Used to calculate Body Fat %, Lean Muscle, Total Body Water, Bone Mass, Visceral Fat, and BMR locally from scale impedance.",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.outline
                    )

                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        OutlinedTextField(
                            value = userHeightCm,
                            onValueChange = { userHeightCm = it },
                            label = { Text("Height (cm)") },
                            modifier = Modifier.weight(1f),
                            singleLine = true
                        )
                        OutlinedTextField(
                            value = userAge,
                            onValueChange = { userAge = it },
                            label = { Text("Age (years)") },
                            modifier = Modifier.weight(1f),
                            singleLine = true
                        )
                    }

                    // Sex selection
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("Biological Sex", fontWeight = FontWeight.Medium)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            val isMaleSelected = userSex.equals("male", ignoreCase = true)
                            FilterChip(
                                selected = isMaleSelected,
                                onClick = { userSex = "male" },
                                label = {
                                    Text(
                                        text = "Male",
                                        fontWeight = if (isMaleSelected) FontWeight.Bold else FontWeight.Normal,
                                        color = if (isMaleSelected) MaterialTheme.colorScheme.onPrimaryContainer else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                                    )
                                },
                                leadingIcon = if (isMaleSelected) {
                                    {
                                        Icon(
                                            imageVector = Icons.Default.Check,
                                            contentDescription = null,
                                            tint = MaterialTheme.colorScheme.primary,
                                            modifier = Modifier.size(16.dp)
                                        )
                                    }
                                } else null,
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.7f),
                                    containerColor = Color.Transparent
                                ),
                                border = FilterChipDefaults.filterChipBorder(
                                    enabled = true,
                                    selected = isMaleSelected,
                                    selectedBorderColor = MaterialTheme.colorScheme.primary,
                                    selectedBorderWidth = 2.dp,
                                    borderColor = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                                    borderWidth = 1.dp
                                )
                            )

                            val isFemaleSelected = userSex.equals("female", ignoreCase = true)
                            FilterChip(
                                selected = isFemaleSelected,
                                onClick = { userSex = "female" },
                                label = {
                                    Text(
                                        text = "Female",
                                        fontWeight = if (isFemaleSelected) FontWeight.Bold else FontWeight.Normal,
                                        color = if (isFemaleSelected) MaterialTheme.colorScheme.onPrimaryContainer else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                                    )
                                },
                                leadingIcon = if (isFemaleSelected) {
                                    {
                                        Icon(
                                            imageVector = Icons.Default.Check,
                                            contentDescription = null,
                                            tint = MaterialTheme.colorScheme.primary,
                                            modifier = Modifier.size(16.dp)
                                        )
                                    }
                                } else null,
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.7f),
                                    containerColor = Color.Transparent
                                ),
                                border = FilterChipDefaults.filterChipBorder(
                                    enabled = true,
                                    selected = isFemaleSelected,
                                    selectedBorderColor = MaterialTheme.colorScheme.primary,
                                    selectedBorderWidth = 2.dp,
                                    borderColor = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                                    borderWidth = 1.dp
                                )
                            )
                        }
                    }

                    // Athlete mode
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Athlete Mode", fontWeight = FontWeight.Medium)
                            Text("Calibrates BIA for athletic muscle density and hydration", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                        Switch(
                            checked = userIsAthlete,
                            onCheckedChange = { userIsAthlete = it }
                        )
                    }

                    HorizontalDivider()

                    // Display Theme (Light / Dark / System)
                    Text("Display Theme", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)
                    Text("Choose between system default, light, or dark mode", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        val themeOptions = listOf(
                            Triple("system", "System", Icons.Default.BrightnessAuto),
                            Triple("light", "Light", Icons.Default.LightMode),
                            Triple("dark", "Dark", Icons.Default.DarkMode)
                        )
                        themeOptions.forEach { (mode, label, icon) ->
                            val isSelected = themeMode.equals(mode, ignoreCase = true)
                            FilterChip(
                                selected = isSelected,
                                onClick = {
                                    themeMode = mode
                                    configManager.saveConfig(configManager.config.value.copy(themeMode = mode))
                                },
                                label = {
                                    Text(
                                        text = label,
                                        fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                        fontSize = 12.sp,
                                        color = if (isSelected) MaterialTheme.colorScheme.onPrimaryContainer else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                                    )
                                },
                                leadingIcon = {
                                    Icon(
                                        imageVector = icon,
                                        contentDescription = label,
                                        modifier = Modifier.size(16.dp),
                                        tint = if (isSelected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline.copy(alpha = 0.5f)
                                    )
                                },
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.7f),
                                    containerColor = Color.Transparent
                                ),
                                border = FilterChipDefaults.filterChipBorder(
                                    enabled = true,
                                    selected = isSelected,
                                    selectedBorderColor = MaterialTheme.colorScheme.primary,
                                    selectedBorderWidth = 2.dp,
                                    borderColor = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                                    borderWidth = 1.dp
                                ),
                                modifier = Modifier.weight(1f)
                            )
                        }
                    }

                    HorizontalDivider()

                    // Data actions (moved off the dashboard)
                    Text("Data", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)
                    Text("Watch: sync now from the Gadgetbridge export (picks the file the first time)", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = onSyncWatch, modifier = Modifier.weight(1f)) {
                            Text("Watch", fontSize = 13.sp)
                        }
                    }

                    HorizontalDivider()

                    // Configuration File Import / Export
                    Text("Configuration File", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, color = MaterialTheme.colorScheme.primary)
                    Text("Sync configuration with Downloads/uniscale_config.json", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        OutlinedButton(
                            onClick = {
                                val file = configManager.exportConfigToFile()
                                noticeMessage = if (file != null) "Exported to Downloads/${file.name}" else "Failed to export config"
                            },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.FileUpload, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Export", fontSize = 13.sp)
                        }

                        OutlinedButton(
                            onClick = {
                                val result = configManager.importConfigFromFile()
                                result.onSuccess { imported ->
                                    hubUrl = imported.hubUrl
                                    apiToken = imported.apiToken
                                    googleMapsApiKey = imported.googleMapsApiKey
                                    enableMtls = imported.enableMtls
                                    clientCertAlias = imported.clientCertAlias
                                    themeMode = imported.themeMode
                                    autoUploadHub = imported.autoUploadHub
                                    watchSyncMinutes = imported.watchSyncMinutes
                                    supplementReminders = imported.supplementReminders
                                    doneSound = imported.doneSound
                                    slotTimes = imported.slotTimes
                                    forceYunmaiPounds = imported.forceYunmaiPounds
                                    enableYunmai = imported.enableYunmai
                                    enableFitdays = imported.enableFitdays
                                    enableVesync = imported.enableVesync
                                    enableViatomBp2 = imported.enableViatomBp2
                                    enableOmron = imported.enableOmron
                                    userHeightCm = imported.userHeightCm.toInt().toString()
                                    userAge = imported.userAge.toString()
                                    userSex = imported.userSex
                                    userIsAthlete = imported.userIsAthlete
                                    noticeMessage = "Imported uniscale_config.json successfully!"
                                }.onFailure { err ->
                                    noticeMessage = "Import error: ${err.message}"
                                }
                            },
                            modifier = Modifier.weight(1f)
                        ) {
                            Icon(Icons.Default.FileDownload, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Import", fontSize = 13.sp)
                        }
                    }

                    if (noticeMessage != null) {
                        Text(
                            text = noticeMessage!!,
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.secondary,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }

                Spacer(modifier = Modifier.height(16.dp))

                Text(
                    text = "uniscale v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}) · ${BuildConfig.GIT_COMMIT}",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(modifier = Modifier.height(8.dp))

                // Action Buttons
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    TextButton(onClick = onDismiss) {
                        Text("Cancel")
                    }
                    Spacer(modifier = Modifier.width(8.dp))
                    Button(
                        onClick = {
                            val newConfig = currentConfig.copy(
                                hubUrl = hubUrl.trim(),
                                apiToken = apiToken.trim(),
                                googleMapsApiKey = googleMapsApiKey.trim(),
                                enableMtls = enableMtls,
                                clientCertAlias = if (enableMtls) clientCertAlias else null,
                                themeMode = themeMode,
                                autoUploadHub = autoUploadHub,
                                watchSyncMinutes = watchSyncMinutes,
                                supplementReminders = supplementReminders,
                                doneSound = doneSound,
                                // A time that doesn't parse keeps its previous value.
                                slotTimes = slotTimes.mapValues { (slot, t) ->
                                    AppConfig.parseSlotTime(t)?.let { "%02d:%02d".format(it.hour, it.minute) }
                                        ?: currentConfig.slotTimes.getValue(slot)
                                },
                                forceYunmaiPounds = forceYunmaiPounds,
                                enableYunmai = enableYunmai,
                                enableFitdays = enableFitdays,
                                enableVesync = enableVesync,
                                enableViatomBp2 = enableViatomBp2,
                                enableOmron = enableOmron,
                                userHeightCm = userHeightCm.toFloatOrNull() ?: 175f,
                                userAge = userAge.toIntOrNull() ?: 40,
                                userSex = userSex,
                                userIsAthlete = userIsAthlete
                            )
                            configManager.saveConfig(newConfig)
                            onDismiss()
                        }
                    ) {
                        Text("Save Changes")
                    }
                }
            }
        }
    }
}
