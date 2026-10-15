package com.hrishikesh.carnage

import android.app.Notification
import android.app.RemoteInput
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.media.AudioDeviceInfo
import android.media.AudioManager
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

    /**
     * Reads a new message aloud — only while a Bluetooth earphone is in, the
     * way Venom announces on the wearable. Out loud on a phone speaker would
     * be reading his messages to the room.
     */
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val note = sbn.notification ?: return
        if (!Prefs.announce(this) || sbn.packageName == packageName) return
        if (sbn.isOngoing || note.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        val isMessage = note.category == Notification.CATEGORY_MESSAGE ||
            sbn.packageName in MESSENGERS
        if (!isMessage || !earphoneIn()) return
        val title = note.extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = note.extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        if (text.isBlank()) return
        val said = "${sbn.packageName}|$title|$text"
        if (said == lastSaid) return              // apps re-post the same message
        lastSaid = said
        Announcer.say("Message from $title: ${text.take(160)}")
    }

    private fun earphoneIn(): Boolean =
        getSystemService(AudioManager::class.java)
            .getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .any {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
            }

    companion object {
        @Volatile
        private var instance: ShadeListener? = null
        private var lastSaid = ""

        private val MESSENGERS = setOf(
            "com.whatsapp", "com.whatsapp.w4b", "org.telegram.messenger",
            "com.google.android.apps.messaging", "com.samsung.android.messaging",
            "com.instagram.android",
        )

        /** The connected listener, when notification access is granted. */
        fun live(): ShadeListener? = instance

        /**
         * Answers the newest message from [who] through its notification's
         * own reply box, as the shade's Reply button does. A status map for
         * Python: sent, no_reply_box (the app offers none), or not_found;
         * null without notification access.
         */
        @JvmStatic
        fun reply(who: String, text: String): Map<String, Any?>? {
            val live = instance ?: return null
            val wanted = who.trim().lowercase()
            val active = runCatching { live.activeNotifications }.getOrNull() ?: return null
            val match = active
                .filter { it.packageName != live.packageName }
                .sortedByDescending { it.postTime }
                .firstOrNull { sbn ->
                    val extras = sbn.notification?.extras ?: return@firstOrNull false
                    listOf(Notification.EXTRA_TITLE, Notification.EXTRA_CONVERSATION_TITLE)
                        .mapNotNull { extras.getCharSequence(it)?.toString()?.lowercase() }
                        .any { wanted.isNotEmpty() && wanted in it }
                }
                ?: return mapOf("status" to "not_found")
            val note = match.notification
            val title = note.extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: who
            val app = runCatching {
                live.packageManager.getApplicationLabel(
                    live.packageManager.getApplicationInfo(match.packageName, 0)).toString()
            }.getOrDefault(match.packageName)
            val action = note.actions?.filter { !it.remoteInputs.isNullOrEmpty() }
                ?.let { replies ->
                    replies.firstOrNull {
                        Build.VERSION.SDK_INT >= Build.VERSION_CODES.P &&
                            it.semanticAction == Notification.Action.SEMANTIC_ACTION_REPLY
                    } ?: replies.firstOrNull()
                }
                ?: return mapOf("status" to "no_reply_box", "to" to title, "app" to app)
            val fill = Intent()
            val answers = Bundle()
            action.remoteInputs.forEach { answers.putCharSequence(it.resultKey, text) }
            RemoteInput.addResultsToIntent(action.remoteInputs, fill, answers)
            action.actionIntent.send(live, 0, fill)
            return mapOf("status" to "sent", "to" to title, "app" to app)
        }

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
