package com.hrishikesh.shield

import android.app.AlertDialog
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.view.Gravity
import android.view.View
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * One item, up close. A note is edited in place (saved as he leaves); a photo
 * or PDF is shown; a text file is shown as text; anything else can be opened
 * in the app that handles it. Below: everyone who has touched it, and when.
 */
class ItemActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_ID = "id"
        const val EXTRA_EDIT = "edit"
        private const val PDF_PAGES = 30
    }

    private lateinit var id: String
    private lateinit var body: LinearLayout
    private var editor: EditText? = null
    private var shownNote: String? = null
    private val refresh = { if (editor?.hasFocus() != true) show() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_item)
        Store.init(this)
        id = intent.getStringExtra(EXTRA_ID) ?: return finish()
        body = findViewById(R.id.body)
        findViewById<View>(R.id.back).setOnClickListener { finish() }
        findViewById<View>(R.id.name).setOnClickListener {
            val item = Store.get(id) ?: return@setOnClickListener
            Ui.ask(this, getString(R.string.rename), item.name) { Store.update(id, "you", name = it) }
        }
        findViewById<View>(R.id.star).setOnClickListener {
            Store.get(id)?.let { Store.update(id, "you", starred = !it.starred) }
        }
        findViewById<View>(R.id.more).setOnClickListener { actions(it) }
        Store.listen(refresh)
        show(fresh = true)
    }

    override fun onPause() {
        saveNote()
        super.onPause()
    }

    override fun onDestroy() {
        Store.unlisten(refresh)
        super.onDestroy()
    }

    private fun show(fresh: Boolean = false) {
        val item = Store.get(id)
        if (item == null || item.trashed != null) return finish()
        findViewById<TextView>(R.id.name).text = item.name
        findViewById<ImageButton>(R.id.star).apply {
            setImageResource(if (item.starred) R.drawable.ic_star_on else R.drawable.ic_star)
            imageTintList = android.content.res.ColorStateList.valueOf(
                getColor(if (item.starred) R.color.ai else R.color.ink))
        }
        val made = if (item.createdBy == "you") "You added it" else "${Ui.who(item.createdBy)} added it"
        val folder = item.parent?.let { Store.get(it)?.name } ?: getString(R.string.everything)
        findViewById<TextView>(R.id.meta).text =
            "${Ui.meta(item).substringBefore(" · ")} · in $folder · $made ${Ui.ago(item.created)}"
        tags(item)
        // The body is built once: rebuilding it under his cursor would lose
        // his place in a note, and a PDF is slow to draw.
        if (fresh || item.kind != Kind.NOTE) content(item)
        about(item)
        history(item)
    }

    private fun tags(item: Item) {
        val row = findViewById<LinearLayout>(R.id.tags)
        row.removeAllViews()
        (item.tags.map { "#$it" } + "+ tag").forEach { label ->
            row.addView(TextView(this).apply {
                text = label
                textSize = 13f
                setTextColor(getColor(if (label.startsWith("#")) R.color.shield else R.color.dim))
                setBackgroundResource(R.drawable.chip)
                setPadding(Ui.dp(context, 12), Ui.dp(context, 6), Ui.dp(context, 12), Ui.dp(context, 6))
                setOnClickListener {
                    Ui.ask(this@ItemActivity, getString(R.string.tags), item.tags.joinToString(", "),
                        getString(R.string.tags_hint)) {
                        Store.update(this@ItemActivity.id, "you", tags = it.split(','))
                    }
                }
            }, LinearLayout.LayoutParams(-2, -2).apply { marginEnd = Ui.dp(this@ItemActivity, 8) })
        }
    }

    // ── the thing itself ────────────────────────────────────────────────────
    private fun content(item: Item) {
        body.removeAllViews()
        editor = null
        when {
            item.kind == Kind.NOTE -> note(item)
            item.kind == Kind.LINK -> link(item)
            item.isImage -> photo(item)
            item.isPdf -> pdf(item)
            item.body != null -> text(item.body)
            else -> nothing()
        }
        if (item.kind == Kind.FILE && !item.isImage) openWithButton()
    }

    private fun note(item: Item) {
        shownNote = item.body.orEmpty()
        editor = EditText(this).apply {
            setText(item.body.orEmpty())
            hint = getString(R.string.note_hint)
            setTextColor(getColor(R.color.ink))
            setHintTextColor(getColor(R.color.dim))
            textSize = 16f
            setLineSpacing(0f, 1.2f)
            gravity = Gravity.TOP
            minLines = 12
            setBackgroundResource(R.drawable.glass_card)
            setPadding(Ui.dp(context, 18), Ui.dp(context, 16), Ui.dp(context, 18), Ui.dp(context, 16))
        }
        body.addView(editor, LinearLayout.LayoutParams(-1, -2))
        if (intent.getBooleanExtra(EXTRA_EDIT, false)) {
            editor?.requestFocus()
            window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
        }
    }

    /** A note is saved when he leaves it, and named by its first line if it has no name. */
    private fun saveNote() {
        val field = editor ?: return
        val text = field.text.toString()
        if (text == shownNote) return
        shownNote = text
        val item = Store.get(id) ?: return
        val firstLine = text.lineSequence().firstOrNull { it.isNotBlank() }?.trim()?.take(60)
        val rename = if (item.name.startsWith(Kind.untitled(Kind.NOTE)) && firstLine != null) firstLine else null
        Store.update(id, "you", body = text, name = rename)
    }

    private fun link(item: Item) {
        val url = item.body.orEmpty()
        body.addView(TextView(this).apply {
            text = url
            setTextColor(getColor(R.color.shield))
            textSize = 15f
            setTextIsSelectable(true)
            setBackgroundResource(R.drawable.glass_card)
            setPadding(Ui.dp(context, 18), Ui.dp(context, 16), Ui.dp(context, 18), Ui.dp(context, 16))
        }, LinearLayout.LayoutParams(-1, -2))
        body.addView(button(getString(R.string.open)) {
            runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
        })
    }

    private fun photo(item: Item) {
        val view = ImageView(this).apply { adjustViewBounds = true }
        Ui.roundCorners(view, 18)
        body.addView(view, LinearLayout.LayoutParams(-1, -2))
        lifecycleScope.launch {
            val picture = withContext(Dispatchers.IO) {
                Ui.decode(Store.content(item.id), resources.displayMetrics.widthPixels)
            }
            view.setImageBitmap(picture)
        }
    }

    private fun pdf(item: Item) {
        lifecycleScope.launch {
            val pages = withContext(Dispatchers.IO) { renderPdf(Store.content(item.id)) }
            pages.forEach { page ->
                body.addView(ImageView(this@ItemActivity).apply {
                    adjustViewBounds = true
                    setImageBitmap(page)
                    setBackgroundColor(Color.WHITE)
                }, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = Ui.dp(this@ItemActivity, 10) })
            }
            if (pages.isEmpty()) nothing()
        }
    }

    private fun renderPdf(file: File): List<Bitmap> = runCatching {
        val fd = ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
        val pdf = PdfRenderer(fd)
        try {
            val width = resources.displayMetrics.widthPixels
            List(minOf(pdf.pageCount, PDF_PAGES)) { i ->
                val page = pdf.openPage(i)
                try {
                    val height = width * page.height / page.width
                    Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also {
                        it.eraseColor(Color.WHITE)
                        page.render(it, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                    }
                } finally {
                    page.close()
                }
            }
        } finally {
            pdf.close()
            fd.close()
        }
    }.getOrDefault(emptyList())

    private fun text(content: String) {
        body.addView(TextView(this).apply {
            text = content.take(200_000)
            typeface = Typeface.MONOSPACE
            textSize = 13f
            setTextColor(getColor(R.color.ink))
            setTextIsSelectable(true)
            setBackgroundResource(R.drawable.glass_card)
            setPadding(Ui.dp(context, 16), Ui.dp(context, 14), Ui.dp(context, 16), Ui.dp(context, 14))
        }, LinearLayout.LayoutParams(-1, -2))
    }

    private fun nothing() {
        body.addView(TextView(this).apply {
            setText(R.string.no_preview)
            setTextColor(getColor(R.color.dim))
            gravity = Gravity.CENTER
            setPadding(0, Ui.dp(context, 40), 0, Ui.dp(context, 12))
        })
    }

    private fun openWithButton() = body.addView(button(getString(R.string.share)) { share() })

    private fun button(label: String, onClick: () -> Unit) = TextView(this).apply {
        text = label
        setTextColor(getColor(R.color.ink))
        textSize = 14f
        gravity = Gravity.CENTER
        setBackgroundResource(R.drawable.glass_pill)
        setPadding(Ui.dp(context, 20), Ui.dp(context, 12), Ui.dp(context, 20), Ui.dp(context, 12))
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = Ui.dp(context, 12) }
    }

    /** What an AI found out about it, above its history. */
    private fun about(item: Item) {
        body.findViewWithTag<View>("about")?.let { body.removeView(it) }
        val text = item.about?.takeIf { it.isNotBlank() } ?: return
        body.addView(LinearLayout(this).apply {
            tag = "about"
            orientation = LinearLayout.VERTICAL
            setPadding(0, Ui.dp(context, 16), 0, 0)
            addView(heading(getString(R.string.about_it)))
            addView(TextView(context).apply {
                this.text = text
                textSize = 14f
                setTextColor(getColor(R.color.ink))
                setTextIsSelectable(true)
                setBackgroundResource(R.drawable.glass_card)
                setPadding(Ui.dp(context, 16), Ui.dp(context, 12), Ui.dp(context, 16), Ui.dp(context, 12))
            })
        })
    }

    // ── who touched it ──────────────────────────────────────────────────────
    private fun history(item: Item) {
        body.findViewWithTag<View>("history")?.let { body.removeView(it) }
        val box = LinearLayout(this).apply {
            tag = "history"
            orientation = LinearLayout.VERTICAL
            setPadding(0, Ui.dp(context, 22), 0, 0)
        }
        val links = Store.links(item.id)
        if (links.isNotEmpty()) {
            box.addView(heading(getString(R.string.linked)))
            links.forEach { (other, label) ->
                box.addView(line("${other.name}${if (label.isNotBlank()) ", $label" else ""}", R.color.shield) {
                    startActivity(Intent(this, ItemActivity::class.java).putExtra(EXTRA_ID, other.id))
                })
            }
        }
        box.addView(heading(getString(R.string.history)))
        Store.history(item.id, 30).forEach { change ->
            val color = if (change.by in setOf("you", "shield")) R.color.ink else R.color.ai
            box.addView(line("${Ui.who(change.by)} ${change.action} ${change.detail} · ${Ui.ago(change.at)}", color))
        }
        body.addView(box)
    }

    private fun heading(text: String) = TextView(this).apply {
        this.text = text.uppercase()
        letterSpacing = 0.2f
        textSize = 11f
        setTextColor(getColor(R.color.dim))
        setPadding(0, Ui.dp(context, 10), 0, Ui.dp(context, 6))
    }

    private fun line(text: String, color: Int, onClick: (() -> Unit)? = null) = TextView(this).apply {
        this.text = text
        textSize = 13f
        setTextColor(getColor(color))
        setPadding(0, Ui.dp(context, 4), 0, Ui.dp(context, 4))
        onClick?.let { go -> setOnClickListener { go() } }
    }

    // ── actions ─────────────────────────────────────────────────────────────
    private fun actions(anchor: View) {
        val item = Store.get(id) ?: return
        PopupMenu(this, anchor).apply {
            menu.add(R.string.rename).setOnMenuItemClickListener {
                Ui.ask(this@ItemActivity, getString(R.string.rename), item.name) { Store.update(id, "you", name = it) }
                true
            }
            menu.add(R.string.move).setOnMenuItemClickListener { Mover.ask(this@ItemActivity, item); true }
            menu.add(R.string.tags).setOnMenuItemClickListener {
                Ui.ask(this@ItemActivity, getString(R.string.tags), item.tags.joinToString(", "),
                    getString(R.string.tags_hint)) { Store.update(id, "you", tags = it.split(',')) }
                true
            }
            if (item.kind != Kind.FOLDER) menu.add(R.string.share).setOnMenuItemClickListener { share(); true }
            menu.add(R.string.delete).setOnMenuItemClickListener {
                AlertDialog.Builder(this@ItemActivity)
                    .setTitle(getString(R.string.delete))
                    .setMessage("“${item.name}” goes to the Trash. You can restore it from there.")
                    .setPositiveButton(R.string.delete) { _, _ ->
                        Store.trash(id, "you")
                        finish()
                    }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
                true
            }
            show()
        }
    }

    /** To another app: a copy under its real name, never Shield's own file. */
    private fun share() {
        saveNote()
        val item = Store.get(id) ?: return
        lifecycleScope.launch {
            val copy = withContext(Dispatchers.IO) {
                val dir = File(cacheDir, "shared").apply { deleteRecursively(); mkdirs() }
                File(dir, item.name.replace('/', '_')).also { out ->
                    if (item.kind == Kind.FILE) Store.content(item.id).copyTo(out, overwrite = true)
                    else out.writeText(item.body.orEmpty())
                }
            }
            val uri = FileProvider.getUriForFile(this@ItemActivity, "com.hrishikesh.shield.files", copy)
            val send = Intent(Intent.ACTION_SEND)
                .setType(item.mime.ifEmpty { "application/octet-stream" })
                .putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            startActivity(Intent.createChooser(send, item.name))
        }
    }
}
