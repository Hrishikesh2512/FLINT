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
    /**
     * One turn of conversation. Blocking — callers move it off the main
     * thread. [itemsJson] is what goes with it ([{"name","mime","text"|
     * "data"}]: a screenshot, a photo, a PDF, shared text); [onWords] hears
     * her reply so far, on a background thread, as the voice model says it.
     */
    fun ask(
        said: String,
        context: String = "",
        itemsJson: String = "[]",
        onWords: ((String) -> Unit)? = null,
    ): String =
        runCatching {
            carnage?.callAttr("answer", said, context, itemsJson, onWords?.let(::Words))
                ?.toString()
        }
            .onFailure { Log.w(TAG, "ask failed", it) }
            .getOrNull()
            ?: "Something went wrong in my head just then."

    /**
     * His screen's app and text as context for [ask]; the screenshot itself
     * goes to [ask] as an item. "" if nothing came through.
     */
    fun readScreen(app: String, text: String, jpegB64: String): String =
        runCatching { carnage?.callAttr("read_screen", app, text, jpegB64)?.toString() }
            .onFailure { Log.w(TAG, "read screen failed", it) }
            .getOrNull()
            .orEmpty()

    /**
     * A WhatsApp "Export chat" text, filed in her memory. Her sentence about
     * it, or null when the text is not an export (then it is an ordinary
     * attachment). Blocking: a long chat takes a moment to file.
     */
    fun importWhatsApp(text: String, fileName: String, subject: String): String? =
        runCatching {
            val result = org.json.JSONObject(
                carnage?.callAttr("import_whatsapp", text, fileName, subject)?.toString() ?: "{}")
            if (result.optBoolean("imported")) result.optString("say") else null
        }.getOrNull()

    fun status(): String =
        runCatching { carnage?.callAttr("status")?.toString() }.getOrNull() ?: "{}"

    /** True until she has a key to think with. */
    fun needsSetup(): Boolean =
        runCatching { carnage?.callAttr("needs_setup")?.toBoolean() }.getOrNull() ?: true

    /** What Venom needs in order to sync with this phone, as JSON. */
    fun pairing(): String =
        runCatching { carnage?.callAttr("pairing")?.toString() }.getOrNull() ?: "{}"

    /** What has come due, as a JSON array; also runs relayed work. Blocking. */
    fun tick(): String =
        runCatching { carnage?.callAttr("tick")?.toString() }.getOrNull() ?: "[]"

    // ── the live voice call: Kotlin carries the audio, Python is the brain ──
    /** The Live API websocket address with the key on it; empty with no key. */
    fun liveUrl(): String =
        runCatching { carnage?.callAttr("live_url")?.toString() }.getOrNull().orEmpty()

    /** The `setup` frame — her prompt and every tool — as JSON. Blocking. */
    fun liveSetup(): String? =
        runCatching { carnage?.callAttr("live_setup")?.toString() }
            .onFailure { Log.w(TAG, "live setup failed", it) }
            .getOrNull()

    /** A `toolCall` frame in; {"response": toolResponse, "end": bool} out. Blocking. */
    fun liveTool(message: String): String? =
        runCatching { carnage?.callAttr("live_tool", message)?.toString() }
            .onFailure { Log.w(TAG, "live tool failed", it) }
            .getOrNull()

    fun liveRecord(who: String, text: String) {
        runCatching { carnage?.callAttr("live_record", who, text) }
    }

    /** The call is over: file it in the shared archive. */
    fun liveFinish() {
        runCatching { carnage?.callAttr("live_finish") }
    }

    /** Save the setup screen's answers. Rebuilds her; blocking. */
    fun configure(userName: String, apiKey: String): String =
        runCatching { carnage?.callAttr("configure", userName, apiKey)?.toString() }
            .onFailure { Log.w(TAG, "configure failed", it) }
            .getOrNull()
            ?: "Could not save that."
}

/**
 * Her reply so far, handed from Python as it arrives. A plain public class,
 * not a lambda, so Chaquopy can always find and call `words` on it.
 */
class Words(private val deliver: (String) -> Unit) {
    fun words(text: String) = deliver(text)
}
