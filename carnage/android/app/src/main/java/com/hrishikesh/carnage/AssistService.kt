package com.hrishikesh.carnage

import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService

/**
 * Jarvis as the phone's assistant — the slot Gemini and Google Assistant sit
 * in. Chosen by him in Settings → Apps → Default apps → Digital assistant app;
 * Android does not let an app take it by asking.
 *
 * Once chosen, holding the power button (or swiping up from a bottom corner,
 * or the headset's long press) opens [AssistSession] over whatever is on the
 * screen, with that screen handed to her. This class only has to exist: the
 * system binds it to know the assistant is alive, and keeps the process warm.
 */
class AssistService : VoiceInteractionService() {

    override fun onReady() {
        super.onReady()
        instance = this
        // The brain is slow to wake (CPython, then her stores): start it now,
        // so the first long press is not the one that pays for it.
        Thread({ Brain.start(applicationContext) }, "carnage-assist-warm").start()
    }

    /**
     * Called up from the lock screen. Nothing of his may show there, so this
     * opens the app's own talk screen, which already knows how to sit over
     * the lock screen showing only her core.
     */
    override fun onShutdown() {
        instance = null
        super.onShutdown()
    }

    companion object {
        /** The running service, for bringing the sheet back after Hands works. */
        @Volatile var instance: AssistService? = null
    }

    override fun onLaunchVoiceAssistFromKeyguard() {
        startActivity(
            Intent(this, MainActivity::class.java)
                .setAction(MainActivity.ACTION_TALK)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}

/** Hands Android a new [AssistSession] each time he calls her up. */
class AssistSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = AssistSession(this)
}
