package com.uniscale.app.sync

import com.uniscale.app.config.AppConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.LocalDateTime

class SupplementReminderWorkerTest {
    private val times = AppConfig.DEFAULT_SLOT_TIMES

    @Test
    fun `next slot is the first one after now today`() {
        val now = LocalDateTime.of(2026, 1, 1, 12, 0)
        assertEquals("lunch" to LocalDateTime.of(2026, 1, 1, 12, 30), SupplementReminderWorker.nextSlot(times, now))
    }

    @Test
    fun `after the last slot it wraps to tomorrow's first`() {
        val now = LocalDateTime.of(2026, 1, 1, 22, 1)
        assertEquals("wake" to LocalDateTime.of(2026, 1, 2, 7, 0), SupplementReminderWorker.nextSlot(times, now))
    }

    @Test
    fun `a slot exactly at now is not picked again`() {
        val now = LocalDateTime.of(2026, 1, 1, 8, 0)
        assertEquals("lunch", SupplementReminderWorker.nextSlot(times, now)?.first)
    }

    @Test
    fun `unparseable times are skipped and none at all gives null`() {
        val now = LocalDateTime.of(2026, 1, 1, 9, 0)
        assertEquals("dinner", SupplementReminderWorker.nextSlot(mapOf("lunch" to "noon", "dinner" to "18:30"), now)?.first)
        assertNull(SupplementReminderWorker.nextSlot(mapOf("lunch" to "noon"), now))
    }
}
