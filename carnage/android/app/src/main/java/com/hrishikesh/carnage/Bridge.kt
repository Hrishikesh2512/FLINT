package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.BatteryManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.telephony.SmsManager
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * The Android half of the device seam.
 *
 * `carnage/platform.py` defines a `Phone` protocol and an `AndroidPhone` that
 * routes every call through a single callable. This is the other end of that
 * callable — one `call(method, kwargs)` entry point rather than a binding per
 * capability, and deliberately so: a method per skill would put every new tool
 * behind an app release, which is exactly the coupling the Python side exists
 * to avoid. Adding a skill stays a Python change.
 *
 * Everything here returns a value or null and never throws across the boundary.
 * A Java exception crossing into CPython arrives as something the voice loop
 * cannot sensibly handle, and the honest answer to "where am I" when location
 * is off is "I can't see that", not a stack trace.
 */
object Bridge {

    private const val TAG = "carnage.bridge"

    @Volatile
    private var appContext: Context? = null

    fun attach(context: Context) {
        appContext = context.applicationContext
    }

    /** Called from Python. Returns plain maps/primitives or null. */
    @JvmStatic
    fun call(method: String, args: Map<String, Any?>? = null): Any? {
        val context = appContext ?: return null
        val kwargs = args ?: emptyMap()
        return try {
            when (method) {
                "battery" -> battery(context)
                "locate" -> locate(context)
                "send_sms" -> sendSms(
                    context,
                    kwargs["to"]?.toString().orEmpty(),
                    kwargs["text"]?.toString().orEmpty(),
                )
                "notifications" -> ShadeListener.snapshot()
                "vibrate" -> vibrate(
                    context,
                    (kwargs["milliseconds"] as? Number)?.toLong() ?: 400L,
                )
                else -> {
                    Log.w(TAG, "unknown bridge method: $method")
                    null
                }
            }
        } catch (t: Throwable) {
            // Including Errors: a SecurityException from a revoked permission
            // is the common case and must read as "unavailable", not a crash.
            Log.w(TAG, "bridge $method failed", t)
            null
        }
    }

    // ── power ───────────────────────────────────────────────────────────────
    private fun battery(context: Context): Map<String, Any> {
        val manager = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val percent = manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val status = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val plugged = status?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val temperature = (status?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0
        return mapOf(
            "percent" to percent,
            "charging" to (plugged != 0),
            "temperature" to temperature,
        )
    }

    // ── where he is ─────────────────────────────────────────────────────────
    @SuppressLint("MissingPermission")
    private fun locate(context: Context): Map<String, Any>? {
        if (!granted(context, Manifest.permission.ACCESS_FINE_LOCATION) &&
            !granted(context, Manifest.permission.ACCESS_COARSE_LOCATION)
        ) {
            return null
        }
        val manager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager

        // Last known first: it is instant, and a fix from a minute ago is the
        // right answer to "where am I" while a fresh one would make her pause
        // mid-sentence. Freshness is enforced on the Python side, which drops
        // anything stale rather than speaking it.
        val best = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .mapNotNull { provider ->
                runCatching { manager.getLastKnownLocation(provider) }.getOrNull()
            }
            .maxByOrNull(Location::getTime)
            ?: return null

        return mapOf(
            "latitude" to best.latitude,
            "longitude" to best.longitude,
            "accuracy" to best.accuracy.toDouble(),
            "provider" to (best.provider ?: "android"),
        )
    }

    // ── messages that genuinely send ────────────────────────────────────────
    private fun sendSms(context: Context, to: String, text: String): Boolean {
        if (to.isBlank() || text.isBlank()) return false
        if (!granted(context, Manifest.permission.SEND_SMS)) {
            Log.i(TAG, "sms refused: permission not granted")
            return false
        }
        val manager =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                context.getSystemService(SmsManager::class.java)
            } else {
                @Suppress("DEPRECATION")
                SmsManager.getDefault()
            } ?: return false

        // Long messages have to be split or they are silently truncated, and a
        // truncated emergency message is worse than a failed one.
        val parts = manager.divideMessage(text)
        if (parts.size > 1) {
            manager.sendMultipartTextMessage(to, null, parts, null, null)
        } else {
            manager.sendTextMessage(to, null, text, null, null)
        }
        return true
    }

    // ── odds and ends ───────────────────────────────────────────────────────
    private fun vibrate(context: Context, milliseconds: Long): Boolean {
        val vibrator =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE)
                    as? VibratorManager)?.defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
            } ?: return false
        vibrator.vibrate(
            VibrationEffect.createOneShot(milliseconds, VibrationEffect.DEFAULT_AMPLITUDE)
        )
        return true
    }

    private fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED
}
