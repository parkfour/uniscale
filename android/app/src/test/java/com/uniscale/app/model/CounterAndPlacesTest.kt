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
    fun `a counter formula's result is read with its daily values`() {
        val c = CounterState.fromJson(JSONObject("""{"title": "Fluid",
            "elements": [{"key": "in", "label": "In"}, {"key": "out", "label": "Out"}],
            "formula": "In-Out=Net", "computed": {"key": "net", "label": "Net", "value": -2, "total": 3.5},
            "date": "2026-11-20", "counts": {"in": 5, "out": 7}, "totals": {"in": 9, "out": 5.5},
            "daily_history": [{"date": "2026-11-20", "counts": {"in": 5, "out": 7}, "total": 12, "computed": -2},
                              {"date": "2026-11-19", "counts": {"in": 1, "out": 0}, "total": 1, "computed": null}]}"""))
        assertEquals("In-Out=Net", c.formula)
        assertEquals(CounterComputed("Net", -2.0, 3.5), c.computed)
        assertEquals(listOf(-2.0, null), c.dailyHistory.map { it.computed })
        assertEquals("-2", counterNumber(-2.0))
        assertEquals("1.5", counterNumber(1.5))
        assertEquals("--", counterNumber(null))
        // Without a formula there is nothing computed
        val plain = CounterState.fromJson(JSONObject("""{"title": "X", "elements": [], "formula": null, "computed": null}"""))
        assertEquals(null, plain.formula)
        assertEquals(null, plain.computed)
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

    @Test
    fun `patterns from the hub keep their lines in order`() {
        val json = JSONObject("""{"days": 90, "logged_days": 42, "patterns": [
            {"text": "More D(out) → higher next-day systolic BP (129.7 vs 124.1; r +0.52, 13 days)", "r": 0.52},
            {"text": "", "r": 0.4},
            {"text": "Going to Ambrosia → lower same-day diastolic BP (76 vs 84.5; r -0.62, 12 days)"}]}""")
        val p = com.uniscale.app.model.PatternsState.fromJson(json)
        assertEquals(42, p.loggedDays)
        assertEquals(2, p.lines.size)
        assertTrue(p.lines[0].startsWith("More D(out)"))
        assertEquals(0, com.uniscale.app.model.PatternsState.fromJson(JSONObject("{}")).loggedDays)
    }
}
