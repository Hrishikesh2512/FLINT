package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.AlarmClock
import android.provider.ContactsContract
import android.telecom.TelecomManager
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * The phone doing what his thumb does: the phone book, calls, alarms, the
 * torch, opening apps, Do Not Disturb.
 *
 * Calls and the torch work from anywhere, screen off included. Alarms and
 * opening apps start another app's screen, which Android only allows while
 * Carnage itself is on screen — so those say no honestly when it isn't, and
 * the Python side falls back (an alarm becomes Jarvis's own reminder).
 */
object Actions {

    private const val TAG = "carnage.actions"

    /** Set by MainActivity: true while Carnage is the app on screen. */
    @Volatile var onScreen = false

    /**
     * Whether Android will let her start another app's screen now. Besides
     * Carnage being on screen: the assistant sheet showing, or the Hands
     * accessibility service being on — Android exempts both from its
     * background-launch block, which otherwise drops the launch silently.
     */
    fun canLaunch(): Boolean = onScreen || AssistSession.showing || Hands.connected

    // ── the phone book ──────────────────────────────────────────────────────
    fun contacts(context: Context, query: String): List<Map<String, String>> {
        if (query.isBlank() || !granted(context, Manifest.permission.READ_CONTACTS)) {
            return emptyList()
        }
        val phone = ContactsContract.CommonDataKinds.Phone.CONTENT_URI
        val name = ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME
        val number = ContactsContract.CommonDataKinds.Phone.NUMBER
        val found = LinkedHashMap<String, Map<String, String>>()
        context.contentResolver.query(
            phone, arrayOf(name, number), "$name LIKE ?", arrayOf("%$query%"),
            "${ContactsContract.CommonDataKinds.Phone.IS_PRIMARY} DESC, $name ASC",
        )?.use { cursor ->
            while (cursor.moveToNext() && found.size < 8) {
                val who = cursor.getString(0).orEmpty()
                val digits = cursor.getString(1).orEmpty()
                // One entry per person: a contact saved with three copies of
                // the same number is still one choice.
                val key = who.lowercase()
                if (who.isNotBlank() && digits.isNotBlank() && key !in found) {
                    found[key] = mapOf("name" to who, "number" to digits)
                }
            }
        }
        // Names starting with what he said beat names merely containing it.
        return found.values.sortedBy {
            if (it.getValue("name").startsWith(query, ignoreCase = true)) 0 else 1
        }
    }

    // ── calls ───────────────────────────────────────────────────────────────
    @SuppressLint("MissingPermission")        // checked
    fun call(context: Context, number: String): Boolean {
        if (number.isBlank() || !granted(context, Manifest.permission.CALL_PHONE)) return false
        // TelecomManager places the call itself, so it works with the screen
        // off — an ACTION_CALL intent would need a visible app to start from.
        context.getSystemService(TelecomManager::class.java)
            .placeCall(Uri.fromParts("tel", number, null), Bundle())
        return true
    }

    // ── alarms ──────────────────────────────────────────────────────────────
    fun alarm(context: Context, hour: Int, minute: Int, label: String): Boolean {
        if (!canLaunch()) return false
        val intent = Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (label.isNotBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
        return runCatching { context.startActivity(intent); true }
            .onFailure { Log.w(TAG, "no clock app took the alarm", it) }
            .getOrDefault(false)
    }

    // ── the torch ───────────────────────────────────────────────────────────
    fun torch(context: Context, on: Boolean): Boolean {
        val cameras = context.getSystemService(CameraManager::class.java)
        val id = cameras.cameraIdList.firstOrNull {
            cameras.getCameraCharacteristics(it)
                .get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return false
        cameras.setTorchMode(id, on)
        return true
    }

    // ── apps ────────────────────────────────────────────────────────────────
    fun openApp(context: Context, name: String): String? {
        if (!canLaunch() || name.isBlank()) return null
        val pm = context.packageManager
        val launchers = pm.queryIntentActivities(
            Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0
        )
        val wanted = name.trim().lowercase()
        val best = launchers
            .map { it to it.loadLabel(pm).toString() }
            .filter { (info, _) -> info.activityInfo.packageName != context.packageName }
            .minByOrNull { (_, label) ->
                val l = label.lowercase()
                when {
                    l == wanted -> 0
                    l.startsWith(wanted) -> 1
                    wanted in l -> 2
                    l.replace(" ", "") == wanted.replace(" ", "") -> 1
                    else -> 9
                }
            }
            ?.takeIf { (_, label) ->
                val l = label.lowercase()
                l == wanted || l.startsWith(wanted) || wanted in l ||
                    l.replace(" ", "") == wanted.replace(" ", "")
            } ?: return null
        val launch = pm.getLaunchIntentForPackage(best.first.activityInfo.packageName)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) ?: return null
        return runCatching { context.startActivity(launch); best.second }.getOrNull()
            ?.also { AssistSession.launched() }
    }

    // ── getting somewhere ───────────────────────────────────────────────────
    /** Google Maps, already navigating; [mode] is d, w, b or l (two-wheeler). */
    fun navigate(context: Context, destination: String, mode: String): Boolean {
        if (!canLaunch() || destination.isBlank()) return false
        val how = mode.takeIf { it in setOf("d", "w", "b", "l") } ?: "d"
        val navigation = Intent(
            Intent.ACTION_VIEW,
            Uri.parse("google.navigation:q=${Uri.encode(destination)}&mode=$how"),
        ).setPackage("com.google.android.apps.maps").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        // No Google Maps: any maps app, showing the place instead.
        val anyMap = Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=${Uri.encode(destination)}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val opened = runCatching { context.startActivity(navigation); true }.getOrElse {
            runCatching { context.startActivity(anyMap); true }.getOrDefault(false)
        }
        if (opened) AssistSession.launched()
        return opened
    }

    // ── Do Not Disturb ──────────────────────────────────────────────────────
    fun doNotDisturb(on: Boolean): Boolean {
        // Notification access (which "what did I miss?" already needs) is
        // allowed to change the interruption filter.
        val listener = ShadeListener.live() ?: return false
        listener.requestInterruptionFilter(
            if (on) NotificationManager.INTERRUPTION_FILTER_PRIORITY
            else NotificationManager.INTERRUPTION_FILTER_ALL
        )
        return true
    }

    private fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED
}
