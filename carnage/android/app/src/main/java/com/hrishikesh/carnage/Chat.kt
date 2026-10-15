package com.hrishikesh.carnage

import android.animation.ValueAnimator
import android.content.Context
import android.text.method.LinkMovementMethod
import android.view.animation.LinearInterpolator
import android.widget.TextView
import io.noties.markwon.Markwon

/**
 * How her replies look, shared by the app and the assistant sheet so the two
 * never drift: formatting (bold, lists, code, links), and the reply appearing
 * a few words at a time.
 *
 * Normally her words stream in as the voice model says them, and [show]
 * formats the finished reply in place. [reveal] is for a reply that arrives
 * whole (nothing streamed): shown at reading pace rather than as a wall.
 */
object Chat {

    @Volatile private var markwon: Markwon? = null

    private fun markwon(context: Context): Markwon =
        markwon ?: Markwon.create(context.applicationContext).also { markwon = it }

    /** Her reply into [view], formatted, at once — after it streamed in. */
    fun show(view: TextView, markdown: String) {
        (view.tag as? ValueAnimator)?.cancel()
        view.movementMethod = LinkMovementMethod.getInstance()
        markwon(view.context).setMarkdown(view, markdown.trim())
    }

    /** Her reply into [view]: formatted, revealed word by word. */
    fun reveal(view: TextView, markdown: String, onGrow: () -> Unit = {}) {
        (view.tag as? ValueAnimator)?.cancel()
        val md = markwon(view.context)
        val spanned = md.toMarkdown(markdown.trim())
        view.movementMethod = LinkMovementMethod.getInstance()
        // Where each word ends: the reveal never cuts one in half.
        val ends = Regex("\\S+").findAll(spanned).map { it.range.last + 1 }.toList()
        if (ends.size < 4) {
            md.setParsedMarkdown(view, spanned)
            onGrow()
            return
        }
        val animator = ValueAnimator.ofInt(1, ends.size).apply {
            duration = (ends.size * 28L).coerceAtMost(1800L)
            interpolator = LinearInterpolator()
            var shown = 0
            addUpdateListener {
                val words = it.animatedValue as Int
                if (words != shown) {
                    shown = words
                    view.text = spanned.subSequence(0, ends[words - 1])
                    onGrow()
                }
            }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: android.animation.Animator) {
                    md.setParsedMarkdown(view, spanned)
                    onGrow()
                }
            })
        }
        view.tag = animator
        animator.start()
    }
}
