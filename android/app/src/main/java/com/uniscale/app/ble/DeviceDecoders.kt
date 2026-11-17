package com.uniscale.app.ble

import android.bluetooth.le.ScanRecord
import android.util.Log
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.EcgData
import com.uniscale.app.model.WeightData
import java.time.Instant
import java.util.Calendar
import java.util.UUID

data class LepuDecodedPacket(
    val cmd: Int,
    val payload: ByteArray,
    val seq: Int,
    val status: Int = 0x01
)

data class Bp2RtResult(
    val status: Int = 0,
    val batteryStatus: Int = 0,
    val batteryPercent: Int = 0,
    val cuffPressure: Int? = null,
    val bp: BloodPressureData? = null,
    val ecg: EcgData? = null
)

sealed class AaaxFrame {
    data class Weight(val weightKg: Float, val isFinal: Boolean) : AaaxFrame()
    data class Impedance(val impedance: Int) : AaaxFrame()
}

class LepuPacketReassembler {
    private val buffer = mutableListOf<Byte>()
    private var lastFeedTime = 0L

    @Synchronized
    fun feed(data: ByteArray, onPacket: (LepuDecodedPacket) -> Unit) {
        val now = System.currentTimeMillis()
        if (now - lastFeedTime > 2000L && buffer.isNotEmpty()) {
            Log.w("LepuReassembler", "Buffer stale (${now - lastFeedTime}ms), clearing ${buffer.size} bytes.")
            buffer.clear()
        }
        lastFeedTime = now

        for (b in data) buffer.add(b)

        while (buffer.size >= 8) {
            val headerIdx = buffer.indexOf(0xA5.toByte())
            if (headerIdx < 0) {
                buffer.clear()
                return
            }
            if (headerIdx > 0) {
                for (k in 0 until headerIdx) buffer.removeAt(0)
            }
            if (buffer.size < 7) return

            val cmd = buffer[1].toInt() and 0xFF
            val cmdInv = buffer[2].toInt() and 0xFF
            if (cmdInv != ((cmd.inv()) and 0xFF)) {
                buffer.removeAt(0)
                continue
            }

            val length = (buffer[5].toInt() and 0xFF) or ((buffer[6].toInt() and 0xFF) shl 8)
            val totalLen = 7 + length + 1
            if (length > 4096) {
                buffer.removeAt(0)
                continue
            }
            if (buffer.size < totalLen) {
                return // wait for remaining notification chunks
            }

            val packetBytes = ByteArray(totalLen)
            for (i in 0 until totalLen) packetBytes[i] = buffer.removeAt(0)

            val calcCrc = DeviceDecoders.crc8(packetBytes, totalLen - 1)
            val recvCrc = packetBytes[totalLen - 1].toInt() and 0xFF
            if (calcCrc == recvCrc) {
                val payload = ByteArray(length)
                System.arraycopy(packetBytes, 7, payload, 0, length)
                val status = packetBytes[3].toInt() and 0xFF
                val seq = packetBytes[4].toInt() and 0xFF
                Log.i("LepuReassembler", "Reassembled packet: cmd=0x%02X len=$length seq=$seq status=0x%02X".format(cmd, status))
                onPacket(LepuDecodedPacket(cmd, payload, seq, status))
            } else {
                Log.w("LepuReassembler", "CRC check failed: cmd=0x%02X len=$length, calc=0x%02X, recv=0x%02X".format(cmd, calcCrc, recvCrc))
            }
        }
    }

    @Synchronized
    fun reset() {
        buffer.clear()
        lastFeedTime = 0L
    }
}

object DeviceDecoders {

    // UUID Constants
    val YUNMAI_SERVICE: UUID = UUID.fromString("0000ffe0-0000-1000-8000-00805f9b34fb")
    val YUNMAI_NOTIFY: UUID = UUID.fromString("0000ffe4-0000-1000-8000-00805f9b34fb")
    val YUNMAI_WRITE: UUID = UUID.fromString("0000ffe9-0000-1000-8000-00805f9b34fb")

    val ETEKCITY_SERVICE: UUID = UUID.fromString("0000fff0-0000-1000-8000-00805f9b34fb")
    val ETEKCITY_NOTIFY: UUID = UUID.fromString("0000fff1-0000-1000-8000-00805f9b34fb")

    val FITDAYS_SERVICE: UUID = UUID.fromString("0000ffb0-0000-1000-8000-00805f9b34fb")
    val FITDAYS_NOTIFY: UUID = UUID.fromString("0000ffb2-0000-1000-8000-00805f9b34fb")

