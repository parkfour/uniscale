package com.uniscale.app.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import androidx.core.content.ContextCompat
import kotlin.math.roundToInt

/**
 * Talks to Gadgetbridge's Intent API (Gadgetbridge → Settings → Developer options → Intent API).
 * Each piece needs its own toggle there; when one is off Gadgetbridge just ignores the broadcast.
 *  - refresh(): "Allow activity sync", "Broadcast on activity sync finish",
 *    "Allow database export", "Broadcast on database export".
 *  - setProfile(): "Allow 3rd party apps to change settings" (then restart Gadgetbridge).
 * Broadcasts are implicit (no package), so nightly builds get them too.
 */
class GadgetbridgeIntents(private val context: Context, onExported: () -> Unit) {

    private var lastRefresh = 0L

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            when (intent.action) {
                SYNC_FINISHED -> send(Intent(EXPORT_DB))
                EXPORT_DONE -> onExported()
            }
        }
    }

    fun register() = ContextCompat.registerReceiver(
        context, receiver,
        IntentFilter().apply { addAction(SYNC_FINISHED); addAction(EXPORT_DONE) },
        ContextCompat.RECEIVER_EXPORTED
    )

    fun unregister() = context.unregisterReceiver(receiver)

    /** Pull from the watch; when that finishes, export the database, then onExported runs. */
    fun refresh() {
        val now = System.currentTimeMillis()
        // ponytail: onResume also fires after permission dialogs, so skip repeats within a minute.
        if (now - lastRefresh < 60_000L) return
        lastRefresh = now
        send(Intent(ACTIVITY_SYNC))
    }

    /** Gadgetbridge's "About you" stores whole kg/cm as strings (its settings screen edits them as text). */
    fun setProfile(weightKg: Float, heightCm: Float) {
        setGlobal("activity_user_weight_kg", weightKg.roundToInt().toString())
        setGlobal("activity_user_height_cm", heightCm.roundToInt().toString())
    }

    private fun setGlobal(key: String, value: String) =
        send(Intent(SET_GLOBAL).putExtra("key", key).putExtra("value", value))

    private fun send(intent: Intent) = context.sendBroadcast(intent)

    private companion object {
        const val GB = "nodomain.freeyourgadget.gadgetbridge"
        const val ACTIVITY_SYNC = "$GB.command.ACTIVITY_SYNC"
        const val EXPORT_DB = "$GB.command.TRIGGER_DATABASE_EXPORT"
        const val SET_GLOBAL = "$GB.action.SET_GLOBAL_SETTING"
        const val SYNC_FINISHED = "$GB.action.ACTIVITY_SYNC_FINISH"
        const val EXPORT_DONE = "$GB.action.DATABASE_EXPORT_SUCCESS"
    }
}
