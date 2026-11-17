package com.uniscale.app.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class SignedDeltaTest {
    @Test
    fun `change shows its sign and rounds`() {
        assertEquals("−0.2", signed(-0.2f, 1))
        assertEquals("+3", signed(3f, 0))
        assertEquals("±0.0", signed(0.04f, 1))
        assertEquals("±0", signed(-0.3f, 0))
    }
}
