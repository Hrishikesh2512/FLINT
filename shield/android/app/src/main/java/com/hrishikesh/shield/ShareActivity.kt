package com.hrishikesh.shield

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import kotlin.concurrent.thread

/**
 * Share → Shield from any app. Files, photos and text land in the Inbox, to
 * be sorted later; a shared web address becomes a link. No screen of its own:
 * a toast, and he is back where he was.
 */
class ShareActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Store.init(this)
        val share = intent
        val inbox = Store.resolve("/${Store.INBOX}")?.id
        thread {
            var saved = 0
            streams(share).forEach { if (Importer.file(this, it, inbox, by = "you") != null) saved++ }
            share.getStringExtra(Intent.EXTRA_TEXT)?.trim()?.takeIf { it.isNotEmpty() }?.let { text ->
                val subject = share.getStringExtra(Intent.EXTRA_SUBJECT).orEmpty()
                if (Regex("^https?://\\S+$").matches(text)) {
                    val name = subject.ifBlank { Uri.parse(text).host?.removePrefix("www.") ?: text }
                    Store.create(inbox, Kind.LINK, name, body = text, by = "you")
                } else {
                    Store.create(inbox, Kind.NOTE, subject.ifBlank { text.lineSequence().first().take(60) },
                        body = text, by = "you")
                }
                saved++
            }
            runOnUiThread {
                Ui.toast(this, if (saved > 0) getString(R.string.saved_to_inbox) else "Nothing to save")
                finish()
            }
        }
    }

    private fun streams(intent: Intent): List<Uri> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                ?: listOfNotNull(intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java))
        } else {
            @Suppress("DEPRECATION")
            runCatching { intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) }.getOrNull()
                ?: listOfNotNull(intent.getParcelableExtra(Intent.EXTRA_STREAM) as? Uri)
        }
}
