package com.hrishikesh.carnage

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.SystemClock

/**
 * Whether anything private to listen on is plugged in or paired.
 *
 * Jarvis only makes sound into headphones: her voice, the chime, spoken
 * announcements. Without them she stays silent and her replies appear as
 * text — the phone's loudspeaker is never used, so nothing she says is read
 * out to the room. Checked often, so the answer is cached for a moment.
 */
object Headphones {

    private val types = buildSet {
        add(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP)
        add(AudioDeviceInfo.TYPE_BLUETOOTH_SCO)
        add(AudioDeviceInfo.TYPE_WIRED_HEADSET)
        add(AudioDeviceInfo.TYPE_WIRED_HEADPHONES)
        add(AudioDeviceInfo.TYPE_USB_HEADSET)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            add(AudioDeviceInfo.TYPE_BLE_HEADSET)
        }
    }

    @Volatile private var cached = false
    @Volatile private var checkedAt = 0L

    fun connected(context: Context): Boolean {
        val now = SystemClock.elapsedRealtime()
        if (now - checkedAt > 1000) {
            cached = runCatching {
                context.getSystemService(AudioManager::class.java)
                    .getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                    .any { it.type in types }
            }.getOrDefault(false)
            checkedAt = now
        }
        return cached
    }
}