    val VIATOM_SERVICE: UUID = UUID.fromString("14839ac4-7d7e-415c-9a42-167340cf2339")
    val VIATOM_WRITE: UUID = UUID.fromString("8b00ace7-eb0b-49b0-bbe9-9aee0a26e1a3")
    val VIATOM_NOTIFY: UUID = UUID.fromString("0734594a-a8e7-4b1a-a6b1-cd5243059a57")

    val OMRON_SERVICE: UUID = UUID.fromString("00001810-0000-1000-8000-00805f9b34fb")
    val OMRON_NOTIFY: UUID = UUID.fromString("00002a35-0000-1000-8000-00805f9b34fb")

    // --- LEPU CRC-8/CCITT TABLE (poly 0x07, init 0x00) ---
    private val CRC8_TABLE = intArrayOf(
        0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15,
        0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
        0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65,
        0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D,
        0xE0, 0xE7, 0xEE, 0xE9, 0xFC, 0xFB, 0xF2, 0xF5,
        0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
        0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85,
        0xA8, 0xAF, 0xA6, 0xA1, 0xB4, 0xB3, 0xBA, 0xBD,
        0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2,
        0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA,
        0xB7, 0xB0, 0xB9, 0xBE, 0xAB, 0xAC, 0xA5, 0xA2,
        0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
        0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32,
        0x1F, 0x18, 0x11, 0x16, 0x03, 0x04, 0x0D, 0x0A,
        0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42,
        0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A,
        0x89, 0x8E, 0x87, 0x80, 0x95, 0x92, 0x9B, 0x9C,
        0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
        0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC,
        0xC1, 0xC6, 0xCF, 0xC8, 0xDD, 0xDA, 0xD3, 0xD4,
        0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C,
        0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44,
        0x19, 0x1E, 0x17, 0x10, 0x05, 0x02, 0x0B, 0x0C,
        0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
        0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B,
        0x76, 0x71, 0x78, 0x7F, 0x6A, 0x6D, 0x64, 0x63,
        0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B,
        0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13,
        0xAE, 0xA9, 0xA0, 0xA7, 0xB2, 0xB5, 0xBC, 0xBB,
        0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
        0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB,
        0xE6, 0xE1, 0xE8, 0xEF, 0xFA, 0xFD, 0xF4, 0xF3
    )

    fun crc8(data: ByteArray, length: Int): Int {
        var crc = 0
        for (i in 0 until length) {
            crc = CRC8_TABLE[0xFF and (crc xor (data[i].toInt() and 0xFF))]
        }
        return crc
    }

    fun buildLepuPacket(cmd: Int, payload: ByteArray = ByteArray(0), seq: Int = 1): ByteArray {
        val length = payload.size
        val packet = ByteArray(7 + length + 1)
        packet[0] = 0xA5.toByte()
        packet[1] = (cmd and 0xFF).toByte()
        packet[2] = ((cmd.inv()) and 0xFF).toByte()
        packet[3] = 0x00 // TX (host -> device)
        packet[4] = (seq and 0xFF).toByte()
        packet[5] = (length and 0xFF).toByte()
        packet[6] = ((length shr 8) and 0xFF).toByte()
        System.arraycopy(payload, 0, packet, 7, length)
        packet[7 + length] = crc8(packet, 7 + length).toByte()
        return packet
    }

    fun buildBp2ReadFileStart(filename: String, seq: Int = 1): ByteArray {
        val payload = ByteArray(20)
        val nameBytes = filename.toByteArray(Charsets.US_ASCII)
        System.arraycopy(nameBytes, 0, payload, 0, Math.min(nameBytes.size, 16))
        return buildLepuPacket(0xF2, payload, seq)
    }

    fun buildBp2ReadFileData(offset: Int, seq: Int = 1): ByteArray {
        val payload = ByteArray(4)
        payload[0] = (offset and 0xFF).toByte()
        payload[1] = ((offset shr 8) and 0xFF).toByte()
        payload[2] = ((offset shr 16) and 0xFF).toByte()
        payload[3] = ((offset shr 24) and 0xFF).toByte()
        return buildLepuPacket(0xF3, payload, seq)
    }

    fun buildBp2ReadFileEnd(seq: Int = 1): ByteArray {
        return buildLepuPacket(0xF4, seq = seq)
    }

    fun buildBp2GetFileList(seq: Int = 1): ByteArray {
        return buildLepuPacket(0xF1, seq = seq)
    }

    fun buildBp2RtData(seq: Int = 1): ByteArray {
        return buildLepuPacket(0x08, seq = seq)
    }

    fun buildBp2GetInfo(seq: Int = 1): ByteArray {
        return buildLepuPacket(0x00, seq = seq)
    }

    fun buildBp2GetConfig(seq: Int = 1): ByteArray {
        return buildLepuPacket(0x06, seq = seq)
    }

