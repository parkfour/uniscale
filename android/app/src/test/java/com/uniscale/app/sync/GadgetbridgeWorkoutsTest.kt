package com.uniscale.app.sync

import com.uniscale.app.model.HrZone
import com.uniscale.app.model.WorkoutSplit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

class GadgetbridgeWorkoutsTest {

    private val start = Instant.parse("2026-01-15T15:00:00Z").toEpochMilli()

    // Synthetic SUMMARY_DATA in Gadgetbridge's {"key": {"unit": ..., "value": ...}} shape.
    private val summary = """
        {"activeSeconds": {"unit": "seconds", "value": 1800.5},
         "distanceMeters": {"unit": "meters", "value": 5000.0},
         "steps": {"unit": "steps_unit", "value": 5200.0},
         "caloriesBurnt": {"unit": "calories_unit", "value": 350},
         "averageHR": {"unit": "bpm", "value": 150}, "maxHR": {"unit": "bpm", "value": 170},
         "averageCadence": {"unit": "spm", "value": 165.4},
         "activityTotalAscent": {"unit": "meters", "value": 25},
         "average_power": {"unit": "unit_watt", "value": 230}, "max_power": {"unit": "unit_watt", "value": 300},
         "aerobicTrainingEffect": {"unit": "", "value": 3.2},
         "maximumOxygenUptake": {"unit": "unit_ml_kg_min", "value": 45.27},
         "recoveryTime": {"unit": "seconds", "value": 108000},
         "stepLengthAvg": {"unit": "unit_millimeter", "value": 1012.0},
         "ground_contact_time": {"unit": "unit_millisecond", "value": 242.0},
         "vertical_oscillation": {"unit": "unit_millimeter", "value": 84.0},
         "vertical_ratio": {"unit": "unit_percent", "value": 7.6},
         "stance_time_balance": {"unit": "unit_percent", "value": 49.8},
         "maxCadence": {"unit": "spm", "value": 181.0},
         "hrZoneWarmUp": {"unit": "seconds", "value": 20.0}, "hrZoneBound1": {"unit": "bpm", "value": 95},
         "hrZoneThreshold": {"unit": "seconds", "value": 1500.0}, "hrZoneBound4": {"unit": "bpm", "value": 150},
         "intervals_header": {"type": "tableRow", "isHeader": true, "columns": [
            {"unit": "string", "value": "#"}, {"unit": "string", "value": "Distance"},
            {"unit": "string", "value": "Pace"}, {"unit": "string", "value": "heart_rate"},
            {"unit": "string", "value": "pref_header_time"}]},
         "interval_1": {"type": "tableRow", "columns": [{"unit": "", "value": 1}, {"unit": "meters", "value": 1000.0},
            {"unit": "seconds_km", "value": 360.0}, {"unit": "bpm", "value": 145}, {"unit": "seconds", "value": 360.0}]},
         "interval_2": {"type": "tableRow", "columns": [{"unit": "", "value": 2}, {"unit": "meters", "value": 500.0},
            {"unit": "seconds_km", "value": 340.0}, {"unit": "bpm", "value": 158}, {"unit": "seconds", "value": 170.0}]}}
    """.trimIndent()

    @Test
    fun `run summary is mapped`() {
        val w = GadgetbridgeWorkouts.fromSummary(start, start + 1_900_000, 16, "Run", summary)
        assertEquals("running", w.sport)
        assertEquals(Instant.ofEpochMilli(start), w.startTime)
        assertEquals(1800.5, w.durationSeconds, 0.0)
        assertEquals(5000.0, w.distanceMeters, 0.0)
        assertEquals(5200, w.steps)
        assertEquals(350, w.calories)
        assertEquals(150, w.avgHeartRate)
        assertEquals(170, w.maxHeartRate)
        assertEquals(83, w.avgCadence) // 165.4 steps/min -> strides/min
        assertEquals(25.0, w.elevationGainM)
        assertEquals(230, w.avgPower)
        assertEquals(3.2, w.aerobicTrainingEffect)
        assertEquals(45.3, w.vo2max)
        assertEquals(GadgetbridgeNights.DEVICE_NAME, w.deviceName)
        assertEquals(30.0, w.extras["recovery_time_hours"])
        assertEquals(1.01, w.extras["stride_length_m"])
        assertEquals(242.0, w.extras["ground_contact_ms"])
        assertEquals(84.0, w.extras["vertical_oscillation_mm"])
        assertEquals(7.6, w.extras["vertical_ratio_percent"])
        assertEquals(49.8, w.extras["ground_contact_balance"])
        assertEquals(91.0, w.extras["max_cadence"]) // 181 steps/min -> strides/min
        assertEquals(listOf(HrZone(1, "Warm up", 95, 20.0), HrZone(4, "Threshold", 150, 1500.0)), w.hrZones)
        assertEquals(listOf(WorkoutSplit(1000.0, 360.0, 145), WorkoutSplit(500.0, 170.0, 158)), w.splits)
    }

    @Test
    fun `unknown kind falls back to the name and missing summary to the times`() {
        val w = GadgetbridgeWorkouts.fromSummary(start, start + 600_000, 999, "Yoga", null)
        assertEquals("yoga", w.sport)
        assertEquals(600.0, w.durationSeconds, 0.0)
        assertNull(w.avgHeartRate)
    }
}
