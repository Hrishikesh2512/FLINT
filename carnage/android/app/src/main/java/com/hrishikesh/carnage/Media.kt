package com.hrishikesh.carnage

import android.app.SearchManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Bundle
import android.os.SystemClock
import android.provider.MediaStore
import android.util.Log
import android.view.KeyEvent
import kotlin.math.roundToInt

/**
 * Music on the phone — what Venom does with mpv, done by his own music app.
 *
 * "Play X" prefers asking a music app's *running* media session to search and
 * play, because that works with the screen off; Android refuses to open
 * another app's screen from the background. Only when no player is running is
 * the app opened by intent, which works while Carnage is on screen.
 *
 * Reading other apps' media sessions needs notification access — the same
 * grant "what did I miss?" already asks for — so both features arrive together.
 */
object Media {

    private const val TAG = "carnage.media"

    /** Preferred players, best first. Any app that accepts a search will do. */
    private val PLAYERS = listOf(
        "com.google.android.apps.youtube.music",
        "com.spotify.music",
        "com.jio.media.jiobeats",
        "com.gaana",
        "com.wynk.music",
        "com.amazon.mp3",
        "com.google.android.youtube",
    )

    fun play(context: Context, query: String): String? {
        if (query.isBlank()) return null
        controllers(context)
            .filter { it.packageName in PLAYERS || canSearch(it) }
            .sortedBy { PLAYERS.indexOf(it.packageName).let { i -> if (i < 0) 99 else i } }
            .firstOrNull()
            ?.let { controller ->
                controller.transportControls.playFromSearch(query, Bundle())
                return label(context, controller.packageName)
            }

        val intent = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH).apply {
            putExtra(SearchManager.QUERY, query)
            putExtra(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        val pm = context.packageManager
        val target = PLAYERS.firstOrNull { pkg ->
            pm.queryIntentActivities(Intent(intent).setPackage(pkg), 0).isNotEmpty()
        }
        if (target != null) intent.setPackage(target)
        else if (intent.resolveActivity(pm) == null) return null
        return runCatching {
            context.startActivity(intent)
            label(context, target ?: intent.resolveActivity(pm).packageName)
        }.onFailure { Log.w(TAG, "could not open a player", it) }.getOrNull()
    }

    /** pause / resume / next / previous / stop on whatever is playing. */
    fun control(context: Context, action: String): Boolean {
        val playing = controllers(context).firstOrNull {
            it.playbackState?.state == PlaybackState.STATE_PLAYING
        } ?: controllers(context).firstOrNull { it.packageName != context.packageName }
        if (playing != null) {
            val controls = playing.transportControls
            when (action) {
                "pause" -> controls.pause()
                "resume" -> controls.play()
                "next" -> controls.skipToNext()
                "previous" -> controls.skipToPrevious()
                "stop" -> controls.stop()
                else -> return false
            }
            return true
        }
        // No access to sessions: a media key reaches whatever plays. PLAY and
        // PAUSE (never PLAY_PAUSE) so Carnage's own earphone-button session,
        // which only answers PLAY_PAUSE and the headset hook, ignores them.
        val code = when (action) {
            "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
            "resume" -> KeyEvent.KEYCODE_MEDIA_PLAY
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> return false
        }
        val audio = context.getSystemService(AudioManager::class.java)
        val now = SystemClock.uptimeMillis()
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0))
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, code, 0))
        return true
    }

    /** Set the media volume to [percent] (0-100), or read it when negative. */
    fun volume(context: Context, percent: Int): Int {
        val audio = context.getSystemService(AudioManager::class.java)
        val max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        if (percent >= 0) {
            val level = (max * percent.coerceIn(0, 100) / 100.0).roundToInt()
            audio.setStreamVolume(AudioManager.STREAM_MUSIC, level, 0)
        }
        val now = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
        return if (max == 0) 0 else (now * 100.0 / max).roundToInt()
    }

    private fun controllers(context: Context): List<MediaController> = runCatching {
        context.getSystemService(MediaSessionManager::class.java)
            .getActiveSessions(ComponentName(context, ShadeListener::class.java))
    }.getOrElse {
        // SecurityException until notification access is granted.
        emptyList()
    }

    private fun canSearch(controller: MediaController): Boolean =
        (controller.playbackState?.actions ?: 0L) and
            PlaybackState.ACTION_PLAY_FROM_SEARCH != 0L

    private fun label(context: Context, pkg: String): String = runCatching {
        val pm = context.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault("your music app")
}
