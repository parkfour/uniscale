package com.uniscale.app.sync

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.uniscale.app.MainActivity

/** The app's two notifications: supplement reminders and the chat assistant's replies. */
object Notifications {
    const val EXTRA_OPEN_CHAT = "open_chat"
    private const val CHANNEL_REMINDERS = "supplement_reminders"
    private const val CHANNEL_GEMMA = "dr_gemma"
    private const val ID_REMINDER = 1
    private const val ID_GEMMA = 2

    fun remind(context: Context, title: String, text: String) =
        post(context, CHANNEL_REMINDERS, "Supplement reminders", ID_REMINDER, title, text, openChat = false)

    fun assistantReplied(context: Context, name: String, text: String) =
        post(context, CHANNEL_GEMMA, "Assistant replies", ID_GEMMA, "$name replied", text, openChat = true)

    private fun post(
        context: Context, channel: String, channelName: String, id: Int,
        title: String, text: String, openChat: Boolean
    ) {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return
        context.getSystemService(NotificationManager::class.java)
            .createNotificationChannel(NotificationChannel(channel, channelName, NotificationManager.IMPORTANCE_DEFAULT))
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_OPEN_CHAT, openChat)
        val tap = PendingIntent.getActivity(
            context, id, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = NotificationCompat.Builder(context, channel)
            .setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(id, notification)
    }
}
