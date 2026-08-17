package com.hrishikesh.carnage

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * What makes her an assistant rather than an app you have to open.
 *
 * Android suspends a normal process within minutes of the screen going off. A
 * foreground service is the only supported way to say "this must keep
 * running", and it is the difference between the other devices finding a hub
 * to sync with and finding nothing. The persistent notification is the price
 * and is not hidden — a thing that runs all day should be visible while it
 * does.
 *
 * The wake lock is deliberately partial: the CPU stays available, the screen
 * does not. She needs to think and answer the network, not light up a phone in
 * someone's pocket.
 */
class CarnageService : android.app.Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, notification("Starting up…"))
        acquireWakeLock()

        scope.launch {
            val ok = Brain.start(applicationContext)
            update(if (ok) Brain.describe() else "Could not start — check the logs")
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // START_STICKY: if Android kills her under memory pressure she should
        // come back on her own. An assistant that stays dead until someone
        // notices is not one.
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        runCatching { wakeLock?.takeIf { it.isHeld }?.release() }
        super.onDestroy()
    }

    // ── the visible part ────────────────────────────────────────────────────
    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_presence)
            .setContentIntent(open)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
    }

    private fun update(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification(text))
    }

    private fun acquireWakeLock() {
        val power = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "carnage:alive")
            .apply { setReferenceCounted(false); acquire() }
    }

    companion object {
        private const val CHANNEL = "carnage.presence"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            ensureChannel(context)
            val intent = Intent(context, CarnageService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        private fun ensureChannel(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL) != null) return
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL, "Presence", NotificationManager.IMPORTANCE_LOW
                ).apply {
                    description = "Shown while she is running."
                    setShowBadge(false)
                }
            )
        }
    }
}

/** Brings her back after a reboot. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            Log.i("carnage", "boot completed — starting")
            CarnageService.start(context)
        }
    }
}