    fun buildBp2GetDeviceInfo(seq: Int = 1): ByteArray {
        return buildLepuPacket(0xE1, seq = seq)
    }

    fun buildBp2SyncTime(seq: Int = 1): ByteArray {
        val cal = Calendar.getInstance()
        val year = cal.get(Calendar.YEAR)
        val month = cal.get(Calendar.MONTH) + 1
        val day = cal.get(Calendar.DAY_OF_MONTH)
        val hour = cal.get(Calendar.HOUR_OF_DAY)
        val min = cal.get(Calendar.MINUTE)
        val sec = cal.get(Calendar.SECOND)
        val payload = ByteArray(7)
        payload[0] = (year and 0xFF).toByte()
        payload[1] = ((year shr 8) and 0xFF).toByte()
        payload[2] = (month and 0xFF).toByte()
        payload[3] = (day and 0xFF).toByte()
        payload[4] = (hour and 0xFF).toByte()
        payload[5] = (min and 0xFF).toByte()
        payload[6] = (sec and 0xFF).toByte()
        return buildLepuPacket(0xEC, payload, seq)
    }

    fun parseBp2RtData(payload: ByteArray, name: String, address: String): Bp2RtResult {
        if (payload.isEmpty()) return Bp2RtResult()
        val status = payload[0].toInt() and 0xFF
        val batStatus = if (payload.size > 1) payload[1].toInt() and 0xFF else 0
        val batPercent = if (payload.size > 2) payload[2].toInt() and 0xFF else 0

        when (status) {
            4, 15 -> { // STATUS_BP_ING, STATUS_BP_AVG_ING (Measuring Blood Pressure)
                val cuff = if (payload.size >= 13) {
                    val rawPress = (payload[11].toInt() and 0xFF) or ((payload[12].toInt() and 0xFF) shl 8)
                    if (rawPress > 500) rawPress / 100 else rawPress
                } else if (payload.size in 5..9) {
                    (payload[3].toInt() and 0xFF) or ((payload[4].toInt() and 0xFF) shl 8)
                } else null

                return Bp2RtResult(
                    status = status,
                    batteryStatus = batStatus,
                    batteryPercent = batPercent,
                    cuffPressure = cuff
                )
            }
            5, 17 -> { // STATUS_BP_END, STATUS_BP_AVG_END (Blood Pressure Complete)
                var bp: BloodPressureData? = null
                if (payload.size >= 21) {
                    val sys = (payload[13].toInt() and 0xFF) or ((payload[14].toInt() and 0xFF) shl 8)
                    val dia = (payload[15].toInt() and 0xFF) or ((payload[16].toInt() and 0xFF) shl 8)
                    val pr = (payload[19].toInt() and 0xFF) or ((payload[20].toInt() and 0xFF) shl 8)
                    if (sys in 40..280 && dia in 20..200) {
                        bp = BloodPressureData(
                            systolic = sys,
                            diastolic = dia,
                            pulse = if (pr in 25..250) pr else 70,
                            deviceName = name,
                            deviceAddress = address
                        )
                    }
                } else if (payload.size in 11..16) {
                    val sys = (payload[3].toInt() and 0xFF) or ((payload[4].toInt() and 0xFF) shl 8)
                    val dia = (payload[5].toInt() and 0xFF) or ((payload[6].toInt() and 0xFF) shl 8)
                    val hr = (payload[9].toInt() and 0xFF) or ((payload[10].toInt() and 0xFF) shl 8)
                    if (sys in 40..280 && dia in 20..200) {
                        bp = BloodPressureData(
                            systolic = sys,
                            diastolic = dia,
                            pulse = if (hr in 25..250) hr else 70,
                            deviceName = name,
                            deviceAddress = address
                        )
                    }
                }
                return Bp2RtResult(
                    status = status,
                    batteryStatus = batStatus,
                    batteryPercent = batPercent,
                    bp = bp
                )
            }
            6 -> { // STATUS_ECG_ING (Measuring ECG)
                val curDuration = if (payload.size >= 14) {
                    (payload[10].toInt() and 0xFF) or
                            ((payload[11].toInt() and 0xFF) shl 8) or
                            ((payload[12].toInt() and 0xFF) shl 16) or
                            ((payload[13].toInt() and 0xFF) shl 24)
                } else 0

                val flag = if (payload.size >= 18) {
                    (payload[14].toInt() and 0xFF) or ((payload[15].toInt() and 0xFF) shl 8)
                } else 0
                val isPoorSignal = (flag and 1) != 0
                val isLeadOff = (flag and 2) != 0

                val hr = if (payload.size >= 20) {
                    (payload[18].toInt() and 0xFF) or ((payload[19].toInt() and 0xFF) shl 8)
                } else 0

                val samples = mutableListOf<Float>()
                if (payload.size >= 34) {
                    var i = 32
                    while (i + 1 < payload.size) {
                        val low = payload[i].toInt() and 0xFF
                        val high = payload[i + 1].toInt()
                        val raw = ((high shl 8) or low).toShort()
                        samples.add(raw * 0.003098f)
                        i += 2
                    }
                }

                val rhythmDesc = when {
                    isLeadOff -> "Lead Off - Hold electrodes firmly"
                    isPoorSignal -> "Weak signal - Relax hands"
                    hr in 25..250 -> "Measuring ECG ($hr bpm)..."
                    else -> "Measuring ECG..."
                }

                val ecg = EcgData(
                    heartRate = if (hr in 25..250) hr else 0,
                    sampleRateHz = 125,
                    samplesMv = samples,
                    durationSeconds = if (curDuration in 1..300) curDuration.toFloat() else (samples.size / 125f),
                    rhythmStatus = rhythmDesc,
                    deviceName = name,
                    deviceAddress = address
                )
                return Bp2RtResult(
                    status = status,
                    batteryStatus = batStatus,
                    batteryPercent = batPercent,
                    ecg = ecg
                )
            }
            7 -> { // STATUS_ECG_END (ECG Complete)
                var result = 0
                var hr = 0
                var qrs = 0
                var pvcs = 0
                var qtc = 0

                if (payload.size >= 14) {
                    result = (payload[10].toInt() and 0xFF) or
                            ((payload[11].toInt() and 0xFF) shl 8) or
                            ((payload[12].toInt() and 0xFF) shl 16) or
                            ((payload[13].toInt() and 0xFF) shl 24)
                }
                if (payload.size >= 16) {
                    hr = (payload[14].toInt() and 0xFF) or ((payload[15].toInt() and 0xFF) shl 8)
                }
                if (payload.size >= 18) {
                    qrs = (payload[16].toInt() and 0xFF) or ((payload[17].toInt() and 0xFF) shl 8)
                }
                if (payload.size >= 20) {
                    pvcs = (payload[18].toInt() and 0xFF) or ((payload[19].toInt() and 0xFF) shl 8)
                }
                if (payload.size >= 22) {
                    qtc = (payload[20].toInt() and 0xFF) or ((payload[21].toInt() and 0xFF) shl 8)
                }

                val rhythmStatus = when {
                    result == 0 -> "Normal Sinus Rhythm"
                    result == -1 -> "Poor Signal Quality"
                    result == -2 -> "Lead Off / Incomplete"
                    (result and 8) != 0 -> "Possible Atrial Fibrillation"
                    (result and 4) != 0 -> "Irregular ECG Rhythm"
                    (result and 16) != 0 -> "Possible PVCs"
                    (result and 32) != 0 -> "Pause / Missed Beat"
                    (result and 64) != 0 -> "Wide QRS ($qrs ms)"
                    (result and 128) != 0 -> "Prolonged QTc ($qtc ms)"
                    (result and 256) != 0 -> "Short QTc ($qtc ms)"
                    (result and 1) != 0 -> "Fast Heart Rate (Tachycardia)"
                    (result and 2) != 0 -> "Slow Heart Rate (Bradycardia)"
                    else -> "ECG Analyzed"
                }

                val ecg = EcgData(
                    heartRate = if (hr in 25..250) hr else 70,
                    sampleRateHz = 125,
                    samplesMv = emptyList(),
                    durationSeconds = 30f,
                    rhythmStatus = rhythmStatus,
                    deviceName = name,
                    deviceAddress = address
                )
                return Bp2RtResult(
                    status = status,
                    batteryStatus = batStatus,
                    batteryPercent = batPercent,
                    ecg = ecg
                )
            }
            else -> {
                return Bp2RtResult(
                    status = status,
                    batteryStatus = batStatus,
                    batteryPercent = batPercent
                )
            }
        }
    }

