package com.hrishikesh.carnage

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Reading the notification shade — the one thing the browser body cannot do
 * at all, and the reason "what did I miss?" is answerable on a real app.
 *
 * Optional by design: it needs a permission the user grants in a separate
 * settings screen, and until they do she simply has nothing to report. A
 * snapshot is taken on demand rather than kept as a running log, because a
 * transcript of every notification a phone ever showed is a surveillance
 * record, not a memory.
 */
class ShadeListener : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        instance = null
    }

    companion object {
        @Volatile
        private var instance: ShadeListener? = null

        /** What is in the shade right now, as plain maps for Python. */
        @JvmStatic
        fun snapshot(): List<Map<String, Any?>> {
            val live = instance ?: return emptyList()
            val active: Array<StatusBarNotification> =
                runCatching { live.activeNotifications }.getOrNull() ?: return emptyList()
            return active.mapNotNull { sbn ->
                val extras = sbn.notification?.extras ?: return@mapNotNull null
                val title = extras.getCharSequence("android.title")?.toString().orEmpty()
                val text = extras.getCharSequence("android.text")?.toString().orEmpty()
                if (title.isBlank() && text.isBlank()) return@mapNotNull null
                mapOf(
                    "app" to sbn.packageName,
                    "title" to title,
                    "text" to text,
                    "when" to (sbn.postTime / 1000.0),
                )
            }
        }
    }
}
