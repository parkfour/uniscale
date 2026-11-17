package com.uniscale.app.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import android.util.Log
import com.uniscale.app.config.AppConfigManager
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.DeviceCategory
import com.uniscale.app.model.DiscoveredDevice
import com.uniscale.app.model.EcgData
import com.uniscale.app.model.WeightData
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID

@SuppressLint("MissingPermission")
class BleManager(
    private val context: Context,
    private val configManager: AppConfigManager? = null
) {

    private val bluetoothAdapter: BluetoothAdapter? = BluetoothAdapter.getDefaultAdapter()
    private val scope = CoroutineScope(Dispatchers.IO)

    private val _discoveredDevices = MutableStateFlow<List<DiscoveredDevice>>(emptyList())
    val discoveredDevices = _discoveredDevices.asStateFlow()

    private val _latestWeight = MutableStateFlow<WeightData?>(null)
    val latestWeight = _latestWeight.asStateFlow()

    private val _latestBp = MutableStateFlow<BloodPressureData?>(null)
    val latestBp = _latestBp.asStateFlow()

    private val _latestEcg = MutableStateFlow<EcgData?>(null)
    val latestEcg = _latestEcg.asStateFlow()

    private val _isScanning = MutableStateFlow(false)
    val isScanning = _isScanning.asStateFlow()

    private val _statusText = MutableStateFlow("Ready")
    val statusText = _statusText.asStateFlow()

    var onNewWeight: ((WeightData) -> Unit)? = null
    var onNewBp: ((BloodPressureData) -> Unit)? = null
    var onNewEcg: ((EcgData) -> Unit)? = null

    private var activeGatt: BluetoothGatt? = null
    private val cccdUuid = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    private val ecgSampleBuffer = mutableListOf<Float>()
    private val lepuReassembler = LepuPacketReassembler()
    private var bpFileSize: Int = 0
    private val bpFileBuffer = mutableListOf<Byte>()
    private var lepuSeq: Int = 1
    private val fileQueue = ArrayDeque<String>()
    private var currentDownloadFile: String? = null
    private var syncTimeoutJob: Job? = null
    private var bp2RtJob: Job? = null
    private var lastBp2State: Int = -1
    private val ingestedBpFiles = mutableSetOf<String>()
    private var lastIngestedBp: BloodPressureData? = null

    private fun dispatchBp(bp: BloodPressureData) {
        val prev = lastIngestedBp
        if (prev != null &&
            prev.systolic == bp.systolic &&
            prev.diastolic == bp.diastolic &&
            prev.pulse == bp.pulse &&
            Math.abs(prev.timestamp.toEpochMilli() - bp.timestamp.toEpochMilli()) < 120_000L
        ) {
            Log.i("BleManager", "Deduplicated identical BP: ${bp.systolic}/${bp.diastolic} pulse ${bp.pulse}")
            _latestBp.value = bp
            return
        }
        lastIngestedBp = bp
        _latestBp.value = bp
        _statusText.value = "BP: ${bp.systolic}/${bp.diastolic} mmHg (HR: ${bp.pulse})"
        Log.i("BleManager", ">>> INGESTED BP: ${bp.systolic}/${bp.diastolic} pulse ${bp.pulse}")
        onNewBp?.invoke(bp)
    }

    private fun nextLepuSeq(): Int {
        val s = lepuSeq
        lepuSeq = if (lepuSeq >= 255) 1 else lepuSeq + 1
        return s
    }

    fun startScan(force: Boolean = false) {
        if (!force && _isScanning.value) {
            Log.d("BleManager", "startScan: Scan already in progress")
            return
        }

        if (bluetoothAdapter == null || !bluetoothAdapter.isEnabled) {
            _statusText.value = "Bluetooth is disabled"
            Log.w("BleManager", "startScan: Bluetooth adapter is null or disabled")
            return
        }

        val scanner = bluetoothAdapter.bluetoothLeScanner
        if (scanner == null) {
            _statusText.value = "BLE Scanner unavailable"
            Log.e("BleManager", "startScan: bluetoothLeScanner is null")
            return
        }

        try {
            scanner.stopScan(scanCallback)
        } catch (e: Exception) {
            Log.w("BleManager", "startScan cleanup stopScan: ${e.message}")
        }

        _isScanning.value = true
        _statusText.value = "Scanning for scales and monitors..."
        Log.i("BleManager", "startScan: Starting low-latency BLE scan (force=$force)...")

        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setReportDelay(0)
            .build()

        try {
            scanner.startScan(emptyList(), settings, scanCallback)
        } catch (e: Exception) {
            Log.e("BleManager", "startScan exception: ${e.message}")
            _statusText.value = "Scan error: ${e.message}"
            _isScanning.value = false
        }
    }

    fun restartScan() {
        startScan(force = true)
    }

    fun stopScan() {
        try {
            val scanner = bluetoothAdapter?.bluetoothLeScanner
            scanner?.stopScan(scanCallback)
        } catch (e: Exception) {
            Log.w("BleManager", "stopScan exception: ${e.message}")
        }
        _isScanning.value = false
        _statusText.value = "Scan stopped"
        Log.i("BleManager", "stopScan: Scan stopped")
    }

    // Debounce & settlement tracking for scale broadcast streams
    private var lastUploadedWeight: Float? = null
    private var lastUploadedWeightTime: Long = 0L
    private var lastSettlingWeightTime: Long = 0L
    private var consecutiveSameWeightCount = 0
    private var lastSettlingWeight: Float? = null

    fun isBroadcastOnlyDevice(name: String, address: String, hasAaaxFrame: Boolean = false): Boolean {
        if (hasAaaxFrame) return true
        val n = name.lowercase()
        return n.startsWith("aaa") ||
               n.contains("posture")
    }

    private fun handleAaaxWeight(frame: AaaxFrame.Weight, name: String, address: String) {
        val now = System.currentTimeMillis()
        if (frame.weightKg <= 0.5f) {
            _statusText.value = "Scale ready (0.0 lb). Step on scale..."
            Log.d("BleManager", ">>> AAAx Scale Ready (0.0 kg)")
            return
        }

        val weightLb = Math.round(frame.weightKg * 2.20462262f * 10f) / 10f

        // Track steady weight fallback: if weight is constant within 0.1 kg across 4+ frames over >= 1.2s, treat as settled
        if (lastSettlingWeight != null && Math.abs(frame.weightKg - lastSettlingWeight!!) < 0.1f) {
            consecutiveSameWeightCount++
        } else {
            consecutiveSameWeightCount = 1
            lastSettlingWeight = frame.weightKg
            lastSettlingWeightTime = now
        }
        val isSettledFallback = consecutiveSameWeightCount >= 4 && (now - lastSettlingWeightTime) >= 1200L

        if (frame.isFinal || isSettledFallback) {
            val weight = WeightData(
                weightKg = frame.weightKg,
                deviceName = name.ifEmpty { "Fitdays Scale" },
                deviceAddress = address
            )
            handleFinalWeight(weight)
        } else {
            // Live intermediate measuring
            _latestWeight.value = WeightData(
                weightKg = frame.weightKg,
                deviceName = name.ifEmpty { "Fitdays Scale" },
                deviceAddress = address
            )
            _statusText.value = "Measuring: $weightLb lb (${frame.weightKg} kg)..."
            Log.d("BleManager", ">>> AAAx Measuring: $weightLb lb (${frame.weightKg} kg)")
        }
    }

    private fun enrichWithBia(weight: WeightData): WeightData {
        val imp = weight.impedance ?: return weight
        if (imp <= 0) return weight
        val cfg = configManager?.config?.value ?: return weight
        val isMale = cfg.userSex.equals("male", ignoreCase = true)
        val comp = com.uniscale.app.model.BiaCalculator.calculate(
            weightKg = weight.weightKg,
            impedance = imp,
            heightCm = cfg.userHeightCm,
            age = cfg.userAge,
            isMale = isMale,
            isAthlete = cfg.userIsAthlete
        )
        return weight.copy(
            bodyFatPercent = comp.bodyFatPercent,
            bmi = comp.bmi,
            muscleMassLb = comp.muscleMassLb,
            waterPercent = comp.waterPercent,
            boneMassLb = comp.boneMassLb,
            visceralFat = comp.visceralFat,
            bmrKcal = comp.bmrKcal
        )
    }

    private fun handleFinalWeight(weight: WeightData) {
        val now = System.currentTimeMillis()
        val isDuplicate = lastUploadedWeight != null &&
                Math.abs(weight.weightKg - lastUploadedWeight!!) < 0.2f &&
                (now - lastUploadedWeightTime) < 15_000L

        val enriched = enrichWithBia(weight)
        _latestWeight.value = enriched
        val impText = if (enriched.impedance != null && enriched.impedance > 0) " (Imp: ${enriched.impedance}Ω)" else ""
        val fatText = if (enriched.bodyFatPercent != null) " (Fat: ${enriched.bodyFatPercent}%)" else ""
        _statusText.value = "Weight: ${enriched.weightLb} lb (${enriched.weightKg} kg)$fatText$impText"
        Log.i("BleManager", ">>> INGESTED FINAL WEIGHT: ${enriched.weightLb} lb (${enriched.weightKg} kg)$fatText$impText (dup=$isDuplicate)")

        if (!isDuplicate) {
            lastUploadedWeight = enriched.weightKg
            lastUploadedWeightTime = now
            onNewWeight?.invoke(enriched)
        }
    }

    private fun handleImpedance(impedance: Int, name: String, address: String) {
        val currentWeight = _latestWeight.value
        val now = System.currentTimeMillis()
        if (currentWeight != null && (now - lastUploadedWeightTime) < 20_000L && currentWeight.impedance == null) {
            val updated = enrichWithBia(currentWeight.copy(impedance = impedance))
            _latestWeight.value = updated
            val fatText = if (updated.bodyFatPercent != null) " (Fat: ${updated.bodyFatPercent}%)" else ""
            _statusText.value = "Weight: ${updated.weightLb} lb (${updated.weightKg} kg)$fatText (Imp: ${impedance}Ω)"
            Log.i("BleManager", ">>> ATTACHED IMPEDANCE & BIA: ${impedance}Ω, Fat=${updated.bodyFatPercent}%, Muscle=${updated.muscleMassLb}lb")
            onNewWeight?.invoke(updated)
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device = result.device
            val scanRecordName = result.scanRecord?.deviceName
            val name = if (!scanRecordName.isNullOrBlank()) scanRecordName else (device.name ?: "")
            val address = device.address
            val uuids = result.scanRecord?.serviceUuids?.map { it.uuid } ?: emptyList()

            val category = classifyDevice(name, uuids, address)
            if (category != DeviceCategory.UNKNOWN) {
                val current = _discoveredDevices.value.toMutableList()
                val index = current.indexOfFirst { it.address == address }
                val item = DiscoveredDevice(name = name.ifEmpty { category.name }, address = address, category = category, rssi = result.rssi)
                if (index >= 0) {
                    current[index] = item
                } else {
                    current.add(item)
                    Log.i("BleManager", ">>> DISCOVERED TARGET: $name ($category) at $address")
                }
                _discoveredDevices.value = current

                // Log raw scan record for target devices
                val rawBytes = result.scanRecord?.bytes
                Log.i("BleManager", ">>> TARGET ADV RECORD [$address] ($name): ${rawBytes?.joinToString(" ") { "%02X".format(it) }}")

                // 1. AAAx Broadcast Scale (Fitdays / Icomon AAA002, AAA007, AAA013, AAA07A, etc.)
                val aaaxFrame = DeviceDecoders.parseAaaxFromScanRecord(result.scanRecord, address)
                if (aaaxFrame != null) {
                    when (aaaxFrame) {
                        is AaaxFrame.Weight -> {
                            handleAaaxWeight(aaaxFrame, name.ifEmpty { "Fitdays Scale" }, address)
                        }
                        is AaaxFrame.Impedance -> {
                            handleImpedance(aaaxFrame.impedance, name.ifEmpty { "Fitdays Scale" }, address)
                        }
                    }
                } else if (category == DeviceCategory.FITDAYS_SCALE) {
                    // Standard / legacy Fitdays broadcast packets (e.g. 0xCF, 0xCB)
                    DeviceDecoders.parseFitdaysFromScanRecord(result.scanRecord, name, address)?.let { weight ->
                        handleFinalWeight(weight)
                    }
                }

                // Parse broadcast manufacturer data if scale is advertising weight live
                result.scanRecord?.manufacturerSpecificData?.let { mfgData ->
                    for (i in 0 until mfgData.size()) {
                        val data = mfgData.valueAt(i)
                        when (category) {
                            DeviceCategory.YUNMAI_SCALE -> {
                                DeviceDecoders.parseYunmai(data, name, address)?.let { weight ->
                                    handleFinalWeight(weight)
                                }
                            }
                            DeviceCategory.ETEKCITY_SCALE -> {
                                DeviceDecoders.parseEtekcity(data, name, address)?.let { weight ->
                                    handleFinalWeight(weight)
                                }
                            }
                            else -> Unit
                        }
                    }
                }

                // Auto-connect if not currently connected (NEVER connect to broadcast-only scales)
                val isBroadcastOnly = isBroadcastOnlyDevice(name, address, aaaxFrame != null)
                if (!isBroadcastOnly && activeGatt == null && !isConnecting) {
                    connectDevice(device, category)
                }
            }
        }

        override fun onScanFailed(errorCode: Int) {
            Log.w("BleManager", "onScanFailed: error code $errorCode")
            if (errorCode == ScanCallback.SCAN_FAILED_ALREADY_STARTED) {
                _isScanning.value = true
                _statusText.value = "Scanning for scales and monitors..."
                return
            }
            _statusText.value = "Scan failed ($errorCode)"
            _isScanning.value = false
        }
    }

    private fun classifyDevice(name: String, uuids: List<UUID>, address: String): DeviceCategory {
        val n = name.lowercase()
        val cfg = configManager?.config?.value
        val yunmaiEnabled = cfg?.enableYunmai ?: true
        val fitdaysEnabled = cfg?.enableFitdays ?: true
        val vesyncEnabled = cfg?.enableVesync ?: true
        val viatomEnabled = cfg?.enableViatomBp2 ?: true
        val omronEnabled = cfg?.enableOmron ?: true

        return when {
            fitdaysEnabled && (n.contains("pcb") || n.contains("aaa") || n.contains("chipsea") || n.contains("icomon") || n.contains("leaone") || uuids.any { it == DeviceDecoders.FITDAYS_SERVICE }) -> DeviceCategory.FITDAYS_SCALE
            yunmaiEnabled && (n.contains("yunmai") || uuids.any { it == DeviceDecoders.YUNMAI_SERVICE }) -> DeviceCategory.YUNMAI_SCALE
            vesyncEnabled && (n.contains("esf") || n.contains("scaly") || n.contains("etekcity") || uuids.any { it == DeviceDecoders.ETEKCITY_SERVICE }) -> DeviceCategory.ETEKCITY_SCALE
            viatomEnabled && (n.contains("bp2") || n.contains("viatom") || n.contains("wellue") || uuids.any { it == DeviceDecoders.VIATOM_SERVICE }) -> DeviceCategory.VIATOM_BP2
            omronEnabled && (n.contains("omron") || uuids.any { it == DeviceDecoders.OMRON_SERVICE }) -> DeviceCategory.OMRON_BP
            else -> DeviceCategory.UNKNOWN
        }
    }

    @Volatile private var isConnecting = false

    private fun connectDevice(device: BluetoothDevice, category: DeviceCategory) {
        synchronized(this) {
            if (activeGatt != null || isConnecting) {
                Log.w("BleManager", "connectDevice: already active or connecting. Ignoring ${device.address}")
                return
            }
            isConnecting = true
        }

        // Pause scanner during connectGatt to prevent radio collision (status 147)
        try {
            bluetoothAdapter?.bluetoothLeScanner?.stopScan(scanCallback)
        } catch (e: Exception) {}
        _isScanning.value = false

        val devDisplayName = device.name ?: category.name
        _statusText.value = "Connecting to $devDisplayName..."
        Log.i("BleManager", "connectDevice: Connecting to $devDisplayName [${device.address}] using TRANSPORT_LE")

        val callback = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
                Log.i("BleManager", "onConnectionStateChange: status=$status, newState=$newState")
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    Log.w("BleManager", "GATT connection error: status=$status. Resetting activeGatt.")
                    isConnecting = false
                    syncTimeoutJob?.cancel()
                    try { gatt.close() } catch (e: Exception) {}
                    activeGatt = null
                    lepuReassembler.reset()
                    bpFileBuffer.clear()
                    bpFileSize = 0
                    fileQueue.clear()
                    currentDownloadFile = null
                    _statusText.value = "Scanning for scales and monitors..."
                    startScan(force = true)
                    return
                }

                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    isConnecting = false
                    if (category == DeviceCategory.VIATOM_BP2) {
                        _statusText.value = "Connected to $devDisplayName. Requesting MTU..."
                        Log.i("BleManager", "Connected BP2. Requesting MTU 512...")
                        lepuReassembler.reset()
                        bpFileBuffer.clear()
                        bpFileSize = 0
                        lepuSeq = 1
                        fileQueue.clear()
                        currentDownloadFile = null
                        val requested = gatt.requestMtu(512)
                        Log.i("BleManager", "requestMtu(512) returned: $requested")
                        if (!requested) {
                            gatt.discoverServices()
                        }
                    } else {
                        // SCALES and OMRON: Skip MTU exchange! Discover services immediately!
                        _statusText.value = "Connected to $devDisplayName. Discovering services..."
                        Log.i("BleManager", "Connected to $devDisplayName ($category). Discovering services immediately...")
                        scope.launch {
                            kotlinx.coroutines.delay(100)
                            val discovered = gatt.discoverServices()
                            Log.i("BleManager", "discoverServices() invoked, returned: $discovered")
                        }
                    }
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    isConnecting = false
                    syncTimeoutJob?.cancel()
                    bp2RtJob?.cancel()
                    bp2RtJob = null
                    lastBp2State = -1
                    _statusText.value = "Scanning for scales and monitors..."
                    Log.i("BleManager", "Disconnected. Closing GATT and resuming scan.")
                    lepuReassembler.reset()
                    bpFileBuffer.clear()
                    bpFileSize = 0
                    fileQueue.clear()
                    currentDownloadFile = null
                    try { gatt.close() } catch (e: Exception) {}
                    activeGatt = null
                    startScan(force = true)
                }
            }

            override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
                Log.i("BleManager", "onMtuChanged: mtu=$mtu, status=$status. Discovering services...")
                gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
                gatt.discoverServices()
            }

            override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
                Log.i("BleManager", "onServicesDiscovered: status=$status")
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    _statusText.value = "Configuring $devDisplayName..."
                    handleServicesDiscovered(gatt, category, device.name ?: category.name, device.address)
                } else {
                    Log.e("BleManager", "Service discovery failed: status=$status")
                    try { gatt.close() } catch (e: Exception) {}
                    activeGatt = null
                    isConnecting = false
                    startScan(force = true)
                }
            }

            override fun onDescriptorWrite(gatt: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
                Log.i("BleManager", "onDescriptorWrite: ${descriptor.uuid} status=$status")
                if (category == DeviceCategory.VIATOM_BP2 && status == BluetoothGatt.GATT_SUCCESS) {
                    val service = gatt.getService(DeviceDecoders.VIATOM_SERVICE)
                    val writeChar = service?.getCharacteristic(DeviceDecoders.VIATOM_WRITE)
                    if (writeChar != null) {
                        scope.launch {
                            kotlinx.coroutines.delay(200)
                            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2SyncTime(nextLepuSeq()))
                            Log.i("BleManager", "Sent Viatom BP2 Sync Time")

                            kotlinx.coroutines.delay(200)
                            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2GetConfig(nextLepuSeq()))
                            Log.i("BleManager", "Sent Viatom BP2 Get Config")

                            kotlinx.coroutines.delay(200)
                            _statusText.value = "Connected to $devDisplayName (Syncing records...)"

                            // Request file list to check for any stored measurements
                            currentDownloadFile = null
                            fileQueue.clear()
                            bpFileSize = 0
                            bpFileBuffer.clear()
                            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2GetFileList(nextLepuSeq()))
                            Log.i("BleManager", "Sent Viatom BP2 Get File List (0xF1)")

                            // Start real-time polling ticker
                            startBp2RtPolling(gatt, writeChar)

                            // Guard timeout: if sync takes > 6 seconds, transition to Ready
                            syncTimeoutJob?.cancel()
                            syncTimeoutJob = scope.launch {
                                kotlinx.coroutines.delay(6000L)
                                if (_statusText.value.contains("Syncing records")) {
                                    Log.w("BleManager", "Sync timeout reached, transitioning to Ready.")
                                    onBpFileDownloadFinished()
                                }
                            }
                        }
                    }
                }
            }

            override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
                Log.i("BleManager", "onCharacteristicWrite: ${characteristic.uuid} status=$status")
            }

            // Android 13+ (API 33+) callback
            override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
                dispatchCharacteristicData(gatt, characteristic, value, category, device)
            }

            // Legacy Android callback (< API 33)
            @Deprecated("Deprecated in Java")
            override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                    val data = characteristic.value ?: return
                    dispatchCharacteristicData(gatt, characteristic, data, category, device)
                }
            }
        }

        activeGatt = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        } else {
            device.connectGatt(context, false, callback)
        }
    }

    private fun safeWriteDescriptor(gatt: BluetoothGatt, desc: BluetoothGattDescriptor, value: ByteArray) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            gatt.writeDescriptor(desc, value)
        } else {
            desc.value = value
            gatt.writeDescriptor(desc)
        }
    }

    private fun safeWriteCharacteristic(gatt: BluetoothGatt, char: BluetoothGattCharacteristic, value: ByteArray) {
        val writeType = if ((char.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0) {
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        } else {
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        }
        val maxChunk = 20
        if (value.size <= maxChunk) {
            writeSingleChunk(gatt, char, value, writeType)
        } else {
            scope.launch {
                var offset = 0
                while (offset < value.size) {
                    val end = Math.min(offset + maxChunk, value.size)
                    val chunk = value.copyOfRange(offset, end)
                    writeSingleChunk(gatt, char, chunk, writeType)
                    Log.d("BleManager", "safeWriteCharacteristic chunk [${offset}..${end - 1}] of ${value.size} bytes: ${chunk.joinToString(" ") { "%02X".format(it) }}")
                    offset = end
                    if (offset < value.size) {
                        kotlinx.coroutines.delay(30)
                    }
                }
            }
        }
    }

    private fun writeSingleChunk(gatt: BluetoothGatt, char: BluetoothGattCharacteristic, chunk: ByteArray, writeType: Int) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            gatt.writeCharacteristic(char, chunk, writeType)
        } else {
            char.writeType = writeType
            char.value = chunk
            gatt.writeCharacteristic(char)
        }
    }

    private fun dispatchCharacteristicData(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        data: ByteArray,
        category: DeviceCategory,
        device: BluetoothDevice
    ) {
        val devName = device.name ?: category.name
        val devAddress = device.address

        Log.d("BleManager", "dispatchCharacteristicData: [${characteristic.uuid}] len=${data.size}: ${data.joinToString(" ") { "%02X".format(it) }}")

        when (category) {
            DeviceCategory.YUNMAI_SCALE -> {
                DeviceDecoders.parseYunmai(data, devName, devAddress)?.let { weight ->
                    _latestWeight.value = weight
                    _statusText.value = "Weight: ${weight.weightLb} lb (${weight.weightKg} kg)"
                    onNewWeight?.invoke(weight)
                }
            }
            DeviceCategory.ETEKCITY_SCALE -> {
                DeviceDecoders.parseEtekcity(data, devName, devAddress)?.let { weight ->
                    _latestWeight.value = weight
                    _statusText.value = "Weight: ${weight.weightLb} lb (${weight.weightKg} kg)"
                    onNewWeight?.invoke(weight)
                }
            }
            DeviceCategory.FITDAYS_SCALE -> {
                val weight = DeviceDecoders.parseFitdays(data, devName, devAddress)
                    ?: DeviceDecoders.parseStandardWeight(data, devName, devAddress)
                weight?.let { w ->
                    _latestWeight.value = w
                    val impText = if (w.impedance != null && w.impedance > 0) " (Imp: ${w.impedance}Ω)" else ""
                    _statusText.value = "Weight: ${w.weightLb} lb (${w.weightKg} kg)$impText"
                    Log.i("BleManager", ">>> INGESTED FITDAYS WEIGHT: ${w.weightLb} lb$impText")
                    onNewWeight?.invoke(w)
                }
            }
            DeviceCategory.VIATOM_BP2 -> {
                lepuReassembler.feed(data) { packet ->
                    handleBp2Packet(gatt, packet, devName, devAddress)
                }
            }
            DeviceCategory.OMRON_BP -> {
                DeviceDecoders.parseOmron(data, devName, devAddress)?.let { bp ->
                    _latestBp.value = bp
                    _statusText.value = "BP: ${bp.systolic}/${bp.diastolic} mmHg (P: ${bp.pulse})"
                    onNewBp?.invoke(bp)
                }
            }
            else -> Unit
        }
    }

    private fun startBp2RtPolling(gatt: BluetoothGatt, writeChar: BluetoothGattCharacteristic) {
        bp2RtJob?.cancel()
        bp2RtJob = scope.launch {
            while (isActive && activeGatt == gatt) {
                // Do not interleave RT polling when file download is active
                if (currentDownloadFile == null && fileQueue.isEmpty()) {
                    safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2RtData(nextLepuSeq()))
                }
                kotlinx.coroutines.delay(1000L)
            }
        }
    }

    private fun handleBp2Packet(
        gatt: BluetoothGatt,
        packet: LepuDecodedPacket,
        devName: String,
        devAddress: String
    ) {
        val service = gatt.getService(DeviceDecoders.VIATOM_SERVICE)
        val writeChar = service?.getCharacteristic(DeviceDecoders.VIATOM_WRITE)

        when (packet.cmd) {
            0x08 -> { // CMD_RT_DATA
                val rt = DeviceDecoders.parseBp2RtData(packet.payload, devName, devAddress)
                Log.d("BleManager", "BP2 RT Data: status=${rt.status} cuff=${rt.cuffPressure} bp=${rt.bp} ecg=${rt.ecg?.heartRate}")

                when (rt.status) {
                    4, 15 -> { // Measuring BP
                        if (rt.cuffPressure != null && rt.cuffPressure > 0) {
                            _statusText.value = "Measuring BP: ${rt.cuffPressure} mmHg"
                        } else {
                            _statusText.value = "Measuring BP..."
                        }
                    }
                    5, 17 -> { // BP Complete
                        rt.bp?.let { bp ->
                            dispatchBp(bp)
                        }
                        if (lastBp2State != 5 && lastBp2State != 17) {
                            Log.i("BleManager", "BP2 completed BP measurement (status=${rt.status}). Requesting file list...")
                            if (writeChar != null && currentDownloadFile == null && fileQueue.isEmpty()) {
                                safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2GetFileList(nextLepuSeq()))
                            }
                        }
                    }
                    6 -> { // Measuring ECG
                        val hr = rt.ecg?.heartRate ?: 0
                        val hrText = if (hr > 0) " ($hr bpm)" else ""
                        _statusText.value = "Measuring ECG$hrText..."

                        rt.ecg?.let { newEcg ->
                            if (newEcg.samplesMv.isNotEmpty()) {
                                synchronized(ecgSampleBuffer) {
                                    ecgSampleBuffer.addAll(newEcg.samplesMv)
                                    while (ecgSampleBuffer.size > 500) {
                                        ecgSampleBuffer.removeAt(0)
                                    }
                                    val buffered = newEcg.copy(
                                        samplesMv = ecgSampleBuffer.toList(),
                                        durationSeconds = ecgSampleBuffer.size / 125f
                                    )
                                    _latestEcg.value = buffered
                                }
                            } else if (hr > 0) {
                                val current = _latestEcg.value
                                _latestEcg.value = (current ?: newEcg).copy(
                                    heartRate = hr,
                                    rhythmStatus = newEcg.rhythmStatus
                                )
                            }
                        }
                    }
                    7 -> { // ECG Complete
                        if (lastBp2State != 7) {
                            val finalHr = rt.ecg?.heartRate ?: 0
                            val diagText = rt.ecg?.rhythmStatus ?: "Complete"
                            Log.i("BleManager", "BP2 completed ECG (status=7). HR: $finalHr bpm ($diagText). Requesting file list...")
                            _statusText.value = "ECG Complete: $finalHr bpm. Syncing..."

                            rt.ecg?.let { finalSummary ->
                                synchronized(ecgSampleBuffer) {
                                    val currentSamples = _latestEcg.value?.samplesMv ?: emptyList()
                                    val samples = when {
                                        currentSamples.isNotEmpty() -> currentSamples
                                        ecgSampleBuffer.isNotEmpty() -> ecgSampleBuffer.toList()
                                        else -> finalSummary.samplesMv
                                    }
                                    val completeEcg = finalSummary.copy(
                                        samplesMv = samples,
                                        durationSeconds = if (samples.isNotEmpty()) samples.size / 125f else 30f
                                    )
                                    _latestEcg.value = completeEcg
                                    if (samples.isNotEmpty()) {
                                        onNewEcg?.invoke(completeEcg)
                                    }
                                }
                            }

                            if (writeChar != null && currentDownloadFile == null && fileQueue.isEmpty()) {
                                safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2GetFileList(nextLepuSeq()))
                            }
                        }
                    }
                    3 -> { // Ready
                        if (lastBp2State in listOf(4, 5, 6, 7, 15, 16, 17)) {
                            if (!_statusText.value.startsWith("BP:") && !_statusText.value.startsWith("ECG:")) {
                                _statusText.value = "BP2 Ready"
                            }
                        }
                    }
                }
                lastBp2State = rt.status
            }
            0x06 -> { // CMD_GET_CONFIG
                if (packet.payload.isNotEmpty()) {
                    val state = packet.payload[0].toInt() and 0xFF
                    val stateText = when (state) {
                        0 -> "Sleep"
                        1 -> "Memory"
                        2 -> "Charging"
                        3 -> "Ready"
                        4 -> "Measuring BP"
                        5 -> "BP Complete"
                        6 -> "Measuring ECG"
                        7 -> "ECG Complete"
                        15 -> "BP Avg Measuring"
                        16 -> "BP Avg Waiting"
                        17 -> "BP Avg Complete"
                        else -> "State $state"
                    }
                    Log.i("BleManager", "BP2 Device State: $stateText ($state)")
                }
            }
            0xF1 -> { // CMD_GET_FILE_LIST
                val files = DeviceDecoders.parseBp2FileList(packet.payload)
                Log.i("BleManager", "BP2 File List: ${files.size} files found.")
                val timestampedFiles = files.filter { it.length == 14 && it.all { c -> c.isDigit() } }.sorted()
                Log.i("BleManager", "BP2 Timestamped files (${timestampedFiles.size}): ${timestampedFiles.takeLast(3).reversed()}")

                val uningested = timestampedFiles.filter { !ingestedBpFiles.contains(it) }
                if (uningested.isEmpty()) {
                    Log.i("BleManager", "No new BP2 files to download.")
                    onBpFileDownloadFinished()
                } else {
                    val toDownload = if (ingestedBpFiles.isEmpty() && uningested.size > 2) {
                        uningested.dropLast(2).forEach { ingestedBpFiles.add(it) }
                        uningested.takeLast(2)
                    } else {
                        uningested
                    }

                    for (f in toDownload) {
                        if (!fileQueue.contains(f) && currentDownloadFile != f) {
                            fileQueue.add(f)
                            Log.i("BleManager", "Queued file for download: $f")
                        }
                    }

                    if (currentDownloadFile == null && fileQueue.isNotEmpty()) {
                        startNextFileDownload(gatt)
                    }
                }
            }
            0xF2 -> { // CMD_READ_FILE_START
                val isError = packet.status != 0x01 || packet.payload.size < 4
                if (!isError) {
                    val size = (packet.payload[0].toInt() and 0xFF) or
                            ((packet.payload[1].toInt() and 0xFF) shl 8) or
                            ((packet.payload[2].toInt() and 0xFF) shl 16) or
                            ((packet.payload[3].toInt() and 0xFF) shl 24)
                    Log.i("BleManager", "BP2 File [$currentDownloadFile] Size: $size bytes")
                    bpFileSize = size
                    bpFileBuffer.clear()
                    if (size > 0 && writeChar != null) {
                        scope.launch {
                            kotlinx.coroutines.delay(40)
                            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileData(0, nextLepuSeq()))
                        }
                    } else {
                        Log.i("BleManager", "BP2 File [$currentDownloadFile] is empty.")
                        currentDownloadFile?.let { ingestedBpFiles.add(it) }
                        if (writeChar != null) {
                            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileEnd(nextLepuSeq()))
                        }
                        if (fileQueue.isNotEmpty()) {
                            startNextFileDownload(gatt)
                        } else {
                            onBpFileDownloadFinished()
                        }
                    }
                } else {
                    Log.w("BleManager", "BP2 Read File Start rejected for $currentDownloadFile: status=0x%02X len=${packet.payload.size}".format(packet.status))
                    currentDownloadFile?.let { ingestedBpFiles.add(it) }
                    if (writeChar != null) {
                        safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileEnd(nextLepuSeq()))
                    }
                    if (fileQueue.isNotEmpty()) {
                        startNextFileDownload(gatt)
                    } else {
                        onBpFileDownloadFinished()
                    }
                }
            }
            0xF3 -> { // CMD_READ_FILE_DATA
                if (packet.status != 0x01 || packet.payload.isEmpty()) {
                    Log.w("BleManager", "BP2 File Chunk rejected or empty (status=0x%02X, len=${packet.payload.size}) at offset ${bpFileBuffer.size}/$bpFileSize. Aborting download.".format(packet.status))
                    currentDownloadFile?.let { ingestedBpFiles.add(it) }
                    if (writeChar != null) {
                        safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileEnd(nextLepuSeq()))
                    }
                    if (fileQueue.isNotEmpty()) {
                        startNextFileDownload(gatt)
                    } else {
                        onBpFileDownloadFinished()
                    }
                    return
                }

                for (b in packet.payload) bpFileBuffer.add(b)
                Log.d("BleManager", "BP2 File Chunk: ${packet.payload.size} bytes (total ${bpFileBuffer.size}/$bpFileSize)")
                if (bpFileBuffer.size < bpFileSize && writeChar != null) {
                    val nextOffset = bpFileBuffer.size
                    scope.launch {
                        kotlinx.coroutines.delay(20)
                        safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileData(nextOffset, nextLepuSeq()))
                    }
                } else if (bpFileBuffer.size >= bpFileSize) {
                    val finishedFile = currentDownloadFile ?: "unknown"
                    Log.i("BleManager", "BP2 File Download Complete: $finishedFile (${bpFileBuffer.size} bytes)!")
                    ingestedBpFiles.add(finishedFile)
                    if (writeChar != null) {
                        safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileEnd(nextLepuSeq()))
                    }
                    val data = bpFileBuffer.toByteArray()
                    if (finishedFile.contains("nibp")) {
                        val records = DeviceDecoders.parseBp2NibpFile(data, devName, devAddress)
                        Log.i("BleManager", "Parsed ${records.size} BP records from $finishedFile")
                        if (records.isNotEmpty()) {
                            val latest = records.maxByOrNull { it.timestamp.toEpochMilli() } ?: records.last()
                            dispatchBp(latest)
                        }
                    } else {
                        val fileType = if (data.size >= 2) data[1].toInt() and 0xFF else 0
                        Log.i("BleManager", "BP2 File [$finishedFile] Content len=${data.size}, fileType=$fileType")
                        if (fileType == 1) {
                            // Single BP Record File
                            DeviceDecoders.parseBp2SingleBpFile(data, devName, devAddress)?.let { bp ->
                                dispatchBp(bp)
                            }
                        } else if (fileType == 2) {
                            // Lead-I ECG File!
                            val ecg = DeviceDecoders.parseBp2EcgFile(data, devName, devAddress)
                            if (ecg != null) {
                                synchronized(ecgSampleBuffer) {
                                    ecgSampleBuffer.clear()
                                    ecgSampleBuffer.addAll(ecg.samplesMv)
                                }
                                _latestEcg.value = ecg
                                _statusText.value = "ECG: ${ecg.heartRate} bpm (${ecg.rhythmStatus})"
                                Log.i("BleManager", ">>> INGESTED LATEST ECG RECORD FROM FILE: ${ecg.heartRate} bpm, ${ecg.samplesMv.size} samples, status: ${ecg.rhythmStatus}")
                                onNewEcg?.invoke(ecg)
                            }
                        }
                    }

                    if (fileQueue.isNotEmpty()) {
                        startNextFileDownload(gatt)
                    } else {
                        onBpFileDownloadFinished()
                    }
                }
            }
            0xF4 -> {
                Log.i("BleManager", "BP2 Read File End ACK received")
            }
            0xEC -> {
                Log.i("BleManager", "BP2 Sync Time ACK received")
            }
            0x00, 0xE1 -> {
                Log.i("BleManager", "BP2 Info received (${packet.payload.size} bytes)")
            }
            else -> {
                Log.d("BleManager", "BP2 Unhandled packet: cmd=0x%02X len=%d".format(packet.cmd, packet.payload.size))
            }
        }
    }

    private fun onBpFileDownloadFinished() {
        syncTimeoutJob?.cancel()
        currentDownloadFile = null
        bpFileSize = 0
        bpFileBuffer.clear()
        if (!_statusText.value.startsWith("BP:") && !_statusText.value.startsWith("ECG:")) {
            _statusText.value = "BP2 Ready"
        }
    }

    private fun startNextFileDownload(gatt: BluetoothGatt) {
        val service = gatt.getService(DeviceDecoders.VIATOM_SERVICE) ?: return
        val writeChar = service.getCharacteristic(DeviceDecoders.VIATOM_WRITE) ?: return
        if (fileQueue.isEmpty()) {
            Log.i("BleManager", "All BP2 file downloads completed.")
            onBpFileDownloadFinished()
            return
        }
        val file = fileQueue.removeFirst()
        currentDownloadFile = file
        bpFileSize = 0
        bpFileBuffer.clear()
        scope.launch {
            kotlinx.coroutines.delay(50)
            Log.i("BleManager", "Starting download of BP2 file: $file")
            safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildBp2ReadFileStart(file, seq = nextLepuSeq()))
        }
    }

    private fun handleServicesDiscovered(gatt: BluetoothGatt, category: DeviceCategory, name: String, address: String) {
        when (category) {
            DeviceCategory.YUNMAI_SCALE -> {
                val service = gatt.getService(DeviceDecoders.YUNMAI_SERVICE)
                val notifyChar = service?.getCharacteristic(DeviceDecoders.YUNMAI_NOTIFY)
                if (notifyChar != null) {
                    gatt.setCharacteristicNotification(notifyChar, true)
                    val desc = notifyChar.getDescriptor(cccdUuid)
                    if (desc != null) {
                        safeWriteDescriptor(gatt, desc, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                    }
                }
                val forceLb = configManager?.config?.value?.forceYunmaiPounds ?: true
                val writeChar = service?.getCharacteristic(DeviceDecoders.YUNMAI_WRITE)
                if (writeChar != null) {
                    scope.launch {
                        kotlinx.coroutines.delay(300)
                        safeWriteCharacteristic(gatt, writeChar, DeviceDecoders.buildYunmaiUnitPacket(unitLb = forceLb))
                        Log.i("BleManager", "Sent Yunmai Unit packet: unitLb=$forceLb")
                    }
                }
            }

            DeviceCategory.ETEKCITY_SCALE -> {
                val service = gatt.getService(DeviceDecoders.ETEKCITY_SERVICE)
                val notifyChar = service?.getCharacteristic(DeviceDecoders.ETEKCITY_NOTIFY)
                notifyChar?.let {
                    gatt.setCharacteristicNotification(it, true)
                    val desc = it.getDescriptor(cccdUuid)
                    if (desc != null) {
                        safeWriteDescriptor(gatt, desc, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                    }
                }
            }

            DeviceCategory.FITDAYS_SCALE -> {
                Log.i("BleManager", "Configuring Fitdays scale services for $name ($address)...")
                var subscribed = false
                val targetServices = listOf(
                    DeviceDecoders.FITDAYS_SERVICE,
                    UUID.fromString("0000fff0-0000-1000-8000-00805f9b34fb"),
                    UUID.fromString("0000181d-0000-1000-8000-00805f9b34fb")
                )
                for (sUuid in targetServices) {
                    val service = gatt.getService(sUuid) ?: continue
                    for (ch in service.characteristics) {
                        val hasNotify = (ch.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                        val hasIndicate = (ch.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
                        if (hasNotify || hasIndicate) {
                            gatt.setCharacteristicNotification(ch, true)
                            val desc = ch.getDescriptor(cccdUuid)
                            if (desc != null) {
                                val value = if (hasNotify) BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE else BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                                safeWriteDescriptor(gatt, desc, value)
                                Log.i("BleManager", "Subscribed to Fitdays notify/indicate char: ${ch.uuid} in service $sUuid")
                                subscribed = true
                            }
                        }
                    }
                }
                if (!subscribed) {
                    Log.w("BleManager", "No standard Fitdays service found. Checking all services...")
                    for (service in gatt.services) {
                        for (ch in service.characteristics) {
                            val hasNotify = (ch.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                            val hasIndicate = (ch.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
                            if (hasNotify || hasIndicate) {
                                gatt.setCharacteristicNotification(ch, true)
                                val desc = ch.getDescriptor(cccdUuid)
                                if (desc != null) {
                                    val value = if (hasNotify) BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE else BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                                    safeWriteDescriptor(gatt, desc, value)
                                    Log.i("BleManager", "Fallback subscribed to notify/indicate char: ${ch.uuid} in service ${service.uuid}")
                                    subscribed = true
                                }
                            }
                        }
                    }
                }
                _statusText.value = "Connected to $name. Ready for weigh-in."
            }

            DeviceCategory.VIATOM_BP2 -> {
                val service = gatt.getService(DeviceDecoders.VIATOM_SERVICE)
                if (service == null) {
                    Log.e("BleManager", "Viatom service ${DeviceDecoders.VIATOM_SERVICE} not found!")
                    for (s in gatt.services) {
                        Log.w("BleManager", "Available service: ${s.uuid}")
                    }
                    return
                }

                val notifyChar = service.getCharacteristic(DeviceDecoders.VIATOM_NOTIFY)
                if (notifyChar != null) {
                    gatt.setCharacteristicNotification(notifyChar, true)
                    val desc = notifyChar.getDescriptor(cccdUuid)
                    if (desc != null) {
                        safeWriteDescriptor(gatt, desc, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                        Log.i("BleManager", "Enabled CCCD notifications for Viatom BP2 notify char")
                    }
                }
            }

            DeviceCategory.OMRON_BP -> {
                val service = gatt.getService(DeviceDecoders.OMRON_SERVICE)
                val notifyChar = service?.getCharacteristic(DeviceDecoders.OMRON_NOTIFY)
                notifyChar?.let {
                    gatt.setCharacteristicNotification(it, true)
                    val desc = it.getDescriptor(cccdUuid)
                    if (desc != null) {
                        safeWriteDescriptor(gatt, desc, BluetoothGattDescriptor.ENABLE_INDICATION_VALUE)
                    }
                }
            }
            else -> Unit
        }
    }
}