    fun parseBp2SingleBpFile(payload: ByteArray, name: String, address: String): BloodPressureData? {
        if (payload.size < 18) return null
        val sys = (payload[11].toInt() and 0xFF) or ((payload[12].toInt() and 0xFF) shl 8)
        val dia = (payload[13].toInt() and 0xFF) or ((payload[14].toInt() and 0xFF) shl 8)
        val pr = payload[17].toInt() and 0xFF

        val tsSec = (payload[2].toLong() and 0xFF) or
                ((payload[3].toLong() and 0xFF) shl 8) or
                ((payload[4].toLong() and 0xFF) shl 16) or
                ((payload[5].toLong() and 0xFF) shl 24)
        val timestamp = if (tsSec > 1500000000L) Instant.ofEpochSecond(tsSec) else Instant.now()

        if (sys in 40..280 && dia in 20..200) {
            return BloodPressureData(
                systolic = sys,
                diastolic = dia,
                pulse = if (pr in 25..250) pr else 70,
                deviceName = name,
                deviceAddress = address,
                timestamp = timestamp
            )
        }
        return null
    }

    fun parseBp2EcgFile(payload: ByteArray, name: String, address: String): EcgData? {
        if (payload.size < 48) return null
        val headerSize = 48

        // Timestamp at offset 2..5 (uint32 LE)
        val tsSec = (payload[2].toLong() and 0xFF) or
                ((payload[3].toLong() and 0xFF) shl 8) or
                ((payload[4].toLong() and 0xFF) shl 16) or
                ((payload[5].toLong() and 0xFF) shl 24)
        val timestamp = if (tsSec in 1500000000L..2000000000L) Instant.ofEpochSecond(tsSec) else Instant.now()

        // Recording duration at offset 10..13 (uint32 LE)
        val recTime = (payload[10].toInt() and 0xFF) or
                ((payload[11].toInt() and 0xFF) shl 8) or
                ((payload[12].toInt() and 0xFF) shl 16) or
                ((payload[13].toInt() and 0xFF) shl 24)

        // Diagnosis / Result at offset 14..17 (int32 LE)
        val result = (payload[14].toInt() and 0xFF) or
                ((payload[15].toInt() and 0xFF) shl 8) or
                ((payload[16].toInt() and 0xFF) shl 16) or
                ((payload[17].toInt() and 0xFF) shl 24)

        // Heart rate at offset 20..21 (uint16 LE)
        val hrRaw = (payload[20].toInt() and 0xFF) or ((payload[21].toInt() and 0xFF) shl 8)
        val hr = if (hrRaw in 30..250) hrRaw else 72

        // QRS at offset 22..23 (uint16 LE)
        val qrs = (payload[22].toInt() and 0xFF) or ((payload[23].toInt() and 0xFF) shl 8)

        val rhythmStatus = when {
            result == 0 -> "Normal Sinus Rhythm"
            result == -1 -> "Poor Signal Quality"
            result == -2 -> "Lead Off / Incomplete"
            (result and 8) != 0 -> "Possible Atrial Fibrillation"
            (result and 4) != 0 -> "Irregular ECG Rhythm"
            (result and 16) != 0 -> "Possible PVCs"
            (result and 32) != 0 -> "Pause / Missed Beat"
            (result and 64) != 0 -> "Wide QRS ($qrs ms)"
            (result and 1) != 0 -> "Fast Heart Rate (Tachycardia)"
            (result and 2) != 0 -> "Slow Heart Rate (Bradycardia)"
            else -> "ECG Analyzed"
        }

        // Waveform samples starting at offset 48, 125 Hz, scale factor 0.003098 mV
        val samples = mutableListOf<Float>()
        var i = headerSize
        while (i + 1 < payload.size) {
            val low = payload[i].toInt() and 0xFF
            val high = payload[i + 1].toInt()
            val raw = ((high shl 8) or low).toShort()
            val mv = raw * 0.003098f
            samples.add(mv)
            i += 2
        }

        val duration = if (recTime in 5..300) recTime.toFloat() else (samples.size / 125f)

        return EcgData(
            heartRate = hr,
            sampleRateHz = 125,
            samplesMv = samples,
            durationSeconds = duration,
            rhythmStatus = rhythmStatus,
            timestamp = timestamp,
            deviceName = name,
            deviceAddress = address
        )
    }

