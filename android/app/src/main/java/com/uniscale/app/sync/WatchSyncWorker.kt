package com.uniscale.app.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.uniscale.app.config.AppConfigManager
import java.util.concurrent.TimeUnit

/**
 * Background hub sync while the app is closed: the same uploads as opening the app. Sends the
 * Gadgetbridge auto-export (sleep, workouts, steps and distance), the scale and BP readings this
 * phone took (LocalReadings) and queued supplement ticks. Runs as a WorkManager job, so there's no
 * persistent notification. Uploads already sent are skipped by HubSyncManager, so each run only
 * sends what's new; failed ones aren't marked synced and are retried next run.
 */
class WatchSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val configManager = AppConfigManager(applicationContext)
        if (!configManager.config.value.autoUploadHub) return Result.success()
        val hub = HubSyncManager(applicationContext, configManager)

        // Null when no export file is picked yet or it's unreadable; the app sorts that out.
        val gadgetbridge = GadgetbridgeExport(applicationContext)
        val watch = gadgetbridge.read()
        watch?.nights?.forEach { hub.uploadSleepSession(it) }
        watch?.workouts?.forEach { hub.uploadWorkout(it) }
        watch?.activity?.forEach { hub.uploadWatchActivity(it) }
        watch?.heartRate?.let { hub.uploadHeartRate(it) }

        val readings = LocalReadings(applicationContext)
        readings.weights().forEach { hub.uploadWeight(it) }
        readings.bloodPressures().forEach { hub.uploadBloodPressure(it) }

        SupplementProtocol(applicationContext, hub).refresh()
        return Result.success()
    }

    companion object {
        private const val WORK_NAME = "watch_sync"

        /** Settings slider stops, in minutes; 0 = off. 15 is WorkManager's minimum period. */
        val INTERVALS = listOf(0, 15, 30, 60, 120, 240)

        fun schedule(context: Context, minutes: Int) {
            val wm = WorkManager.getInstance(context)
            if (minutes <= 0) {
                wm.cancelUniqueWork(WORK_NAME)
                return
            }
            val request = PeriodicWorkRequestBuilder<WatchSyncWorker>(minutes.toLong(), TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            // UPDATE keeps the existing timing when the interval hasn't changed.
            wm.enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, request)
        }
    }
}
