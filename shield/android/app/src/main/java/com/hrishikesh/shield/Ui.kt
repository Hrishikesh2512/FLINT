package com.hrishikesh.shield

import android.app.AlertDialog
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.text.InputType
import android.util.LruCache
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.Toast
import java.io.File

/** How things look and read across Shield's screens. */
object Ui {

    fun icon(item: Item): Int = when {
        item.kind == Kind.FOLDER && item.parent == null && item.name == Store.INBOX -> R.drawable.ic_inbox
        item.kind == Kind.FOLDER -> R.drawable.ic_folder
        item.kind == Kind.NOTE -> R.drawable.ic_note
        item.kind == Kind.LINK -> R.drawable.ic_link
        item.isImage -> R.drawable.ic_image
        item.isPdf -> R.drawable.ic_pdf
        else -> R.drawable.ic_file
    }

    fun tint(context: Context, item: Item): ColorStateList = ColorStateList.valueOf(context.getColor(
        when {
            item.kind == Kind.FOLDER -> R.color.shield
            item.byAi -> R.color.ai
            else -> R.color.ink
        }))

    /** "Jarvis" for "jarvis", "You" for him. */
    fun who(agent: String): String = when (agent) {
        "you" -> "You"
        "shield" -> "Shield"
        else -> agent.split(' ', '_', '-').joinToString(" ") { it.replaceFirstChar(Char::uppercase) }
    }

    /** "Note · 2 h ago · by Jarvis" */
    fun meta(item: Item, now: Long = System.currentTimeMillis()): String {
        val what = when {
            item.kind == Kind.FOLDER -> "Folder"
            item.kind == Kind.NOTE -> "Note"
            item.kind == Kind.LINK -> "Link"
            else -> size(item.size)
        }
        val by = if (item.byAi) " · by ${who(item.createdBy)}" else ""
        return "$what · ${ago(item.updated, now)}$by"
    }

    fun ago(at: Long, now: Long = System.currentTimeMillis()): String {
        val s = (now - at) / 1000
        return when {
            s < 60 -> "just now"
            s < 3600 -> "${s / 60} min ago"
            s < 86_400 -> "${s / 3600} h ago"
            s < 7 * 86_400 -> "${s / 86_400} d ago"
            else -> java.text.SimpleDateFormat("d MMM yyyy", java.util.Locale.getDefault())
                .format(java.util.Date(at))
        }
    }

    fun size(bytes: Long): String = when {
        bytes < 1024 -> "$bytes B"
        bytes < 1024 * 1024 -> "${bytes / 1024} KB"
        else -> "%.1f MB".format(bytes / 1024.0 / 1024.0)
    }

    fun dp(context: Context, value: Int): Int = (value * context.resources.displayMetrics.density).toInt()

    fun toast(context: Context, text: String) = Toast.makeText(context, text, Toast.LENGTH_SHORT).show()

    /** One line of text from him, or nothing if he cancels. */
    fun ask(context: Context, title: String, start: String = "", hint: String = "",
            url: Boolean = false, done: (String) -> Unit) {
        val field = EditText(context).apply {
            setText(start)
            setSelection(start.length)
            this.hint = hint
            inputType = if (url) InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_CLASS_TEXT
            else InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
        }
        val box = FrameLayout(context).apply {
            setPadding(dp(context, 22), dp(context, 8), dp(context, 22), 0)
            addView(field)
        }
        AlertDialog.Builder(context)
            .setTitle(title)
            .setView(box)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                field.text.toString().trim().takeIf { it.isNotEmpty() }?.let(done)
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
        field.requestFocus()
    }

    // ── thumbnails ──────────────────────────────────────────────────────────
    private val thumbs = object : LruCache<String, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }

    /** A photo shrunk to about [edge] px; cached. Blocking: call off the main thread. */
    fun thumbnail(item: Item, edge: Int): Bitmap? {
        val key = "${item.id}:${item.updated}:$edge"
        thumbs.get(key)?.let { return it }
        return decode(Store.content(item.id), edge)?.also { thumbs.put(key, it) }
    }

    fun decode(file: File, edge: Int): Bitmap? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.path, bounds)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= edge) sample *= 2
        BitmapFactory.decodeFile(file.path, BitmapFactory.Options().apply { inSampleSize = sample })
    }.getOrNull()

    fun roundCorners(view: ImageView, radiusDp: Int) {
        val radius = radiusDp * view.resources.displayMetrics.density
        view.outlineProvider = object : android.view.ViewOutlineProvider() {
            override fun getOutline(v: android.view.View, outline: android.graphics.Outline) {
                outline.setRoundRect(0, 0, v.width, v.height, radius)
            }
        }
        view.clipToOutline = true
    }
}