    fun parseBp2FileList(payload: ByteArray): List<String> {
        val files = mutableListOf<String>()
        if (payload.isEmpty()) return files

        val count = payload[0].toInt() and 0xFF
        val expectedLen = 1 + count * 16
        if (count in 1..250 && (payload.size == expectedLen || (payload.size - 1) % 16 == 0)) {
            val numFiles = (payload.size - 1) / 16
            for (i in 0 until numFiles) {
                val offset = 1 + (i * 16)
                val nameBytes = ByteArray(16)
                System.arraycopy(payload, offset, nameBytes, 0, 16)
                val name = String(nameBytes, Charsets.US_ASCII).trim('\u0000', ' ')
                if (name.isNotBlank()) {
                    files.add(name)
                }
            }
        } else {
            val raw = String(payload, Charsets.US_ASCII)
            val split = raw.split(Regex("[\u0000,]+")).map { it.trim() }.filter { it.isNotBlank() }
            files.addAll(split)
        }
        return files
    }

    fun parseBp2NibpFile(payload: ByteArray, name: String, address: String): List<BloodPressureData> {
        val results = mutableListOf<BloodPressureData>()
        val headerSize = 10
        val recordSize = 37
        if (payload.size < headerSize + recordSize) return results

        val numRecords = (payload.size - headerSize) / recordSize
        for (i in 0 until numRecords) {
            val offset = headerSize + (i * recordSize)
            val sys = (payload[offset + 13].toInt() and 0xFF) or ((payload[offset + 14].toInt() and 0xFF) shl 8)
            val dia = (payload[offset + 15].toInt() and 0xFF) or ((payload[offset + 16].toInt() and 0xFF) shl 8)
            val map = (payload[offset + 17].toInt() and 0xFF) or ((payload[offset + 18].toInt() and 0xFF) shl 8)
            val hr = (payload[offset + 19].toInt() and 0xFF) or ((payload[offset + 20].toInt() and 0xFF) shl 8)

            val ts = (payload[offset].toLong() and 0xFF) or
                    ((payload[offset + 1].toLong() and 0xFF) shl 8) or
                    ((payload[offset + 2].toLong() and 0xFF) shl 16) or
                    ((payload[offset + 3].toLong() and 0xFF) shl 24)

            if (sys in 40..280 && dia in 20..200) {
                results.add(
                    BloodPressureData(
                        systolic = sys,
                        diastolic = dia,
                        pulse = if (hr in 25..250) hr else 70,
                        deviceName = name,
                        deviceAddress = address,
                        timestamp = if (ts > 1500000000L) Instant.ofEpochSecond(ts) else Instant.now()
                    )
                )
            }
        }
        results.sortBy { it.timestamp.toEpochMilli() }
        return results
    }

