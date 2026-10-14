package com.hrishikesh.carnage

import android.content.Context
import android.media.AudioAttributes
import android.speech.tts.TextToSpeech
import java.util.Locale

/**
 * Her voice for things that arrive on their own — a timer, a reminder, a
 * message — when no conversation is open to say them in.
 *
 * The phone's own text-to-speech rather than a Live call: an announcement is
 * one sentence, and opening a websocket to say it would cost seconds and
 * quota for nothing. Quiet while a call is live, so she never talks over
 * herself.
 */
object Announcer {

    @Volatile private var tts: TextToSpeech? = null
    @Volatile private var ready = false

    fun attach(context: Context) {
        if (tts != null) return
        tts = TextToSpeech(context.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                // Hinglish reads far better on an Indian English voice.
                tts?.language = Locale("en", "IN")
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                ready = true
            }
        }
    }

    fun say(text: String) {
        if (!ready || text.isBlank() || LiveVoice.active) return
        tts?.speak(text, TextToSpeech.QUEUE_ADD, null, text.hashCode().toString())
    }

    fun shutdown() {
        runCatching { tts?.shutdown() }
        tts = null
        ready = false
    }
}
