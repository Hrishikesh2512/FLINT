package com.hrishikesh.carnage

import android.content.Context
import android.util.Log
import com.chaquo.python.PyObject
import com.chaquo.python.Python

/**
 * The one place Kotlin talks to her.
 *
 * Everything real — memory, tools, the sync hub, the persona — is Python that
 * also runs on the Pi. This class is a door, not a brain: it hands over the
 * state directory and the bridge, then forwards text in and gets text back.
 * Keeping it this thin is what stops the phone growing its own half-copy of
 * the assistant.
 */
object Brain {

    private const val TAG = "carnage.brain"
    private var carnage: PyObject? = null

    @Synchronized
    fun start(context: Context): Boolean {
        if (carnage != null) return true
        return try {
            val module = Python.getInstance().getModule("carnage_android")
            // The Bridge object goes over as-is. Python cannot call a Java
            // object directly, so `carnage_android.start` wraps it in a
            // callable — the adaptation belongs on that side, where the shape
            // AndroidPhone expects is defined.
            carnage = module.callAttr(
                "start",
                context.filesDir.absolutePath,
                Bridge,
            )
            Log.i(TAG, "carnage started: " + carnage?.callAttr("describe")?.toString())
            true
        } catch (t: Throwable) {
            Log.e(TAG, "could not start carnage", t)
            false
        }
    }

    fun describe(): String =
        runCatching { carnage?.callAttr("describe")?.toString() }.getOrNull()
            ?: "not started"

    /** One turn of conversation. Blocking — callers move it off the main thread. */
    fun ask(said: String): String =
        runCatching { carnage?.callAttr("answer", said)?.toString() }
            .onFailure { Log.w(TAG, "ask failed", it) }
            .getOrNull()
            ?: "Something went wrong in my head just then."

    fun status(): String =
        runCatching { carnage?.callAttr("status")?.toString() }.getOrNull() ?: "{}"
}