    // --- YUNMAI ---
    fun buildYunmaiUnitPacket(unitLb: Boolean = true): ByteArray {
        val packet = ByteArray(18)
        packet[0] = 0x0D.toByte()
        packet[1] = 0x12.toByte()
        packet[2] = 0x10.toByte()
        packet[4] = 0x00
        packet[5] = 0x01
        packet[6] = 0x01
        packet[7] = 30 // age
        packet[8] = 175.toByte() // height cm
        packet[15] = if (unitLb) 0x02.toByte() else 0x01.toByte()

        var checksum = 0
        for (i in 1..16) {
            checksum = checksum xor packet[i].toInt()
        }
        packet[17] = checksum.toByte()
        return packet
    }

    fun parseYunmai(data: ByteArray, name: String, address: String): WeightData? {
        if (data.size < 14 || data[0] != 0x0D.toByte()) return null
        val cmd = data[2].toInt()
        if (cmd == 0x02) {
            val b13 = data[13].toInt() and 0xFF
            val b14 = data[14].toInt() and 0xFF
            var rawWeight = (b13 shl 8) or b14
            if (rawWeight == 0 && data.size >= 12) {
                rawWeight = ((data[10].toInt() and 0xFF) shl 8) or (data[11].toInt() and 0xFF)
            }
            val weightKg = rawWeight / 100.0f
            var impedance: Int? = null
            if (data.size >= 17) {
                val imp = ((data[15].toInt() and 0xFF) shl 8) or (data[16].toInt() and 0xFF)
                if (imp > 0) impedance = imp
            }
            if (weightKg > 10.0f) {
                return WeightData(weightKg = weightKg, impedance = impedance, deviceName = name, deviceAddress = address)
            }
        }
        return null
    }

    // --- ETEKCITY / VESYNC ---
    fun parseEtekcity(data: ByteArray, name: String, address: String): WeightData? {
        if (data.size < 7) return null
        val header = data[0].toInt() and 0xFF
        if (header == 0xA5 || header == 0x10) {
            val b2 = data[2].toInt() and 0xFF
            val b3 = data[3].toInt() and 0xFF
            val raw = (b2 shl 8) or b3
            val unit = (data[1].toInt() and 0x03)
            val weightKg = if (unit == 1) {
                val lb = if (raw > 1000) raw / 100f else raw / 10f
                lb / 2.20462262f
            } else {
                if (raw > 1000) raw / 100f else raw / 10f
            }
            if (weightKg > 10f) {
                return WeightData(weightKg = weightKg, deviceName = name, deviceAddress = address)
            }
        }
        return null
    }

