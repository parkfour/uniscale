package com.uniscale.app.sync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant
import java.time.ZoneId

class GadgetbridgeNightsTest {

    private fun ms(iso: String) = Instant.parse(iso).toEpochMilli()

    // Synthetic night shaped like Gadgetbridge's Garmin rows: start/stop events, stage changes, the score at
    // wake-up and the HRV summary ~40 min later.
    private val start = ms("2026-01-15T05:00:00Z")
    private val stop = ms("2026-01-15T12:00:00Z")
    private val events = listOf(GbSleepEvent(start, 0), GbSleepEvent(stop, 1))
    private val stages = listOf(
        start + 600_000 to 2,     // light from +10 min
        start + 3_000_000 to 3,   // deep
        start + 6_000_000 to 4,   // REM
        start + 8_000_000 to 1,   // awake
        start + 8_400_000 to 2    // light until the stop event
    )

    private fun build(zone: String = "America/Chicago") = GadgetbridgeNights.build(
        events = events,
        stages = stages,
        scores = listOf(stop to 72, ms("2026-01-14T12:00:00Z") to 50),
        hrv = listOf(GbHrvSummary(stop + 2_400_000, 38, 4)),
        spo2 = listOf(start + 60_000 to 95, start + 120_000 to 91, start + 180_000 to 0),
        respiration = listOf(start + 60_000 to 14.0f, start + 120_000 to 15.0f),
        zone = ZoneId.of(zone)
    )

    @Test
    fun `one night with stages, device score, HRV and vitals`() {
        val n = build().single()
        assertEquals(25_200L, n.durationSeconds)
        assertEquals(2_400L + 16_800L, n.lightSleepSeconds)
        assertEquals(3_000L, n.deepSleepSeconds)
        assertEquals(2_000L, n.remSleepSeconds)
        assertEquals(400L, n.awakeSeconds)
        assertEquals(72, n.deviceSleepScore)
        assertNull(n.estimatedSleepScore)
        assertEquals(38f, n.hrvRmssdMs)
        assertEquals("balanced", n.hrvStatus)
        assertEquals(93f, n.avgSpo2)
        assertEquals(91f, n.lowestSpo2)
        assertEquals(14.5f, n.avgRespiration)
        assertEquals("2026-01-15", n.date)
        assertEquals(GadgetbridgeNights.DEVICE_NAME, n.deviceName)
    }

    @Test
    fun `night is dated by the local wake-up day`() {
        // Wake-up at 12:00Z is 23:00 on Jan 15 in Sydney but 06:00 on Jan 15 in Chicago; at 14:00Z Sydney is Jan 16.
        val late = GadgetbridgeNights.build(
            listOf(GbSleepEvent(start, 0), GbSleepEvent(ms("2026-01-15T14:00:00Z"), 1)),
            stages, emptyList(), emptyList(), emptyList(), emptyList(), ZoneId.of("Australia/Sydney")
        )
        assertEquals("2026-01-16", late.single().date)
    }

    @Test
    fun `unfinished night is skipped`() {
        val nights = GadgetbridgeNights.build(
            listOf(GbSleepEvent(start, 0)), stages, emptyList(), emptyList(), emptyList(), emptyList(), ZoneId.of("UTC")
        )
        assertEquals(0, nights.size)
    }
}
