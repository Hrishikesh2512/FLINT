package com.hrishikesh.carnage

import android.content.Context

/** The phone's own switches — things only the phone does, so not in carnage.json. */
object Prefs {
    private const val FILE = "carnage"
    private const val WAKE = "wake_word"
    private const val ANNOUNCE = "announce_messages"

    private fun prefs(context: Context) =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    /** Listen for "Hey Jarvis" in the background. */
    fun wakeWord(context: Context): Boolean = prefs(context).getBoolean(WAKE, true)
    fun setWakeWord(context: Context, on: Boolean) =
        prefs(context).edit().putBoolean(WAKE, on).apply()

    /** Read new messages aloud while a Bluetooth earphone is connected. */
    fun announce(context: Context): Boolean = prefs(context).getBoolean(ANNOUNCE, false)
    fun setAnnounce(context: Context, on: Boolean) =
        prefs(context).edit().putBoolean(ANNOUNCE, on).apply()
}
