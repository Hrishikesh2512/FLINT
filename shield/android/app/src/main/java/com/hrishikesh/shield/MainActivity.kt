package com.hrishikesh.shield

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.isVisible
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * The explorer. Folders to walk into, a search over everything (names, the
 * words inside notes and text files, tags), and views across the whole tree:
 * starred, what his AIs made, recent, what happened, and the Trash.
 *
 * Whatever his AIs do through the API shows up here as it happens, the
 * screen listens to the store, not just to its own taps.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_FOLDER = "folder"
    }

    private enum class View_ { FILES, PHONE, STARRED, AI, RECENT, ACTIVITY, TRASH }

    /** A quick way into the phone's storage: Downloads, Camera, … */
    private data class Place(val label: String, val dir: File)

    private lateinit var list: RecyclerView
    private lateinit var empty: TextView
    private lateinit var title: TextView
    private lateinit var crumbs: TextView
    private lateinit var search: EditText
    private lateinit var up: ImageButton
    private lateinit var views: LinearLayout

    private var folder: String? = null
    private var view = View_.FILES
    /** Where he is in the phone's storage; null for the list of places. */
    private var phoneDir: File? = null
    private val rows = Rows()
    private val refresh = { show() }

    private val pickFiles = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris -> if (uris.isNotEmpty()) addFiles(uris) }

    private val notifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        Store.init(this)
        ShieldService.start(this)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        list = findViewById(R.id.list)
        empty = findViewById(R.id.empty)
        title = findViewById(R.id.title)
        crumbs = findViewById(R.id.crumbs)
        search = findViewById(R.id.search)
        up = findViewById(R.id.up)
        views = findViewById(R.id.views)

        list.layoutManager = LinearLayoutManager(this)
        list.adapter = rows
        up.setOnClickListener { if (view == View_.PHONE) phoneUp() else goUp() }
        findViewById<View>(R.id.settings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        findViewById<View>(R.id.add).setOnClickListener { addMenu(it) }
        search.doAfterTextChanged { show() }
        makeViews()

        folder = savedInstanceState?.getString(EXTRA_FOLDER) ?: intent.getStringExtra(EXTRA_FOLDER)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    search.text.isNotEmpty() -> search.setText("")
                    view == View_.PHONE && phoneDir != null -> phoneUp()
                    view != View_.FILES -> pick(View_.FILES)
                    folder != null -> goUp()
                    else -> finish()
                }
            }
        })
        Store.listen(refresh)
        show()
    }

    override fun onDestroy() {
        Store.unlisten(refresh)
        super.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(EXTRA_FOLDER, folder)
    }

    override fun onResume() {
        super.onResume()
        show()
    }

    // ── what is shown ───────────────────────────────────────────────────────
    private fun makeViews() {
        val labels = listOf(
            View_.FILES to R.string.view_files, View_.PHONE to R.string.view_phone,
            View_.STARRED to R.string.view_starred,
            View_.AI to R.string.view_ai, View_.RECENT to R.string.view_recent,
            View_.ACTIVITY to R.string.view_activity, View_.TRASH to R.string.view_trash)
        labels.forEach { (which, label) ->
            views.addView(TextView(this).apply {
                setText(label)
                tag = which
                setTextColor(getColor(R.color.ink))
                textSize = 13f
                setBackgroundResource(R.drawable.chip)
                setPadding(Ui.dp(context, 14), Ui.dp(context, 7), Ui.dp(context, 14), Ui.dp(context, 7))
                setOnClickListener { pick(which) }
            }, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { marginEnd = Ui.dp(this@MainActivity, 8) })
        }
    }

    private fun pick(which: View_) {
        view = which
        show()
    }

    private fun show() {
        for (i in 0 until views.childCount) views.getChildAt(i).isSelected = views.getChildAt(i).tag == view
        val query = search.text.toString().trim()
        empty.setOnClickListener(null)
        if (view == View_.PHONE) return showPhone(query)
        val here = folder?.let { Store.get(it) }
        if (folder != null && (here == null || here.trashed != null)) {
            folder = null
            return show()
        }
        up.isVisible = folder != null && view == View_.FILES && query.isEmpty()
        title.text = when {
            query.isNotEmpty() -> "“$query”"
            view == View_.FILES -> here?.name ?: getString(R.string.everything)
            else -> views.findViewWithTag<TextView>(view).text
        }
        crumbs.text = if (view == View_.FILES && query.isEmpty()) {
            (listOf(getString(R.string.everything)) + Store.path(folder).map { it.name } +
                listOfNotNull(here?.name)).joinToString("  ›  ")
        } else ""
        crumbs.isVisible = crumbs.text.isNotEmpty()

        val shown: List<Any> = when {
            query.isNotEmpty() -> Store.search(query, starred = view == View_.STARRED,
                by = if (view == View_.AI) "ai" else null)
            view == View_.FILES -> Store.list(folder)
            view == View_.STARRED -> Store.search("", starred = true, limit = 200)
            view == View_.AI -> Store.search("", by = "ai", limit = 200)
            view == View_.RECENT -> Store.search("", limit = 60).filter { it.kind != Kind.FOLDER }
            view == View_.ACTIVITY -> Store.activity(150)
            else -> Store.list(null, trash = true)
        }
        rows.show(shown)
        empty.isVisible = shown.isEmpty()
        empty.setText(when {
            query.isNotEmpty() -> R.string.empty_search
            view == View_.AI -> R.string.empty_ai
            view == View_.TRASH -> R.string.empty_trash
            view == View_.ACTIVITY -> R.string.empty_activity
            else -> R.string.empty_folder
        })
    }

    // ── the phone's own storage ─────────────────────────────────────────────
    private fun showPhone(query: String) {
        val dir = phoneDir
        up.isVisible = dir != null && query.isEmpty()
        title.text = when {
            query.isNotEmpty() -> "“$query”"
            dir == null -> getString(R.string.view_phone)
            dir == PhoneFiles.root -> getString(R.string.all_storage)
            else -> dir.name
        }
        crumbs.text = dir?.let {
            (listOf(getString(R.string.view_phone)) +
                it.path.removePrefix(PhoneFiles.root.path).split('/').filter(String::isNotEmpty))
                .joinToString("  ›  ")
        }.orEmpty()
        crumbs.isVisible = crumbs.text.isNotEmpty()
        if (!PhoneFiles.allowed(this)) {
            rows.show(emptyList())
            empty.isVisible = true
            empty.setText(R.string.phone_access)
            empty.setOnClickListener { PhoneFiles.askAccess(this) }
            return
        }
        if (query.isNotEmpty()) {
            lifecycleScope.launch {
                val found = withContext(Dispatchers.IO) { PhoneFiles.search(dir ?: PhoneFiles.root, query) }
                if (view != View_.PHONE || search.text.toString().trim() != query) return@launch
                rows.show(found)
                empty.isVisible = found.isEmpty()
                empty.setText(R.string.empty_search)
            }
            return
        }
        val shown: List<Any> = dir?.let { PhoneFiles.list(it) }
            ?: (PhoneFiles.places().map { Place(it.first, it.second) } +
                Place(getString(R.string.all_storage), PhoneFiles.root))
        rows.show(shown)
        empty.isVisible = shown.isEmpty()
        empty.setText(R.string.empty_phone_folder)
    }

    private fun phoneUp() {
        val dir = phoneDir ?: return
        phoneDir = if (dir == PhoneFiles.root || places().contains(dir)) null else dir.parentFile
        show()
    }

    private fun places() = PhoneFiles.places().map { it.second }

    private fun openPhone(file: File) {
        if (file.isDirectory) {
            phoneDir = file
            search.setText("")
            show()
        } else {
            PhoneFiles.open(this, file)
        }
    }

    private fun phoneActions(file: File, anchor: View) {
        PopupMenu(this, anchor).apply {
            menu.add(R.string.open).setOnMenuItemClickListener { openPhone(file); true }
            if (file.isFile) {
                menu.add(R.string.save_to_shield).setOnMenuItemClickListener {
                    lifecycleScope.launch {
                        val saved = withContext(Dispatchers.IO) { PhoneFiles.saveToShield(file) }
                        Ui.toast(this@MainActivity, getString(
                            if (saved != null) R.string.saved_to_inbox else R.string.could_not_save))
                    }
                    true
                }
                menu.add(R.string.share).setOnMenuItemClickListener { PhoneFiles.share(this@MainActivity, file); true }
            }
            menu.add(R.string.rename).setOnMenuItemClickListener {
                Ui.ask(this@MainActivity, getString(R.string.rename), file.name) { name ->
                    if (!file.renameTo(File(file.parentFile, name))) Ui.toast(this@MainActivity, "Could not rename it")
                    show()
                }
                true
            }
            menu.add(R.string.delete).setOnMenuItemClickListener {
                AlertDialog.Builder(this@MainActivity)
                    .setTitle(getString(R.string.delete))
                    .setMessage(getString(R.string.delete_phone_file, file.name))
                    .setPositiveButton(R.string.delete) { _, _ ->
                        if (!file.deleteRecursively()) Ui.toast(this@MainActivity, "Could not delete it")
                        show()
                    }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
                true
            }
            show()
        }
    }

    private fun goUp() {
        folder = folder?.let { Store.get(it)?.parent }
        show()
    }

    private fun open(item: Item) {
        if (item.trashed != null) return actions(item, list)
        if (item.kind == Kind.FOLDER) {
            folder = item.id
            view = View_.FILES
            search.setText("")
            show()
        } else {
            startActivity(Intent(this, ItemActivity::class.java).putExtra(ItemActivity.EXTRA_ID, item.id))
        }
    }

    // ── adding ──────────────────────────────────────────────────────────────
    /** New things go where he is; from a view or search, into the Inbox. */
    private fun destination(): String? =
        if (view == View_.FILES && search.text.isEmpty()) folder
        else Store.resolve("/${Store.INBOX}")?.id

    private fun addMenu(anchor: View) {
        PopupMenu(this, anchor).apply {
            menu.add(R.string.new_folder).setOnMenuItemClickListener {
                Ui.ask(this@MainActivity, getString(R.string.new_folder), hint = getString(R.string.folder_name)) {
                    Store.create(destination(), Kind.FOLDER, it, by = "you")
                }
                true
            }
            menu.add(R.string.new_note).setOnMenuItemClickListener {
                val note = Store.create(destination(), Kind.NOTE, "", body = "", by = "you")
                startActivity(Intent(this@MainActivity, ItemActivity::class.java)
                    .putExtra(ItemActivity.EXTRA_ID, note.id).putExtra(ItemActivity.EXTRA_EDIT, true))
                true
            }
            menu.add(R.string.add_files).setOnMenuItemClickListener {
                pickFiles.launch(arrayOf("*/*")); true
            }
            menu.add(R.string.add_link).setOnMenuItemClickListener {
                Ui.ask(this@MainActivity, getString(R.string.add_link), hint = "https://…", url = true) { url ->
                    val name = Uri.parse(url).host?.removePrefix("www.") ?: url
                    Store.create(destination(), Kind.LINK, name, body = url, by = "you")
                }
                true
            }
            show()
        }
    }

    private fun addFiles(uris: List<Uri>) {
        val into = destination()
        lifecycleScope.launch {
            val added = withContext(Dispatchers.IO) {
                uris.count { uri -> Importer.file(this@MainActivity, uri, into, by = "you") != null }
            }
            Ui.toast(this@MainActivity, "Added $added ${if (added == 1) "file" else "files"}")
        }
    }

    // ── one item's actions ──────────────────────────────────────────────────
    private fun actions(item: Item, anchor: View) {
        PopupMenu(this, anchor).apply {
            if (item.trashed != null) {
                menu.add(R.string.restore).setOnMenuItemClickListener { Store.restore(item.id, "you"); true }
                menu.add(R.string.delete_forever).setOnMenuItemClickListener {
                    AlertDialog.Builder(this@MainActivity)
                        .setTitle(getString(R.string.delete_forever))
                        .setMessage("“${item.name}” can't be brought back after this.")
                        .setPositiveButton(R.string.delete) { _, _ -> Store.purge(item.id, "you") }
                        .setNegativeButton(android.R.string.cancel, null)
                        .show()
                    true
                }
            } else {
                menu.add(R.string.open).setOnMenuItemClickListener { open(item); true }
                menu.add(R.string.rename).setOnMenuItemClickListener {
                    Ui.ask(this@MainActivity, getString(R.string.rename), item.name) {
                        Store.update(item.id, "you", name = it)
                    }
                    true
                }
                menu.add(R.string.move).setOnMenuItemClickListener { Mover.ask(this@MainActivity, item); true }
                menu.add(if (item.starred) R.string.unstar else R.string.star).setOnMenuItemClickListener {
                    Store.update(item.id, "you", starred = !item.starred); true
                }
                menu.add(R.string.tags).setOnMenuItemClickListener {
                    Ui.ask(this@MainActivity, getString(R.string.tags), item.tags.joinToString(", "),
                        getString(R.string.tags_hint)) { Store.update(item.id, "you", tags = it.split(',')) }
                    true
                }
                menu.add(R.string.delete).setOnMenuItemClickListener {
                    Store.trash(item.id, "you")
                    Ui.toast(this@MainActivity, getString(R.string.moved_to_trash))
                    true
                }
            }
            show()
        }
    }

    // ── the list ────────────────────────────────────────────────────────────
    private inner class Rows : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
        private var shown: List<Any> = emptyList()

        fun show(now: List<Any>) {
            shown = now
            notifyDataSetChanged()
        }

        override fun getItemCount() = shown.size

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
            object : RecyclerView.ViewHolder(
                LayoutInflater.from(parent.context).inflate(R.layout.item_row, parent, false)) {}

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            val row = holder.itemView
            val icon = row.findViewById<ImageView>(R.id.icon)
            val thumb = row.findViewById<ImageView>(R.id.thumb)
            val name = row.findViewById<TextView>(R.id.name)
            val meta = row.findViewById<TextView>(R.id.meta)
            val star = row.findViewById<ImageView>(R.id.star)
            thumb.isVisible = false
            thumb.setImageDrawable(null)

            when (val shownRow = shown[position]) {
                is Item -> {
                    icon.setImageResource(Ui.icon(shownRow))
                    icon.imageTintList = Ui.tint(this@MainActivity, shownRow)
                    name.text = shownRow.name
                    meta.text = if (shownRow.trashed != null) "Deleted ${Ui.ago(shownRow.trashed)}"
                    else Ui.meta(shownRow)
                    meta.setTextColor(getColor(if (shownRow.byAi) R.color.ai else R.color.dim))
                    star.isVisible = shownRow.starred
                    row.setOnClickListener { open(shownRow) }
                    row.setOnLongClickListener { actions(shownRow, it); true }
                    if (shownRow.isImage) thumbnail(shownRow, thumb)
                }
                is Place -> {
                    icon.setImageResource(R.drawable.ic_folder)
                    icon.imageTintList = android.content.res.ColorStateList.valueOf(getColor(R.color.shield))
                    name.text = shownRow.label
                    meta.text = PhoneFiles.meta(shownRow.dir)
                    meta.setTextColor(getColor(R.color.dim))
                    star.isVisible = false
                    row.setOnClickListener { openPhone(shownRow.dir) }
                    row.setOnLongClickListener(null)
                }
                is File -> {
                    val image = shownRow.isFile && PhoneFiles.isImage(shownRow)
                    icon.setImageResource(when {
                        shownRow.isDirectory -> R.drawable.ic_folder
                        image -> R.drawable.ic_image
                        PhoneFiles.mime(shownRow) == "application/pdf" -> R.drawable.ic_pdf
                        else -> R.drawable.ic_file
                    })
                    icon.imageTintList = android.content.res.ColorStateList.valueOf(
                        getColor(if (shownRow.isDirectory) R.color.shield else R.color.ink))
                    name.text = shownRow.name
                    meta.text = PhoneFiles.meta(shownRow)
                    meta.setTextColor(getColor(R.color.dim))
                    star.isVisible = false
                    row.setOnClickListener { openPhone(shownRow) }
                    row.setOnLongClickListener { phoneActions(shownRow, it); true }
                    if (image) phoneThumbnail(shownRow, thumb)
                }
                is Change -> {
                    val item = Store.get(shownRow.item)
                    icon.setImageResource(item?.let(Ui::icon) ?: R.drawable.ic_file)
                    icon.imageTintList = android.content.res.ColorStateList.valueOf(getColor(
                        if (shownRow.by in setOf("you", "shield")) R.color.ink else R.color.ai))
                    name.text = "${Ui.who(shownRow.by)} ${shownRow.action} ${shownRow.detail}"
                    meta.text = Ui.ago(shownRow.at)
                    meta.setTextColor(getColor(R.color.dim))
                    star.isVisible = false
                    row.setOnClickListener { item?.let(::open) }
                    row.setOnLongClickListener(null)
                }
            }
        }

        private fun phoneThumbnail(file: File, into: ImageView) {
            into.tag = file.path
            Ui.roundCorners(into, 14)
            lifecycleScope.launch {
                val picture = withContext(Dispatchers.IO) { Ui.decode(file, Ui.dp(this@MainActivity, 92)) }
                if (into.tag == file.path && picture != null) {
                    into.setImageBitmap(picture)
                    into.isVisible = true
                }
            }
        }

        private fun thumbnail(item: Item, into: ImageView) {
            into.tag = item.id
            Ui.roundCorners(into, 14)
            lifecycleScope.launch {
                val picture = withContext(Dispatchers.IO) { Ui.thumbnail(item, Ui.dp(this@MainActivity, 92)) }
                if (into.tag == item.id && picture != null) {
                    into.setImageBitmap(picture)
                    into.isVisible = true
                }
            }
        }
    }
}

