package com.hrishikesh.carnage

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import android.view.KeyEvent
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONArray

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
    private var buttons: MediaSession? = null
    private var wake: WakeListener? = null
    private var status = "Starting"

    // Keeps the notification's button saying the right thing.
    private val voice = object : LiveVoice.Listener {
        override fun onState(state: String) {
            update(status)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        goForeground()
        acquireWakeLock()
        LiveVoice.addListener(voice)
        Announcer.attach(this)
        listenToEarphoneButton()
        wake = WakeListener(this).also { it.start() }

        scope.launch {
            val ok = Brain.start(applicationContext)
            status = if (ok) "Jarvis is ready" else "Jarvis could not start"
            update(status)
            if (ok) heartbeat()
        }
    }

    /**
     * Every 15 s: fire timers and reminders that have come due, and carry out
     * work her other bodies handed this one. On the Pi the voice loop does
     * this; on the phone nothing else is awake to.
     */
    private suspend fun heartbeat() {
        while (scope.isActive) {
            val due = runCatching { JSONArray(Brain.tick()) }.getOrNull()
            if (due != null) {
                for (i in 0 until due.length()) {
                    val item = due.optJSONObject(i) ?: continue
                    alert(item.optString("kind"), item.optString("text"))
                }
            }
            delay(15_000)
        }
    }

    /** A timer or reminder: a notification that makes a sound, and her voice. */
    private fun alert(kind: String, text: String) {
        if (text.isBlank()) return
        val manager = getSystemService(NotificationManager::class.java)
        val title = if (kind == "timer") "Timer" else "Reminder"
        manager.notify(
            text.hashCode(),
            NotificationCompat.Builder(this, ALERTS)
                .setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_presence)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .build(),
        )
        Announcer.say(text)
    }

    /**
     * Foreground as a microphone service too, when allowed. Android 14 only
     * lets a service keep the mic with the screen off if it claimed it while
     * the app was on screen — so the app starts this service from its own
     * screen, and a start from boot (no mic allowed yet) falls back quietly.
     */
    private fun goForeground() {
        val first = notification(status)
        // The typed form only matters from Android 11, where the microphone
        // type exists; before that the manifest's types simply apply.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            startForeground(NOTIFICATION_ID, first)
            return
        }
        val base = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        } else {
            0
        }
        val micGranted = ContextCompat.checkSelfPermission(
            this, android.Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        val cameraGranted = ContextCompat.checkSelfPermission(
            this, android.Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED
        var types = base
        if (micGranted) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        // "What am I looking at?" with the screen off needs the camera type too.
        if (cameraGranted) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        try {
            startForeground(NOTIFICATION_ID, first, types)
        } catch (e: Exception) {
            Log.w("carnage", "no mic in the background yet — open the app once", e)
            startForeground(NOTIFICATION_ID, first, base)
        }
    }

    /**
     * The earphone's button starts and ends a conversation, as on the Pi.
     * Android sends media buttons to the app that last played sound, which a
     * voice call makes this one; while music plays elsewhere, that app keeps
     * the button.
     */
    private fun listenToEarphoneButton() {
        buttons = MediaSession(this, "carnage.buttons").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onMediaButtonEvent(intent: Intent): Boolean {
                    val key = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent::class.java)
                    } else {
                        @Suppress("DEPRECATION")
                        intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT)
                    } ?: return false
                    if (key.action != KeyEvent.ACTION_DOWN) return true
                    Log.i("carnage", "media button ${KeyEvent.keyCodeToString(key.keyCode)}")
                    // Only a real press. Bluetooth earbuds send PLAY/PAUSE on
                    // their own when audio starts — answering those hung up
                    // every call a second after it opened.
                    if (key.keyCode != KeyEvent.KEYCODE_HEADSETHOOK &&
                        key.keyCode != KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                    ) {
                        return false
                    }
                    if (LiveVoice.msSinceChange() < 2000) return true
                    LiveVoice.toggle(this@CarnageService)
                    return true
                }
            })
            setPlaybackState(
                PlaybackState.Builder()
                    .setActions(PlaybackState.ACTION_PLAY_PAUSE)
                    .setState(PlaybackState.STATE_PAUSED, 0, 1f)
                    .build()
            )
            isActive = true
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_TALK -> LiveVoice.toggle(this)
            // Sent from the app's own screen once the mic is granted: claiming
            // the microphone type now, while visible, is what lets her keep
            // hearing him after the screen goes off.
            ACTION_CLAIM_MIC -> goForeground()
        }
        // START_STICKY: if Android kills her under memory pressure she should
        // come back on her own. An assistant that stays dead until someone
        // notices is not one.
        return START_STICKY
    }

    override fun onDestroy() {
        LiveVoice.removeListener(voice)
        LiveVoice.stop()
        wake?.stop()
        Announcer.shutdown()
        runCatching { buttons?.release() }
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
        val talkIntent = Intent(this, CarnageService::class.java).setAction(ACTION_TALK)
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val talk = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            PendingIntent.getForegroundService(this, 1, talkIntent, flags)
        } else {
            PendingIntent.getService(this, 1, talkIntent, flags)
        }
        return NotificationCompat.Builder(this, CHANNEL)
            .addAction(
                R.drawable.ic_presence,
                getString(if (LiveVoice.active) R.string.hang_up else R.string.talk),
                talk,
            )
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_presence)
            .setContentIntent(open)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(Notification.CATEGORY_SERVICE)
            // Nothing private in it ("Jarvis is ready" and a Talk button), so
            // it may show in full on the lock screen — that is the point of it.
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
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
        private const val ALERTS = "carnage.alerts"
        private const val NOTIFICATION_ID = 1
        const val ACTION_TALK = "com.hrishikesh.carnage.TALK"
        private const val ACTION_CLAIM_MIC = "com.hrishikesh.carnage.CLAIM_MIC"

        /** Call from a visible screen after RECORD_AUDIO is granted. */
        fun claimMic(context: Context) {
            ensureChannel(context)
            val intent = Intent(context, CarnageService::class.java).setAction(ACTION_CLAIM_MIC)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

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
            if (manager.getNotificationChannel(ALERTS) == null) {
                manager.createNotificationChannel(
                    NotificationChannel(
                        ALERTS, "Timers and reminders", NotificationManager.IMPORTANCE_HIGH
                    ).apply { description = "When something you asked for comes due." }
                )
            }
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
