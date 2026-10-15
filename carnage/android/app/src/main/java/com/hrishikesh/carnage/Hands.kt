package com.hrishikesh.carnage

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Jarvis's eyes and fingers in other apps — an accessibility service, the
 * one door Android has for an app to read another app's screen and touch it.
 *
 * Switched on only by him, in Settings → Accessibility; until then she says
 * she cannot use other apps rather than pretending. It does nothing on its
 * own: it never listens to what happens on screen, only looks when asked
 * (`look`) and acts when told (`act`), both from `carnage/hands.py`, which
 * holds the judgement and the rule that nothing which sends, pays, orders or
 * deletes is tapped without his yes.
 *
 * Password fields are listed as such and never typed into.
 */
class Hands : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "hands connected")
    }

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    // Deliberately deaf: nothing on screen is watched or kept between looks.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    companion object {
        private const val TAG = "carnage.hands"
        private const val MAX_ELEMENTS = 150
        private const val MAX_LABEL = 90

        @Volatile private var instance: Hands? = null

        /** What was on screen at the last look, by the number she was shown. */
        private val seen = mutableMapOf<Int, AccessibilityNodeInfo>()

        val connected: Boolean get() = instance != null

        /** The screen as a numbered list of things to touch; null while off. */
        @Synchronized
        fun look(): Map<String, Any?>? {
            val hands = instance ?: return null
            seen.clear()
            val root = hands.targetRoot()
                ?: return mapOf("app" to "Jarvis's own screen (open the app you need)",
                    "elements" to emptyList<Any>())
            val elements = mutableListOf<Map<String, Any?>>()
            walk(root, insideAction = false, elements)
            return mapOf("app" to hands.appName(root.packageName?.toString()), "elements" to elements)
        }

        /** One move. True if Android says it happened. */
        @Synchronized
        fun act(action: String, id: Int, text: String): Boolean {
            val hands = instance ?: return false
            val node = seen[id]?.also { runCatching { it.refresh() } }
            return when (action) {
                "tap" -> node != null && hands.tap(node)
                "type" -> node != null && hands.type(node, text)
                "scroll" -> hands.scroll(node, up = text == "up")
                "back" -> hands.performGlobalAction(GLOBAL_ACTION_BACK)
                "home" -> hands.performGlobalAction(GLOBAL_ACTION_HOME)
                "open_app" -> Actions.openApp(hands, text) != null
                else -> false
            }
        }

        private fun walk(
            node: AccessibilityNodeInfo,
            insideAction: Boolean,
            out: MutableList<Map<String, Any?>>,
        ) {
            if (out.size >= MAX_ELEMENTS || !node.isVisibleToUser) return
            val acts = node.isClickable || node.isEditable || node.isScrollable ||
                node.isCheckable || node.isLongClickable
            val own = ownLabel(node)
            // A tappable row is listed once, with the words inside it as its
            // label; plain text is listed on its own only outside such a row.
            if (acts || (own.isNotEmpty() && !insideAction)) {
                val label = if (acts && own.isEmpty()) innerLabel(node) else own
                val id = out.size + 1
                seen[id] = node
                out += mapOf(
                    "id" to id,
                    "kind" to kind(node),
                    "label" to label.take(MAX_LABEL),
                    "editable" to node.isEditable,
                    "scrollable" to node.isScrollable,
                    "checked" to (node.isCheckable && node.isChecked),
                    "selected" to node.isSelected,
                    "password" to node.isPassword,
                )
            }
            for (i in 0 until node.childCount) {
                val child = node.getChild(i) ?: continue
                walk(child, insideAction || (node.isClickable && !node.isScrollable), out)
            }
        }

        private fun ownLabel(node: AccessibilityNodeInfo): String {
            if (node.isPassword) return node.hintText?.toString()?.trim().orEmpty()
            val text = node.text?.toString()?.trim().orEmpty()
            val described = node.contentDescription?.toString()?.trim().orEmpty()
            val hint = node.hintText?.toString()?.trim().orEmpty()
            return text.ifEmpty { described }.ifEmpty { hint }
        }

        /** The words inside a tappable row that has none of its own. */
        private fun innerLabel(node: AccessibilityNodeInfo): String {
            val words = mutableListOf<String>()
            fun gather(n: AccessibilityNodeInfo, depth: Int) {
                if (depth > 4 || words.joinToString(" ").length > MAX_LABEL) return
                ownLabel(n).takeIf { it.isNotEmpty() }?.let { words += it }
                for (i in 0 until n.childCount) n.getChild(i)?.let { gather(it, depth + 1) }
            }
            for (i in 0 until node.childCount) node.getChild(i)?.let { gather(it, 0) }
            return words.joinToString(" · ")
        }

        private fun kind(node: AccessibilityNodeInfo): String {
            val cls = node.className?.toString()?.substringAfterLast('.').orEmpty()
            return when {
                node.isEditable -> "text field"
                cls.contains("Switch") || cls.contains("CheckBox") -> "switch"
                cls.contains("Button") -> "button"
                node.isScrollable -> "list"
                node.isClickable -> "button"
                cls.contains("Image") -> "image"
                else -> "text"
            }
        }
    }

    // ── which app to look at ────────────────────────────────────────────────
    /**
     * The app he is in — never Jarvis's own sheet or app, which may be on
     * top while she works.
     */
    private fun targetRoot(): AccessibilityNodeInfo? {
        val apps = runCatching { windows }.getOrDefault(emptyList())
            .filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
            .mapNotNull { window -> window.root?.let { window to it } }
            .filter { (_, root) -> root.packageName?.toString() != packageName }
        return (apps.firstOrNull { it.first.isActive || it.first.isFocused } ?: apps.firstOrNull())
            ?.second
            ?: rootInActiveWindow?.takeIf { it.packageName?.toString() != packageName }
    }

    private fun appName(pkg: String?): String {
        pkg ?: return "the screen"
        return runCatching {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
        }.getOrDefault(pkg)
    }

    // ── fingers ─────────────────────────────────────────────────────────────
    private fun tap(node: AccessibilityNodeInfo): Boolean {
        var target: AccessibilityNodeInfo? = node
        while (target != null && !target.isClickable) target = target.parent
        if (target?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true) return true
        // Some apps draw buttons that ignore the click action: touch it instead.
        val box = Rect().also { node.getBoundsInScreen(it) }
        if (box.isEmpty) return false
        return gesture(Path().apply { moveTo(box.exactCenterX(), box.exactCenterY()) }, 60)
    }

    private fun type(node: AccessibilityNodeInfo, text: String): Boolean {
        if (!node.isEditable || node.isPassword) return false
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun scroll(node: AccessibilityNodeInfo?, up: Boolean): Boolean {
        val list = node?.takeIf { it.isScrollable }
            ?: seen.values.filter { it.isScrollable }.maxByOrNull {
                Rect().also { r -> it.getBoundsInScreen(r) }.let { r -> r.width() * r.height() }
            }
        val action = if (up) AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        else AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        if (list?.performAction(action) == true) return true
        // Nothing scrollable said yes: swipe the middle of the screen.
        val size = resources.displayMetrics
        val x = size.widthPixels / 2f
        val (from, to) = if (up) size.heightPixels * 0.3f to size.heightPixels * 0.7f
        else size.heightPixels * 0.7f to size.heightPixels * 0.3f
        return gesture(Path().apply { moveTo(x, from); lineTo(x, to) }, 350)
    }

    private fun gesture(path: Path, millis: Long): Boolean {
        val done = CountDownLatch(1)
        var landed = false
        val stroke = GestureDescription.StrokeDescription(path, 0, millis)
        val sent = dispatchGesture(
            GestureDescription.Builder().addStroke(stroke).build(),
            object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) {
                    landed = true; done.countDown()
                }
                override fun onCancelled(gestureDescription: GestureDescription?) {
                    done.countDown()
                }
            },
            null,
        )
        if (!sent) return false
        done.await(2, TimeUnit.SECONDS)
        return landed
    }
}
