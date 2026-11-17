package com.uniscale.app.model

import com.uniscale.app.config.AppConfig
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CounterAndPlacesTest {

    @Test
    fun testCounterStateDefaultsAndCopy() {
        val counter = CounterState(dOut = 3, dIn = 1, totalDOut = 10, totalDIn = 5, date = "2026-11-14")
        assertEquals(3, counter.dOut)
        assertEquals(1, counter.dIn)
        assertEquals(10, counter.totalDOut)
        assertEquals(5, counter.totalDIn)
        assertEquals("2026-11-14", counter.date)

        val updated = counter.copy(dOut = counter.dOut + 1, dIn = (counter.dIn - 1).coerceAtLeast(0))
        assertEquals(4, updated.dOut)
        assertEquals(0, updated.dIn)
    }

    @Test
    fun testDwellPlaceState() {
        val place = DwellPlace(
            id = "place_20261113_1",
            date = "2026-11-13",
            avgLat = 37.7749,
            avgLon = -122.4194,
            confidenceRange = 65.0,
            startTime = "2026-11-13T09:00:00Z",
            endTime = "2026-11-13T10:30:00Z",
            durationSeconds = 5400L,
            dwellFormatted = "1h 30m",
            name = "Design Studio",
            candidatePois = listOf("Design Studio", "Cafe Nero"),
            isDeleted = false
        )

        assertEquals("place_20261113_1", place.id)
        assertEquals("Design Studio", place.name)
        assertEquals(65.0, place.confidenceRange, 0.001)
        assertEquals(2, place.candidatePois.size)
        assertFalse(place.isDeleted)

        val deleted = place.copy(isDeleted = true)
        assertTrue(deleted.isDeleted)
    }

    @Test
    fun testAppConfigGoogleMapsApiKey() {
        val config = AppConfig(
            hubUrl = "https://hub.example.com",
            googleMapsApiKey = "AIzaSyTestKey123"
        )
        val json = config.toJson()
        assertEquals("AIzaSyTestKey123", json.getString("google_maps_api_key"))

        val parsed = AppConfig.fromJson(json)
        assertEquals("AIzaSyTestKey123", parsed.googleMapsApiKey)
    }
}
