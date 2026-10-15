package com.hrishikesh.carnage

import android.content.ComponentName
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.SpeechRecognizer

/**
 * The speech recognizer an assistant is required to name.
 *
 * Android will not list an app as a digital assistant without one, and when
 * he picks Jarvis it also makes this the phone's default recognizer — the one
 * other apps get when they ask for voice input. Jarvis listens through the
 * Live API, not through this, so all it does is pass every request on to the
 * recognizer the phone already had (Google's, or the maker's), so choosing
 * Jarvis never breaks the mic button in some other app.
 */
class RecognizerProxy : RecognitionService() {

    private var inner: SpeechRecognizer? = null

    override fun onStartListening(intent: Intent, callback: RecognitionService.Callback) {
        inner?.destroy()
        val target = otherRecognizer()
        if (target == null) {
            callback.error(SpeechRecognizer.ERROR_CLIENT)
            return
        }
        inner = SpeechRecognizer.createSpeechRecognizer(this, target).apply {
            setRecognitionListener(Relay(callback))
            startListening(intent)
        }
    }

    override fun onStopListening(callback: RecognitionService.Callback) {
        inner?.stopListening()
    }

    override fun onCancel(callback: RecognitionService.Callback) {
        inner?.cancel()
    }

    override fun onDestroy() {
        inner?.destroy()
        inner = null
        super.onDestroy()
    }

    /** The first recognizer on the phone that is not this one. */
    private fun otherRecognizer(): ComponentName? =
        packageManager.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
            .map { it.serviceInfo }
            .firstOrNull { it.packageName != packageName }
            ?.let { ComponentName(it.packageName, it.name) }

    /** Everything the real recognizer says, straight back to the caller. */
    private class Relay(val callback: RecognitionService.Callback) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = run { callback.readyForSpeech(params ?: Bundle()) }
        override fun onBeginningOfSpeech() = callback.beginningOfSpeech()
        override fun onRmsChanged(rmsdB: Float) = callback.rmsChanged(rmsdB)
        override fun onBufferReceived(buffer: ByteArray?) = run { buffer?.let { callback.bufferReceived(it) }; Unit }
        override fun onEndOfSpeech() = callback.endOfSpeech()
        override fun onError(error: Int) = callback.error(error)
        override fun onResults(results: Bundle?) = callback.results(results ?: Bundle())
        override fun onPartialResults(partialResults: Bundle?) =
            callback.partialResults(partialResults ?: Bundle())
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
