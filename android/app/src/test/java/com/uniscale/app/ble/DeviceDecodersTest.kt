package com.uniscale.app.ble

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceDecodersTest {

    private fun hexToBytes(hex: String): ByteArray {
        val clean = hex.replace(" ", "")
        val result = ByteArray(clean.length / 2)
        for (i in result.indices) {
            result[i] = clean.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
        return result
    }

    @Test
    fun testCapturedUserPacketDecoding() {
        // Captured from an AAA-series scale, MAC replaced with AA:BB:CC:DD:EE:01
        // Raw adv: 02 01 04 03 03 B0 FF 0F FF AC A0 01 EE DD CC BB AA A0 2C A0 A0 0D 99 07 09 41 41 41 30 30 32
        // Manufacturer data: companyId = 0xA0AC, data = 01 EE DD CC BB AA A0 2C A0 A0 0D 99
        val companyId = 0xA0AC
        val data = hexToBytes("01 EE DD CC BB AA A0 2C A0 A0 0D 99")
        val address = "AA:BB:CC:DD:EE:01"

        val frame = DeviceDecoders.parseAaaxManufacturerData(companyId, data, address)
        assertNotNull("Frame should decode successfully", frame)
        assertTrue(frame is AaaxFrame.Weight)
        val weight = frame as AaaxFrame.Weight
        assertEquals(0.0f, weight.weightKg, 0.001f)
        assertEquals(false, weight.isFinal)
    }

    @Test
    fun testOpenScaleSettlingFixture() {
        val companyId = 0xA0AC
        val address = "C0:FF:EE:12:34:56"
        val data = hexToBytes("56 34 12 EE FF C0 A0 CD 84 66 0D A4")

        val frame = DeviceDecoders.parseAaaxManufacturerData(companyId, data, address)
        assertNotNull("Settling frame should decode", frame)
        assertTrue(frame is AaaxFrame.Weight)
        val weight = frame as AaaxFrame.Weight
        assertEquals(74.95f, weight.weightKg, 0.001f)
        assertEquals(false, weight.isFinal)
    }

    @Test
    fun testOpenScaleStableFixture() {
        val companyId = 0xA0AC
        val address = "C0:FF:EE:12:34:56"
        val data = hexToBytes("56 34 12 EE FF C0 20 CD 84 58 0D B6")

        val frame = DeviceDecoders.parseAaaxManufacturerData(companyId, data, address)
        assertNotNull("Stable frame should decode", frame)
        assertTrue(frame is AaaxFrame.Weight)
        val weight = frame as AaaxFrame.Weight
        assertEquals(75.00f, weight.weightKg, 0.001f)
        assertEquals(true, weight.isFinal)
    }

    @Test
    fun testCorruptedChecksumRejection() {
        val companyId = 0xA0AC
        val address = "C0:FF:EE:12:34:56"
        val corrupted = hexToBytes("56 34 12 EE FF C0 20 CD 84 58 0D B7") // modified checksum

        val frame = DeviceDecoders.parseAaaxManufacturerData(companyId, corrupted, address)
        assertNull("Corrupted checksum must be rejected", frame)
    }

    @Test
    fun testBiaCalculator() {
        val comp = com.uniscale.app.model.BiaCalculator.calculate(
            weightKg = 70.0f,
            impedance = 500,
            heightCm = 175.0f,
            age = 40,
            isMale = true
        )
        assertEquals(22.9f, comp.bmi, 0.2f)
        assertTrue("Body fat should be between 10% and 25%", comp.bodyFatPercent in 10.0f..25.0f)
        assertTrue("Water % should be between 50% and 70%", comp.waterPercent in 50.0f..70.0f)
        assertTrue("Muscle mass should be between 40kg and 60kg", comp.muscleMassKg in 40.0f..60.0f)
        assertTrue("Bone mass should be between 2kg and 4kg", comp.boneMassKg in 2.0f..4.0f)
        assertTrue("Visceral fat index should be between 1 and 15", comp.visceralFat in 1..15)
        assertTrue("BMR should be between 1200 and 1700 kcal", comp.bmrKcal in 1200..1700)
    }

    @Test
    fun testBp2RtDataEcgMeasuring() {
        val payload = ByteArray(38)
        payload[0] = 0x06 // status = 6
        payload[1] = 0x00 // batStatus
        payload[2] = 0x39 // batPercent = 57%
        payload[9] = 0x02 // waveDataType
        payload[10] = 0x0A // duration = 10s
        payload[18] = 75 // hr = 75
        // 3 samples starting at offset 32: raw value 1000
        payload[32] = (1000 and 0xFF).toByte()
        payload[33] = ((1000 shr 8) and 0xFF).toByte()
        payload[34] = (1000 and 0xFF).toByte()
        payload[35] = ((1000 shr 8) and 0xFF).toByte()
        payload[36] = (1000 and 0xFF).toByte()
        payload[37] = ((1000 shr 8) and 0xFF).toByte()

        val res = DeviceDecoders.parseBp2RtData(payload, "BP2 Test", "AA:BB:CC:DD:EE:05")
        assertEquals(6, res.status)
        assertNull("cuffPressure must be null during ECG measuring", res.cuffPressure)
        assertNull("bp must be null during ECG measuring", res.bp)
        assertNotNull(res.ecg)
        assertEquals(75, res.ecg!!.heartRate)
        assertEquals(3, res.ecg!!.samplesMv.size)
        assertEquals(3.098f, res.ecg!!.samplesMv[0], 0.01f)
    }

    @Test
    fun testBp2RtDataEcgComplete() {
        val payload = hexToBytes("07 00 39 D4 0E 6A 00 00 00 03 00 00 00 00 4F 00 49 00 00 00 99 01 81 94 03 00 58 3A 00 20 00 00")
        val res = DeviceDecoders.parseBp2RtData(payload, "BP2 Test", "AA:BB:CC:DD:EE:05")
        assertEquals(7, res.status)
        assertNull("cuffPressure must be null during ECG complete", res.cuffPressure)
        assertNotNull(res.ecg)
        assertEquals(79, res.ecg!!.heartRate)
        assertEquals("Normal Sinus Rhythm", res.ecg!!.rhythmStatus)
    }

    @Test
    fun testBp2RtDataBpMeasuring() {
        val payload = ByteArray(15)
        payload[0] = 0x04 // status = 4
        payload[11] = (120 * 100 and 0xFF).toByte()
        payload[12] = ((120 * 100 shr 8) and 0xFF).toByte()

        val res = DeviceDecoders.parseBp2RtData(payload, "BP2 Test", "AA:BB:CC:DD:EE:05")
        assertEquals(4, res.status)
        assertEquals(120, res.cuffPressure)
        assertNull(res.ecg)
        assertNull(res.bp)
    }

    @Test
    fun testBp2RtDataIdleDoesNotLeakCuffPressure() {
        val payload = ByteArray(15)
        payload[0] = 0x03 // status = 3
        payload[11] = 58
        payload[12] = 0

        val res = DeviceDecoders.parseBp2RtData(payload, "BP2 Test", "AA:BB:CC:DD:EE:05")
        assertEquals(3, res.status)
        assertNull("Idle state must NEVER report cuff pressure", res.cuffPressure)
    }

    @Test
    fun testBp2EcgFileParsing() {
        val fileData = ByteArray(54)
        fileData[0] = 1 // fileVersion
        fileData[1] = 2 // fileType (ECG)
        // timestamp at 2..5 (1700000000)
        fileData[2] = (1700000000L and 0xFF).toByte()
        fileData[3] = ((1700000000L shr 8) and 0xFF).toByte()
        fileData[4] = ((1700000000L shr 16) and 0xFF).toByte()
        fileData[5] = ((1700000000L shr 24) and 0xFF).toByte()
        // duration at 10..13 (30s)
        fileData[10] = 30
        // result at 14..17 (0 = Normal)
        fileData[14] = 0
        // hr at 20..21 (72 bpm)
        fileData[20] = 72
        // qrs at 22..23 (80 ms)
        fileData[22] = 80
        // 3 samples at offset 48
        fileData[48] = 50
        fileData[49] = 0
        fileData[50] = 100
        fileData[51] = 0
        fileData[52] = (200 and 0xFF).toByte()
        fileData[53] = 0

        val ecg = DeviceDecoders.parseBp2EcgFile(fileData, "BP2 Test", "AA:BB:CC:DD:EE:05")
        assertNotNull(ecg)
        assertEquals(72, ecg!!.heartRate)
        assertEquals(30.0f, ecg.durationSeconds, 0.1f)
        assertEquals("Normal Sinus Rhythm", ecg.rhythmStatus)
        assertEquals(3, ecg.samplesMv.size)
        assertEquals(50 * 0.003098f, ecg.samplesMv[0], 0.001f)
    }
}
