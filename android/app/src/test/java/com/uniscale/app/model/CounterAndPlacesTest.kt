package com.uniscale.app.model

import com.uniscale.app.config.AppConfig
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CounterAndPlacesTest {

    @Test
    fun `the counter tile is built from the hub's counter set`() {
        val c = CounterState.fromJson(JSONObject("""{"title": "Swearing",
            "elements": [{"key": "f", "label": "F"}, {"key": "s", "label": "S"}],
            "date": "2026-11-17", "counts": {"f": 2, "s": 0}, "totals": {"f": 9, "s": 4},
            "daily_history": [{"date": "2026-11-17", "counts": {"f": 2, "s": 0}, "total": 2}]}"""))
        assertEquals("Swearing", c.title)
        assertEquals(listOf(CounterElement("f", "F"), CounterElement("s", "S")), c.elements)
        assertEquals(mapOf("f" to 2, "s" to 0), c.counts)
        assertEquals(9, c.totals["f"])
        assertEquals(CounterDailyRecord("2026-11-17", mapOf("f" to 2, "s" to 0), 2), c.dailyHistory.single())
    }

    @Test
    fun `no counter set up reads as no elements`() {
        val c = CounterState.fromJson(JSONObject("""{"title": null, "elements": [], "date": "2026-11-17", "counts": {}}"""))
        assertEquals(null, c.title)
        assertTrue(c.elements.isEmpty())
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
