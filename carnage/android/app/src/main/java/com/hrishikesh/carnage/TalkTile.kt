package com.hrishikesh.carnage

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * A "Jarvis" tile in quick settings — reachable from the lock screen with one
 * swipe and one tap.
 *
 * A tap opens the talk screen rather than starting the mic from here: an
 * activity on screen is what Android accepts as "the user wants the mic now",
 * so it works even when the app's process had been stopped. The talk screen
 * shows over the lock screen and goes away again when the call ends.
 */
class TalkTile : TileService() {

    override fun onStartListening() {
        super.onStartListening()
        qsTile?.apply {
            state = if (LiveVoice.active) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
            label = getString(R.string.assistant)
            updateTile()
        }
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    override fun onClick() {
        super.onClick()
        if (LiveVoice.active) {
            LiveVoice.stop()
            onStartListening()
            return
        }
        val intent = Intent(this, MainActivity::class.java)
            .setAction(MainActivity.ACTION_TALK)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startActivityAndCollapse(
                PendingIntent.getActivity(this, 2, intent, PendingIntent.FLAG_IMMUTABLE)
            )
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}
