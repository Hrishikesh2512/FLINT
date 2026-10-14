package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Listens for "Hey Jarvis" and opens a conversation — the Pi's wake loop.
 *
 * Holds the microphone only while it is allowed to and nothing else needs it:
 * it lets go the moment a call starts (the call needs the mic, and on most
 * phones only one app may record at a time) and takes it back when the call
 * ends. Switched off in Set up, it holds nothing at all.
 */
class WakeListener(private val context: Context) {

    private val tag = "carnage.wake"
    @Volatile private var running = false
    private var thread: Thread? = null

    fun start() {
        if (running) return
        running = true
        thread = Thread(::loop, "carnage-wake").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running = false
        thread?.interrupt()
        thread = null
    }

    private fun loop() {
        val model = runCatching { WakeWord(context) }
            .onFailure { Log.e(tag, "wake word models unavailable", it) }
            .getOrNull() ?: return
        model.use { wake ->
            var mic: AudioRecord? = null
            val chunk = ShortArray(WakeWord.CHUNK)
            var hits = 0
            try {
                while (running) {
                    if (!allowed()) {
                        mic = release(mic)
                        sleep(500)
                        continue
                    }
                    if (mic == null) {
                        mic = open() ?: run { sleep(2000); null } ?: continue
                        wake.reset()
                        hits = 0
                    }
                    var filled = 0
                    while (filled < chunk.size && running) {
                        val read = mic.read(chunk, filled, chunk.size - filled)
                        if (read <= 0) break
                        filled += read
                    }
                    if (filled < chunk.size) continue
                    // Two chunks in a row over the line: one stray spike from a
                    // TV or a song should not open a call.
                    hits = if (wake.process(chunk) >= THRESHOLD) hits + 1 else 0
                    if (hits >= 2) {
                        Log.i(tag, "heard the wake word")
                        mic = release(mic)
                        LiveVoice.start(context)
                        sleep(1500)               // let the call take the mic
                    }
                }
            } finally {
                release(mic)
            }
        }
    }

    /** Listening is wanted, permitted, and not in the way of a call. */
    private fun allowed(): Boolean =
        Prefs.wakeWord(context) && !LiveVoice.active &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")        // checked in allowed()
    private fun open(): AudioRecord? = runCatching {
        val minimum = AudioRecord.getMinBufferSize(
            WakeWord.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION, WakeWord.RATE,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minimum, WakeWord.CHUNK * 2 * 4),
        ).takeIf { it.state == AudioRecord.STATE_INITIALIZED }
            ?.also { it.startRecording() }
    }.onFailure { Log.w(tag, "could not open the mic", it) }.getOrNull()

    private fun release(mic: AudioRecord?): AudioRecord? {
        runCatching { mic?.stop(); mic?.release() }
        return null
    }

    private fun sleep(ms: Long) {
        try {
            Thread.sleep(ms)
        } catch (_: InterruptedException) {
        }
    }

    companion object {
        /** The Pi listens at 0.30; a phone mic hears a room, so a little firmer. */
        private const val THRESHOLD = 0.5f
    }
}
