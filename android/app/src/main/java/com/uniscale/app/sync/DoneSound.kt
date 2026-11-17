package com.uniscale.app.sync

import android.content.Context
import android.content.Intent
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.provider.Settings

/** The "measurement done" sound: one of the phone's own ringtones, chosen in Settings. */
object DoneSound {
    // Scales and the BP2 can report one reading more than once (impedance, the ECG file after
    // its summary), so each kind plays at most once a minute.
    private const val QUIET_MS = 60_000L
    private val lastPlayed = mutableMapOf<String, Long>()

    fun play(context: Context, uri: String, kind: String) {
        if (uri.isBlank()) return
        val now = System.currentTimeMillis()
        synchronized(lastPlayed) {
            if (now - (lastPlayed[kind] ?: 0L) < QUIET_MS) return
            lastPlayed[kind] = now
        }
        runCatching { RingtoneManager.getRingtone(context, Uri.parse(uri))?.play() }
    }

    /** The sound's name for Settings: "Silent", or the ringtone's title. */
    fun title(context: Context, uri: String): String =
        if (uri.isBlank()) "Silent"
        else runCatching { RingtoneManager.getRingtone(context, Uri.parse(uri))?.getTitle(context) }.getOrNull() ?: "Default"

    fun pickerIntent(current: String): Intent = Intent(RingtoneManager.ACTION_RINGTONE_PICKER).apply {
        putExtra(RingtoneManager.EXTRA_RINGTONE_TYPE, RingtoneManager.TYPE_ALL)
        putExtra(RingtoneManager.EXTRA_RINGTONE_TITLE, "Measurement done sound")
        putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_SILENT, true)
        putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_DEFAULT, true)
        putExtra(RingtoneManager.EXTRA_RINGTONE_DEFAULT_URI, Settings.System.DEFAULT_NOTIFICATION_URI)
        putExtra(RingtoneManager.EXTRA_RINGTONE_EXISTING_URI, current.takeIf { it.isNotBlank() }?.let(Uri::parse))
    }

    /** The picker's result as a config value; no URI means Silent was picked. */
    fun picked(data: Intent?): String {
        val uri = if (Build.VERSION.SDK_INT >= 33) {
            data?.getParcelableExtra(RingtoneManager.EXTRA_RINGTONE_PICKED_URI, Uri::class.java)
        } else {
            @Suppress("DEPRECATION") data?.getParcelableExtra(RingtoneManager.EXTRA_RINGTONE_PICKED_URI)
        }
        return uri?.toString() ?: ""
    }
}
