package com.hrishikesh.carnage

import android.app.assist.AssistStructure
import android.content.Context
import android.graphics.Bitmap
import android.text.InputType
import android.util.Base64
import android.view.View
import java.io.ByteArrayOutputStream

/**
 * What Android hands the assistant about the screen it was called up over,
 * turned into what the brain takes: the app's name, its text, a JPEG.
 *
 * Both halves only arrive when he has allowed them (Settings → Digital
 * assistant → "Use text from screen" / "Use screenshot", on by default), and
 * never for a screen the app has marked secure — banking apps, mostly. Either
 * can be missing; she then works from whatever did come through.
 */
object Screen {

    /** Enough characters for a long chat; the brain trims further. */
    private const val MAX_CHARS = 8000

    /** Wide enough to read on-screen text, small enough to send quickly. */
    private const val MAX_EDGE = 1024

    /** The app the screen belongs to, by the name he knows it by. */
    fun appName(context: Context, structure: AssistStructure?): String {
        val pkg = structure?.activityComponent?.packageName ?: return ""
        val pm = context.packageManager
        return runCatching {
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        }.getOrDefault(pkg)
    }

    /** Every visible piece of text on the screen, top to bottom. */
    fun text(structure: AssistStructure?): String {
        structure ?: return ""
        val out = StringBuilder()
        val domains = mutableSetOf<String>()
        for (i in 0 until structure.windowNodeCount) {
            walk(structure.getWindowNodeAt(i).rootViewNode, out, domains)
            if (out.length >= MAX_CHARS) break
        }
        return out.toString().take(MAX_CHARS).trim()
    }

    private fun walk(node: AssistStructure.ViewNode?, out: StringBuilder, domains: MutableSet<String>) {
        node ?: return
        if (out.length >= MAX_CHARS || node.visibility != View.VISIBLE) return
        if (!secret(node)) {
            val text = node.text?.toString()?.trim().orEmpty()
            // A button or icon with no text still says what it is this way.
            val label = text.ifEmpty { node.contentDescription?.toString()?.trim().orEmpty() }
            if (label.isNotEmpty()) out.append(label).append('\n')
            // Which site a browser is on, said once however many nodes carry it.
            node.webDomain?.let { if (domains.add(it)) out.append("(web page: $it)\n") }
        }
        for (i in 0 until node.childCount) walk(node.getChildAt(i), out, domains)
    }

    /** A password field is never read, even though Android would pass it on. */
    private fun secret(node: AssistStructure.ViewNode): Boolean {
        val variation = node.inputType and InputType.TYPE_MASK_VARIATION
        val klass = node.inputType and InputType.TYPE_MASK_CLASS
        if (klass == InputType.TYPE_CLASS_TEXT && variation in setOf(
                InputType.TYPE_TEXT_VARIATION_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
            )
        ) return true
        if (klass == InputType.TYPE_CLASS_NUMBER &&
            variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD
        ) return true
        return node.autofillHints?.any { it.contains("password", ignoreCase = true) } == true
    }

    /** The screenshot as base64 JPEG, scaled down; "" without one. */
    fun jpeg(bitmap: Bitmap?): String {
        bitmap ?: return ""
        return runCatching {
            val scale = MAX_EDGE.toFloat() / maxOf(bitmap.width, bitmap.height)
            val small = if (scale < 1f) {
                Bitmap.createScaledBitmap(
                    bitmap, (bitmap.width * scale).toInt(), (bitmap.height * scale).toInt(), true)
            } else bitmap
            // Hardware bitmaps cannot be compressed directly.
            val soft = if (small.config == Bitmap.Config.HARDWARE) {
                small.copy(Bitmap.Config.ARGB_8888, false)
            } else small
            val bytes = ByteArrayOutputStream()
            soft.compress(Bitmap.CompressFormat.JPEG, 80, bytes)
            Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)
        }.getOrDefault("")
    }
}
