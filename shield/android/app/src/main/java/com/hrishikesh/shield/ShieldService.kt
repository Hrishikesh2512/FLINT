package com.hrishikesh.shield

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Keeps the API up: without a foreground service Android stops the app a few
 * minutes after the screen goes off, and his AIs would find Shield gone.
 */
class ShieldService : Service() {

    companion object {
        private const val CHANNEL = "shield"
        private const val ID = 1

        @Volatile var running = false
            private set

        fun start(context: Context) {
            val intent = Intent(context, ShieldService::class.java)
            runCatching {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
                else context.startService(intent)
            }.onFailure { Log.w("shield", "could not start the API service", it) }
        }
    }

    private var api: Api? = null
    private val refresh = { updateNotification() }
    private val scanner = android.os.HandlerThread("shield-autofill").apply { start() }
    private val scans = android.os.Handler(scanner.looper)

    /** New screenshots, downloads and documents, every few minutes. */
    private val scan = object : Runnable {
        override fun run() {
            runCatching { AutoFill.scan(this@ShieldService) }
            scans.postDelayed(this, 3 * 60 * 1000L)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Store.init(this)
        channel()
        val first = notification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(ID, first, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(ID, first)
        }
        api = Api(assets, { Token.get(this) }, { Token.owner(this) }).also {
            runCatching { it.start(5000, false) }
                .onFailure { e -> Log.e("shield", "the API could not start", e) }
        }
        running = api?.isAlive == true
        Store.listen(refresh)
        updateNotification()
        scans.postDelayed(scan, 20_000L)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        Store.unlisten(refresh)
        scans.removeCallbacksAndMessages(null)
        scanner.quitSafely()
        api?.stop()
        running = false
        super.onDestroy()
    }

    private fun updateNotification() {
        getSystemService(NotificationManager::class.java).notify(ID, notification())
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val where = Net.addresses().firstOrNull()?.second?.let { "$it:${Api.PORT}" } ?: "port ${Api.PORT}"
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_shield_small)
            .setContentTitle(getString(R.string.guarding, Store.count()))
            .setContentText(getString(R.string.api_at, where))
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun channel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel), NotificationManager.IMPORTANCE_MIN))
    }
}

/** Back after a restart, so his AIs can reach Shield without him opening it. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) ShieldService.start(context)
    }
}
