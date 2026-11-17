package com.uniscale.app.sync

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.uniscale.app.config.AppConfig
import com.uniscale.app.config.AppConfigManager
import java.time.Duration
import java.time.LocalDateTime
import java.util.concurrent.TimeUnit

/**
 * Fires at a slot's time (from Settings) and, if that slot still has unticked doses, posts a
 * reminder; then schedules itself for the next slot. One pending job at a time, no exact-alarm
 * permission, so Android may deliver it a few minutes late when the phone is dozing.
 */
class SupplementReminderWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val configManager = AppConfigManager(applicationContext)
        val config = configManager.config.value
        if (!config.supplementReminders) return Result.success()
        val slot = inputData.getString(KEY_SLOT)
        if (slot != null) {
            val protocol = SupplementProtocol(applicationContext, HubSyncManager(applicationContext, configManager))
            protocol.refresh()  // offline: fall back to the cached checklist and queued ticks
            val open = protocol.doses.value.filter { it.slot == slot && !it.taken }
            if (open.isNotEmpty()) {
                Notifications.remind(
                    applicationContext,
                    "${slot.replaceFirstChar { it.uppercase() }} supplements",
                    open.joinToString(", ") { "${it.name} ${it.dosage.fmt()}${it.unit}" }
                )
            }
        }
        // Step past this slot's minute so the same slot isn't picked again. Append, not replace:
        // replacing would cancel this still-running job.
        schedule(applicationContext, config, LocalDateTime.now().plusMinutes(1), ExistingWorkPolicy.APPEND_OR_REPLACE)
        return Result.success()
    }

    companion object {
        private const val WORK_NAME = "supplement_reminder"
        private const val KEY_SLOT = "slot"

        fun schedule(
            context: Context,
            config: AppConfig,
            now: LocalDateTime = LocalDateTime.now(),
            policy: ExistingWorkPolicy = ExistingWorkPolicy.REPLACE
        ) {
            val wm = WorkManager.getInstance(context)
            if (!config.supplementReminders) {
                wm.cancelUniqueWork(WORK_NAME)
                return
            }
            val (slot, at) = nextSlot(config.slotTimes, now) ?: return
            val request = OneTimeWorkRequestBuilder<SupplementReminderWorker>()
                .setInitialDelay(Duration.between(LocalDateTime.now(), at).toMillis().coerceAtLeast(0), TimeUnit.MILLISECONDS)
                .setInputData(workDataOf(KEY_SLOT to slot))
                .build()
            wm.enqueueUniqueWork(WORK_NAME, policy, request)
        }

        /** The first slot time after [now] (today, else tomorrow's earliest), or null if none parse. */
        fun nextSlot(slotTimes: Map<String, String>, now: LocalDateTime): Pair<String, LocalDateTime>? {
            val times = slotTimes.mapNotNull { (slot, t) -> AppConfig.parseSlotTime(t)?.let { slot to it } }
                .sortedBy { it.second }
            if (times.isEmpty()) return null
            times.firstOrNull { it.second > now.toLocalTime() }?.let { (slot, t) -> return slot to now.toLocalDate().atTime(t) }
            val (slot, t) = times.first()
            return slot to now.toLocalDate().plusDays(1).atTime(t)
        }

        private fun Double.fmt() = if (this % 1.0 == 0.0) toLong().toString() else toString()
    }
}