    // --- FITDAYS / ICOMON ---
    fun parseFitdays(data: ByteArray, name: String, address: String): WeightData? {
        if (data.size < 4) return null
        val header = data[0].toInt() and 0xFF
        if (header in listOf(0xCF, 0xCB, 0xCA, 0xCE, 0x02, 0x10, 0x20)) {
            // Test candidate weight positions: [2..3] (standard), [1..2] (Icomon 0x02 format)
            val candidates = mutableListOf<Int>()
            if (data.size >= 4) {
                candidates.add(((data[2].toInt() and 0xFF) shl 8) or (data[3].toInt() and 0xFF))
                // Also little-endian [2..3]
                candidates.add(((data[3].toInt() and 0xFF) shl 8) or (data[2].toInt() and 0xFF))
            }
            if (data.size >= 3) {
                candidates.add(((data[1].toInt() and 0xFF) shl 8) or (data[2].toInt() and 0xFF))
                // Also little-endian [1..2]
                candidates.add(((data[2].toInt() and 0xFF) shl 8) or (data[1].toInt() and 0xFF))
            }

            for (raw in candidates) {
                val weightKg = when {
                    raw in 2500..25000 -> raw / 100f
                    raw in 25000..250000 -> Math.round(raw / 10f) / 100f
                    raw in 250..2500 -> raw / 10f
                    else -> 0f
                }
                // Realistic human body weight check: 25 kg (~55 lb) to 250 kg (~550 lb)
                if (weightKg in 25.0f..250.0f) {
                    var impedance: Int? = null
                    // Test possible impedance offsets: [4..5], [3..4], [5..6]
                    val impCandidates = mutableListOf<Int>()
                    if (data.size >= 6) {
                        impCandidates.add(((data[4].toInt() and 0xFF) shl 8) or (data[5].toInt() and 0xFF))
                    }
                    if (data.size >= 5) {
                        impCandidates.add(((data[3].toInt() and 0xFF) shl 8) or (data[4].toInt() and 0xFF))
                    }
                    if (data.size >= 7) {
                        impCandidates.add(((data[5].toInt() and 0xFF) shl 8) or (data[6].toInt() and 0xFF))
                    }
                    for (imp in impCandidates) {
                        if (imp in 150..2500) {
                            impedance = imp
                            break
                        }
                    }
                    return WeightData(
                        weightKg = weightKg,
                        impedance = impedance,
                        deviceName = name,
                        deviceAddress = address
                    )
                }
            }
        }
        return null
    }

    fun parseFitdaysFromScanRecord(scanRecord: ScanRecord?, name: String, address: String): WeightData? {
        if (scanRecord == null) return null

        // 1. Check all manufacturer data entries
        scanRecord.manufacturerSpecificData?.let { mfg ->
            for (i in 0 until mfg.size()) {
                val key = mfg.keyAt(i)
                val value = mfg.valueAt(i)
                parseFitdays(value, name, address)?.let { return it }
                // Also test with 2-byte company ID prepended
                val le = byteArrayOf((key and 0xFF).toByte(), ((key shr 8) and 0xFF).toByte()) + value
                parseFitdays(le, name, address)?.let { return it }
                val be = byteArrayOf(((key shr 8) and 0xFF).toByte(), (key and 0xFF).toByte()) + value
                parseFitdays(be, name, address)?.let { return it }
            }
        }

        // 2. Check service data
        for ((_, sData) in scanRecord.serviceData) {
            parseFitdays(sData, name, address)?.let { return it }
        }

        // 3. Scan the entire raw advertisement payload for any Fitdays frame
        val raw = scanRecord.bytes ?: return null
        for (i in 0 until raw.size - 4) {
            val b = raw[i].toInt() and 0xFF
            if (b in listOf(0xCF, 0xCB, 0xCA, 0xCE, 0x02)) {
                val sub = raw.copyOfRange(i, raw.size)
                parseFitdays(sub, name, address)?.let { return it }
            }
        }

        return null
    }

