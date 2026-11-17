package com.uniscale.app.sync

import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.WeightData
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

class LocalReadingsTest {

    @Test
    fun `a weigh-in survives the round trip through JSON`() {
        val w = WeightData(
            weightKg = 80.5f, impedance = 500, bodyFatPercent = 21.3f, visceralFat = 9,
            timestamp = Instant.parse("2026-11-16T12:00:00Z"), deviceName = "Scale", deviceAddress = "AA:BB:CC:DD:EE:01"
        )
        assertEquals(w, LocalReadings.weightFrom(JSONObject(LocalReadings.weightJson(w).toString())))
    }

    @Test
    fun `the hub's history records read as readings`() {
        val hubWeight = JSONObject(
            """{"type":"weight","weight_kg":80.0,"weight_lb":176.4,"bmi":null,"timestamp":"2026-10-19T11:59:15Z",
               "device_name":"Yunmai","device_address":"AA:BB:CC:DD:EE:01"}"""
        )
        val w = LocalReadings.weightFrom(hubWeight)
        assertEquals(80.0f, w.weightKg)
        assertNull(w.bmi)
        assertEquals(Instant.parse("2026-10-19T11:59:15Z"), w.timestamp)

        val hubBp = JSONObject("""{"systolic":121,"diastolic":79,"pulse":64,"timestamp":"2026-11-06T06:59:52-06:00"}""")
        val b = LocalReadings.bpFrom(hubBp)
        assertEquals(BloodPressureData(121, 79, 64, Instant.parse("2026-11-06T12:59:52Z"), "BP monitor", ""), b)
    }
}