/** Bringing outside files into Shield: from the picker or the share sheet. */
object Importer {
    /** One file in; the new item, or null when it could not be read. */
    fun file(context: android.content.Context, uri: Uri, parent: String?, by: String): Item? = runCatching {
        val resolver = context.contentResolver
        val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { if (it.moveToFirst()) it.getString(0) else null }
            ?: uri.lastPathSegment ?: "file"
        val bytes = resolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        Store.create(parent, Kind.FILE, name, bytes = bytes, mime = resolver.getType(uri).orEmpty(), by = by)
    }.getOrNull()
}

/** "Move to…": every folder, by its path, except where it cannot go. */
object Mover {
    fun ask(context: android.content.Context, item: Item) {
        val folders = Store.every(Kind.FOLDER).filter { f ->
            f.id != item.id && Store.path(f.id).none { it.id == item.id }
        }
        val labels = listOf(context.getString(R.string.top_level)) + folders.map { f ->
            "/" + (Store.path(f.id).map { it.name } + f.name).joinToString("/")
        }
        AlertDialog.Builder(context)
            .setTitle(context.getString(R.string.move_to))
            .setItems(labels.toTypedArray()) { _, which ->
                runCatching {
                    if (which == 0) Store.update(item.id, "you", moveToTop = true)
                    else Store.update(item.id, "you", parent = folders[which - 1].id)
                }.onFailure { Ui.toast(context, it.message ?: "Could not move it") }
            }
            .show()
    }
}