    /**
     * Decode AAA-series broadcast payload (Icomon / Fitdays AAA002, AAA007, AAA013, AAA07A, Posture FG220LB).
     * Format:
     * - bytes 0..5: advertiser's MAC in reverse order (e.g. 01 EE DD CC BB AA for AA:BB:CC:DD:EE:01)
     * - bytes 6..11: payload XOR-encoded with (companyId >> 8) & 0xFF
     *   payload[0..3]: 32-bit big-endian integer: [ state:1 | reserved:13 | grams:18 ]
     *                  state = 1: final/stabilized, 0: live/measuring
     *   payload[4]: 0xAD = weight, 0xA6 = impedance
     *   payload[5]: checksum nibble (sum(payload[0..4]) & 0x1F == payload[5] & 0x1F)
     */
    fun parseAaaxManufacturerData(companyId: Int, data: ByteArray, address: String = ""): AaaxFrame? {
        if (data.size < 12) return null

        // Check advertiser MAC echo (bytes 0..5 reversed) if address is provided
        val macParts = address.split(":").mapNotNull { it.toIntOrNull(16) }
        val echoes = if (macParts.size == 6) {
            (0 until 6).all { (data[it].toInt() and 0xFF) == macParts[5 - it] }
        } else {
            false
        }

        // Accept if MAC echoes or company ID is standard Chipsea/Icomon (0xA0AC)
        if (!echoes && companyId != 0xA0AC) return null

        val xorKey = (companyId ushr 8) and 0xFF
        val payload = ByteArray(6) { p -> (data[6 + p].toInt() xor xorKey).toByte() }
        val chk = (0 until 5).sumOf { payload[it].toInt() and 0xFF }
        if ((chk and 0x1F) != (payload[5].toInt() and 0x1F)) return null

        val frameType = payload[4].toInt() and 0xFF
        when (frameType) {
            0xAD -> { // Weight frame
                val value = ((payload[0].toLong() and 0xFF) shl 24) or
                            ((payload[1].toLong() and 0xFF) shl 16) or
                            ((payload[2].toLong() and 0xFF) shl 8) or
                            (payload[3].toLong() and 0xFF)
                val isFinal = ((value ushr 31) and 0x1L) != 0L
                val grams = (value and 0x3FFFFL).toInt()
                val weightKg = grams / 1000f
                return AaaxFrame.Weight(weightKg = weightKg, isFinal = isFinal)
            }
            0xA6 -> { // Impedance frame
                val imp1 = ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF)
                val imp2 = ((payload[2].toInt() and 0xFF) shl 8) or (payload[3].toInt() and 0xFF)
                val impedance = when {
                    imp1 in 150..2500 -> imp1
                    imp2 in 150..2500 -> imp2
                    else -> null
                }
                if (impedance != null) {
                    return AaaxFrame.Impedance(impedance = impedance)
                }
            }
        }
        return null
    }

    fun parseAaaxFromScanRecord(scanRecord: ScanRecord?, address: String): AaaxFrame? {
        if (scanRecord == null) return null

        // 1. Check manufacturerSpecificData
        scanRecord.manufacturerSpecificData?.let { mfg ->
            for (i in 0 until mfg.size()) {
                val companyId = mfg.keyAt(i)
                val data = mfg.valueAt(i) ?: continue
                parseAaaxManufacturerData(companyId, data, address)?.let { return it }
            }
        }

        // 2. Scan raw bytes for 0xFF manufacturer block
        val raw = scanRecord.bytes ?: return null
        var idx = 0
        while (idx < raw.size - 2) {
            val len = raw[idx].toInt() and 0xFF
            if (len == 0 || idx + 1 + len > raw.size) break
            val type = raw[idx + 1].toInt() and 0xFF
            if (type == 0xFF && len >= 14) { // 2 bytes company ID + >= 12 bytes data
                val companyId = (raw[idx + 2].toInt() and 0xFF) or ((raw[idx + 3].toInt() and 0xFF) shl 8)
                val data = raw.copyOfRange(idx + 4, idx + 1 + len)
                parseAaaxManufacturerData(companyId, data, address)?.let { return it }
            }
            idx += 1 + len
        }

        return null
    }

    // --- BLUETOOTH SIG STANDARD WEIGHT (0x2A9D) ---
    fun parseStandardWeight(data: ByteArray, name: String, address: String): WeightData? {
        if (data.size < 3) return null
        val flags = data[0].toInt() and 0xFF
        val isImperial = (flags and 0x01) != 0
        val raw = (data[1].toInt() and 0xFF) or ((data[2].toInt() and 0xFF) shl 8)
        val weightKg = if (isImperial) {
            val lb = raw * 0.01f
            lb / 2.20462262f
        } else {
            raw * 0.005f
        }
        if (weightKg > 10f) {
            return WeightData(weightKg = weightKg, deviceName = name, deviceAddress = address)
        }
        return null
    }

    // --- OMRON BP ---
    fun parseOmron(data: ByteArray, name: String, address: String): BloodPressureData? {
        if (data.size < 7) return null
        val flags = data[0].toInt() and 0xFF
        val isKpa = (flags and 0x01) != 0

        val sysRaw = (data[1].toInt() and 0xFF) or ((data[2].toInt() and 0xFF) shl 8)
        val diaRaw = (data[3].toInt() and 0xFF) or ((data[4].toInt() and 0xFF) shl 8)

        var sys = sysRaw and 0x0FFF
        var dia = diaRaw and 0x0FFF
        if (isKpa) {
            sys = Math.round(sys * 7.50062f)
            dia = Math.round(dia * 7.50062f)
        }

        val pulse = if (data.size >= 16) data[14].toInt() and 0xFF else 70

        if (sys in 41..259 && dia in 31..159) {
            return BloodPressureData(
                systolic = sys,
                diastolic = dia,
                pulse = pulse,
                deviceName = name,
                deviceAddress = address
            )
        }
        return null
    }
}
