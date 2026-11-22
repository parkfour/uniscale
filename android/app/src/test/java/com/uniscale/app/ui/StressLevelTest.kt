package com.uniscale.app.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class StressLevelTest {
    @Test
    fun `stress falls in Garmin's bands`() {
        assertEquals(listOf("rest", "rest", "low", "low", "medium", "medium", "high", "high"),
            listOf(0, 25, 26, 50, 51, 75, 76, 100).map(::stressLevel))
    }
}
