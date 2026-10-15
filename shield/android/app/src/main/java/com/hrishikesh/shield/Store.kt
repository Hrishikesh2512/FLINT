package com.hrishikesh.shield

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Everything Shield holds, and the only code that touches it.
 *
 * Items are folders, notes, files and links, in one tree. Metadata and a
 * full-text index live in SQLite; a file's bytes live on disk under its id,
 * so renaming or moving never copies anything. The app and the API both go
 * through here, which is what makes "he can see what his AIs did" true: there
 * is no second way in.
 *
 * Every change is written to `history` with who made it, "you" for the app,
 * the agent's name for the API ("jarvis", "venom", anything he builds), so
 * any item can say who made it and who touched it since.
 *
 * Deleting moves to the Trash (a timestamp, children included) and can be
 * undone; only emptying the Trash removes anything for good.
 */
object Store {

    private const val TAG = "shield.store"

    /** The folder shares and AIs drop things into when they don't say where. */
    const val INBOX = "Inbox"

    /** Up to this much of a text file is kept as searchable, readable text. */
    private const val MAX_TEXT = 1_000_000

    private lateinit var db: SQLiteDatabase
    private lateinit var blobs: File
    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArrayList<() -> Unit>()

    @Synchronized
    fun init(context: Context) {
        if (::db.isInitialized) return
        blobs = File(context.filesDir, "shield/blobs").apply { mkdirs() }
        db = Helper(context).writableDatabase
        if (find(null, INBOX) == null) create(null, Kind.FOLDER, INBOX, by = "shield")
    }

    // ── listening ───────────────────────────────────────────────────────────
    /** Told (on the main thread) after any change, whoever made it. */
    fun listen(listener: () -> Unit) = listeners.add(listener)
    fun unlisten(listener: () -> Unit) = listeners.remove(listener)
    private fun changed() = main.post { listeners.forEach { it() } }

    // ── reading ─────────────────────────────────────────────────────────────
    @Synchronized
    fun get(id: String): Item? =
        db.rawQuery("SELECT * FROM items WHERE id = ?", arrayOf(id)).use { c ->
            if (c.moveToFirst()) item(c) else null
        }

    /** A folder's contents (null: the top), folders first, then by name. */
    @Synchronized
    fun list(parent: String?, trash: Boolean = false): List<Item> {
        if (trash) {
            // The Trash shows what was deleted, not everything under it.
            return items("""SELECT i.* FROM items i WHERE i.trashed IS NOT NULL AND
                (i.parent IS NULL OR NOT EXISTS (SELECT 1 FROM items p WHERE p.id = i.parent
                 AND p.trashed = i.trashed)) ORDER BY i.trashed DESC""")
        }
        val where = if (parent == null) "parent IS NULL" else "parent = ?"
        return items(
            "SELECT * FROM items WHERE $where AND trashed IS NULL " +
                "ORDER BY kind != 'folder', name COLLATE NOCASE",
            if (parent == null) emptyArray() else arrayOf(parent))
    }

    /**
     * Search names, note text, text inside files and tags. Every word must
     * match (as a prefix: "pass" finds "passport"). Filters narrow it.
     */
    @Synchronized
    fun search(
        query: String,
        kind: String? = null,
        tag: String? = null,
        starred: Boolean = false,
        by: String? = null,
        limit: Int = 50,
    ): List<Item> {
        // Letters with their marks: Devanagari vowel signs are marks, not letters.
        val terms = Regex("[\\p{L}\\p{M}\\p{N}]+").findAll(query.lowercase()).map { it.value }.toList()
        val where = mutableListOf("i.trashed IS NULL")
        val args = mutableListOf<String>()
        if (terms.isNotEmpty()) {
            where += "i.id IN (SELECT id FROM search WHERE search MATCH ?)"
            args += terms.joinToString(" ") { "$it*" }
        }
        kind?.let { where += "i.kind = ?"; args += it }
        tag?.let { where += "i.id IN (SELECT item FROM tags WHERE tag = ?)"; args += it.lowercase() }
        if (starred) where += "i.starred = 1"
        when (by) {
            null, "" -> {}
            "ai" -> where += "i.created_by NOT IN ('you', 'shield')"
            else -> { where += "i.created_by = ?"; args += by }
        }
        val order = if (terms.isEmpty()) "i.updated DESC" else "i.kind = 'folder' DESC, i.updated DESC"
        return runCatching {
            items("SELECT i.* FROM items i WHERE ${where.joinToString(" AND ")} " +
                "ORDER BY $order LIMIT $limit", args.toTypedArray())
        }.getOrElse {
            Log.w(TAG, "search failed for '$query'", it)
            emptyList()
        }
    }

    /** The folders from the top down to (not including) [id]. */
    @Synchronized
    fun path(id: String?): List<Item> {
        val out = mutableListOf<Item>()
        var at = id?.let { get(it) }?.parent
        while (at != null && out.size < 64) {
            val folder = get(at) ?: break
            out.add(0, folder)
            at = folder.parent
        }
        return out
    }

    /** "/College/Notes" → that folder or item, or null. */
    @Synchronized
    fun resolve(path: String): Item? {
        var at: Item? = null
        for (part in path.split('/').filter { it.isNotBlank() }) {
            at = find(at?.id, part) ?: return null
        }
        return at
    }

    /** A folder at "/A/B", made (with any missing parents) if need be. */
    @Synchronized
    fun folderAt(path: String, by: String): Item? {
        var at: Item? = null
        for (part in path.split('/').filter { it.isNotBlank() }) {
            at = find(at?.id, part)?.takeIf { it.kind == Kind.FOLDER }
                ?: create(at?.id, Kind.FOLDER, part, by = by)
        }
        return at
    }

    @Synchronized
    fun every(kind: String): List<Item> =
        items("SELECT * FROM items WHERE kind = ? AND trashed IS NULL ORDER BY name COLLATE NOCASE",
            arrayOf(kind))

    fun content(id: String): File = File(blobs, id)

    @Synchronized
    fun history(id: String, limit: Int = 50): List<Change> =
        changes("SELECT * FROM history WHERE item = ? ORDER BY at DESC LIMIT $limit", arrayOf(id))

    /** What happened lately across everything: who added, changed, moved what. */
    @Synchronized
    fun activity(limit: Int = 100, by: String? = null): List<Change> =
        if (by == null) changes("SELECT * FROM history ORDER BY at DESC LIMIT $limit")
        else changes("SELECT * FROM history WHERE by = ? ORDER BY at DESC LIMIT $limit", arrayOf(by))

    /** Who has been using Shield, and when they were last seen. */
    @Synchronized
    fun agents(): List<Pair<String, Long>> =
        db.rawQuery("SELECT by, MAX(at) FROM history WHERE by NOT IN ('you','shield') " +
            "GROUP BY by ORDER BY MAX(at) DESC", null).use { c ->
            buildList { while (c.moveToNext()) add(c.getString(0) to c.getLong(1)) }
        }

    @Synchronized
    fun tags(): List<Pair<String, Int>> =
        db.rawQuery("SELECT t.tag, COUNT(*) FROM tags t JOIN items i ON i.id = t.item " +
            "WHERE i.trashed IS NULL GROUP BY t.tag ORDER BY COUNT(*) DESC", null).use { c ->
            buildList { while (c.moveToNext()) add(c.getString(0) to c.getInt(1)) }
        }

    @Synchronized
    fun links(id: String): List<Pair<Item, String>> =
        db.rawQuery("SELECT b, label FROM links WHERE a = ? UNION SELECT a, label FROM links WHERE b = ?",
            arrayOf(id, id)).use { c ->
            buildList {
                while (c.moveToNext()) get(c.getString(0))?.takeIf { it.trashed == null }
                    ?.let { add(it to c.getString(1)) }
            }
        }

    @Synchronized
    fun count(): Int =
        db.rawQuery("SELECT COUNT(*) FROM items WHERE trashed IS NULL AND kind != 'folder'", null)
            .use { if (it.moveToFirst()) it.getInt(0) else 0 }

    // ── writing ─────────────────────────────────────────────────────────────
    /**
     * A new item in [parent] (null: the top). [bytes] for a file, [body] for
     * a note or a link's address. A name already taken there gets " (2)".
     */
    @Synchronized
    fun create(
        parent: String?,
        kind: String,
        name: String,
        body: String? = null,
        bytes: ByteArray? = null,
        mime: String = "",
        tags: Collection<String> = emptyList(),
        by: String,
    ): Item {
        require(kind in Kind.ALL) { "unknown kind: $kind" }
        parent?.let { p -> require(get(p)?.kind == Kind.FOLDER) { "not a folder: $p" } }
        val id = UUID.randomUUID().toString()
        val now = System.currentTimeMillis()
        val clean = unique(parent, name.trim().ifEmpty { Kind.untitled(kind) })
        val text = when {
            bytes != null && Kind.isText(mime, clean) -> String(bytes, 0, minOf(bytes.size, MAX_TEXT))
            else -> body
        }
        bytes?.let { File(blobs, id).writeBytes(it) }
        db.insertOrThrow("items", null, ContentValues().apply {
            put("id", id); put("parent", parent); put("kind", kind); put("name", clean)
            put("mime", mime.ifEmpty { Kind.mimeFor(kind, clean) })
            put("size", bytes?.size?.toLong() ?: (text?.length?.toLong() ?: 0))
            put("body", text); put("created", now); put("updated", now)
            put("created_by", by); put("updated_by", by); put("starred", 0)
        })
        setTags(id, tags)
        index(id)
        log(id, by, "added", "${Kind.label(kind)} \"$clean\"")
        changed()
        return get(id)!!
    }

    /**
     * Change an item: rename, move, edit its text, star it, set or adjust its
     * tags. Only what is given changes. Returns the item as it now is.
     */
    @Synchronized
    fun update(
        id: String,
        by: String,
        name: String? = null,
        parent: String? = null,
        moveToTop: Boolean = false,
        body: String? = null,
        starred: Boolean? = null,
        about: String? = null,
        tags: Collection<String>? = null,
        addTags: Collection<String> = emptyList(),
        removeTags: Collection<String> = emptyList(),
    ): Item {
        val item = get(id) ?: throw NoSuchElementException("no item $id")
        val values = ContentValues()
        val done = mutableListOf<String>()
        val destination = if (moveToTop) null else parent ?: item.parent
        if (moveToTop || (parent != null && parent != item.parent)) {
            destination?.let { d ->
                require(get(d)?.kind == Kind.FOLDER) { "not a folder: $d" }
                require(d != id && path(d).none { it.id == id }) { "a folder cannot go inside itself" }
            }
            values.put("parent", destination)
            done += "moved it to ${destination?.let { get(it)?.name } ?: "the top"}"
        }
        if (name != null && name.trim().isNotEmpty() && name.trim() != item.name) {
            values.put("name", unique(destination, name.trim(), except = id))
            done += "renamed it to \"${name.trim()}\""
        } else if (values.containsKey("parent")) {
            values.put("name", unique(destination, item.name, except = id))
        }
        if (body != null && body != item.body) {
            values.put("body", body)
            values.put("size", body.length.toLong())
            if (item.kind == Kind.FILE) File(blobs, id).writeText(body)
            done += "edited it"
        }
        if (about != null && about != item.about) {
            values.put("about", about)
            done += "wrote what it is"
        }
        if (starred != null && starred != item.starred) {
            values.put("starred", if (starred) 1 else 0)
            done += if (starred) "starred it" else "unstarred it"
        }
        val newTags = (tags?.map(::tagOf) ?: item.tags) + addTags.map(::tagOf) - removeTags.map(::tagOf).toSet()
        if (newTags.toSet() != item.tags.toSet()) {
            setTags(id, newTags)
            done += "tagged it ${newTags.joinToString(", ").ifEmpty { "with nothing" }}"
        }
        if (values.size() > 0 || done.isNotEmpty()) {
            values.put("updated", System.currentTimeMillis())
            values.put("updated_by", by)
            db.update("items", values, "id = ?", arrayOf(id))
            index(id)
            if (done.isNotEmpty()) log(id, by, "changed", done.joinToString("; "))
            changed()
        }
        return get(id)!!
    }

    /** Into the Trash, with everything inside it. */
    @Synchronized
    fun trash(id: String, by: String) {
        val item = get(id) ?: return
        val now = System.currentTimeMillis()
        (listOf(id) + descendants(id)).forEach {
            db.execSQL("UPDATE items SET trashed = ? WHERE id = ? AND trashed IS NULL",
                arrayOf<Any>(now, it))
        }
        log(id, by, "trashed", "\"${item.name}\"")
        changed()
    }

    /** Out of the Trash, with what was trashed along with it. */
    @Synchronized
    fun restore(id: String, by: String) {
        val item = get(id) ?: return
        val stamp = item.trashed ?: return
        (listOf(id) + descendants(id)).forEach {
            db.execSQL("UPDATE items SET trashed = NULL WHERE id = ? AND trashed = ?",
                arrayOf<Any>(it, stamp))
        }
        // Its folder may have been deleted since: then it comes back at the top.
        val parent = item.parent?.let { get(it) }
        if (item.parent != null && (parent == null || parent.trashed != null)) {
            db.execSQL("UPDATE items SET parent = NULL WHERE id = ?", arrayOf(id))
        }
        log(id, by, "restored", "\"${item.name}\"")
        changed()
    }

    /** Gone for good, only from the Trash. */
    @Synchronized
    fun purge(id: String, by: String) {
        val item = get(id) ?: return
        require(item.trashed != null) { "only something in the Trash can be deleted for good" }
        (listOf(id) + descendants(id)).forEach { gone ->
            db.delete("items", "id = ?", arrayOf(gone))
            db.delete("tags", "item = ?", arrayOf(gone))
            db.delete("links", "a = ? OR b = ?", arrayOf(gone, gone))
            db.delete("search", "id = ?", arrayOf(gone))
            File(blobs, gone).delete()
        }
        log(id, by, "deleted for good", "\"${item.name}\"")
        changed()
    }

    @Synchronized
    fun emptyTrash(by: String): Int {
        val trashed = list(null, trash = true)
        trashed.forEach { purge(it.id, by) }
        return trashed.size
    }

    @Synchronized
    fun link(a: String, b: String, label: String, by: String) {
        require(get(a) != null && get(b) != null) { "both items must exist" }
        db.insertWithOnConflict("links", null, ContentValues().apply {
            put("a", a); put("b", b); put("label", label)
        }, SQLiteDatabase.CONFLICT_REPLACE)
        log(a, by, "linked", "to \"${get(b)?.name}\"${if (label.isNotBlank()) " ($label)" else ""}")
        changed()
    }

    // ── the details ─────────────────────────────────────────────────────────
    private fun find(parent: String?, name: String): Item? {
        val where = if (parent == null) "parent IS NULL" else "parent = ?"
        val args = listOfNotNull(parent, name).toTypedArray()
        return items("SELECT * FROM items WHERE $where AND name = ? COLLATE NOCASE AND trashed IS NULL LIMIT 1",
            args).firstOrNull()
    }

    private fun unique(parent: String?, name: String, except: String? = null): String {
        fun taken(candidate: String) = find(parent, candidate)?.let { it.id != except } ?: false
        if (!taken(name)) return name
        val dot = name.lastIndexOf('.').takeIf { it > 0 } ?: name.length
        val (stem, ext) = name.substring(0, dot) to name.substring(dot)
        var n = 2
        while (taken("$stem ($n)$ext")) n++
        return "$stem ($n)$ext"
    }

    private fun descendants(id: String): List<String> {
        val out = mutableListOf<String>()
        val queue = ArrayDeque(listOf(id))
        while (queue.isNotEmpty()) {
            db.rawQuery("SELECT id FROM items WHERE parent = ?", arrayOf(queue.removeFirst())).use { c ->
                while (c.moveToNext()) c.getString(0).let { out += it; queue += it }
            }
        }
        return out
    }

    private fun tagOf(raw: String) = raw.trim().trimStart('#').lowercase()

    private fun setTags(id: String, tags: Collection<String>) {
        db.delete("tags", "item = ?", arrayOf(id))
        tags.map(::tagOf).filter { it.isNotEmpty() }.distinct().forEach { tag ->
            db.insert("tags", null, ContentValues().apply { put("item", id); put("tag", tag) })
        }
    }

    private fun index(id: String) {
        val item = get(id) ?: return
        db.delete("search", "id = ?", arrayOf(id))
        db.insert("search", null, ContentValues().apply {
            put("id", id)
            put("name", item.name.replace(Regex("[._-]"), " "))
            put("body", listOfNotNull(item.body, item.about).joinToString("\n"))
            put("tags", item.tags.joinToString(" "))
        })
    }

    private fun log(item: String, by: String, action: String, detail: String) {
        db.insert("history", null, ContentValues().apply {
            put("item", item); put("at", System.currentTimeMillis())
            put("by", by); put("action", action); put("detail", detail)
        })
    }

    private fun items(sql: String, args: Array<String> = emptyArray()): List<Item> =
        db.rawQuery(sql, args).use { c -> buildList { while (c.moveToNext()) add(item(c)) } }

    private fun item(c: Cursor): Item {
        val id = c.getString(c.getColumnIndexOrThrow("id"))
        val tags = db.rawQuery("SELECT tag FROM tags WHERE item = ? ORDER BY tag", arrayOf(id)).use {
            buildList { while (it.moveToNext()) add(it.getString(0)) }
        }
        fun str(name: String) = c.getString(c.getColumnIndexOrThrow(name))
        fun long(name: String) = c.getLong(c.getColumnIndexOrThrow(name))
        val trashedAt = c.getColumnIndexOrThrow("trashed")
        return Item(
            id = id, parent = str("parent"), kind = str("kind"), name = str("name"),
            mime = str("mime").orEmpty(), size = long("size"), body = str("body"),
            created = long("created"), updated = long("updated"),
            createdBy = str("created_by").orEmpty(), updatedBy = str("updated_by").orEmpty(),
            starred = long("starred") == 1L,
            about = str("about"),
            trashed = if (c.isNull(trashedAt)) null else c.getLong(trashedAt),
            tags = tags,
        )
    }

    private fun changes(sql: String, args: Array<String> = emptyArray()): List<Change> =
        db.rawQuery(sql, args).use { c ->
            buildList {
                while (c.moveToNext()) add(Change(
                    item = c.getString(c.getColumnIndexOrThrow("item")),
                    at = c.getLong(c.getColumnIndexOrThrow("at")),
                    by = c.getString(c.getColumnIndexOrThrow("by")),
                    action = c.getString(c.getColumnIndexOrThrow("action")),
                    detail = c.getString(c.getColumnIndexOrThrow("detail")),
                ))
            }
        }

    private class Helper(context: Context) : SQLiteOpenHelper(context, "shield.db", null, 2) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL("""CREATE TABLE items (
                id TEXT PRIMARY KEY, parent TEXT, kind TEXT NOT NULL, name TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, body TEXT,
                created INTEGER NOT NULL, updated INTEGER NOT NULL,
                created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0, trashed INTEGER, about TEXT)""")
            db.execSQL("CREATE INDEX items_parent ON items(parent, trashed)")
            db.execSQL("CREATE TABLE tags (item TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (item, tag))")
            db.execSQL("CREATE TABLE links (a TEXT NOT NULL, b TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', " +
                "PRIMARY KEY (a, b))")
            db.execSQL("""CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL, at INTEGER NOT NULL, by TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")
            db.execSQL("CREATE INDEX history_item ON history(item, at)")
            // unicode61 understands Hindi and accents; the plain tokenizer is
            // the fallback where a build of SQLite lacks it.
            runCatching {
                db.execSQL("CREATE VIRTUAL TABLE search USING fts4(id, name, body, tags, " +
                    "notindexed=id, tokenize=unicode61)")
            }.onFailure {
                db.execSQL("CREATE VIRTUAL TABLE search USING fts4(id, name, body, tags, notindexed=id)")
            }
        }

        override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) {
            // 2: "about", what an AI found out about an item (the organizer).
            if (old < 2) db.execSQL("ALTER TABLE items ADD COLUMN about TEXT")
        }
    }
}

/** What kinds of thing Shield holds, and how each is shown. */
object Kind {
    const val FOLDER = "folder"
    const val NOTE = "note"
    const val FILE = "file"
    const val LINK = "link"
    val ALL = setOf(FOLDER, NOTE, FILE, LINK)

    fun label(kind: String) = when (kind) {
        FOLDER -> "folder"; NOTE -> "note"; LINK -> "link"; else -> "file"
    }

    fun untitled(kind: String) = when (kind) {
        FOLDER -> "New folder"; NOTE -> "Untitled note"; LINK -> "Link"; else -> "File"
    }

    fun mimeFor(kind: String, name: String): String = when (kind) {
        NOTE -> "text/markdown"
        LINK -> "text/uri-list"
        FOLDER -> "inode/directory"
        else -> android.webkit.MimeTypeMap.getSingleton()
            .getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase())
            ?: "application/octet-stream"
    }

    fun isText(mime: String, name: String): Boolean =
        mime.startsWith("text/") || mime in setOf("application/json", "application/xml",
            "application/x-yaml", "application/javascript") ||
            name.substringAfterLast('.', "").lowercase() in setOf(
                "txt", "md", "json", "csv", "yaml", "yml", "xml", "log", "py", "kt", "js", "ts", "html")
}

data class Item(
    val id: String,
    val parent: String?,
    val kind: String,
    val name: String,
    val mime: String,
    val size: Long,
    val body: String?,
    val created: Long,
    val updated: Long,
    val createdBy: String,
    val updatedBy: String,
    val starred: Boolean,
    val trashed: Long?,
    val tags: List<String>,
    /** What it is, in a sentence, and what matters in it: written by an AI. */
    val about: String? = null,
) {
    val isImage get() = kind == Kind.FILE && mime.startsWith("image/")
    val isPdf get() = kind == Kind.FILE && mime == "application/pdf"
    val byAi get() = createdBy != "you" && createdBy != "shield"

    /** For the API. [full] adds the text (a note, or a text file's contents). */
    fun json(full: Boolean = false): JSONObject = JSONObject().apply {
        put("id", id); put("parent", parent ?: JSONObject.NULL); put("kind", kind)
        put("name", name); put("mime", mime); put("size", size)
        put("created", created); put("updated", updated)
        put("created_by", createdBy); put("updated_by", updatedBy)
        put("starred", starred); put("trashed", trashed ?: JSONObject.NULL)
        put("tags", JSONArray(tags))
        put("has_content", kind == Kind.FILE)
        put("about", about ?: JSONObject.NULL)
        if (full) put("text", body ?: JSONObject.NULL)
        else body?.let { put("preview", it.take(200)) }
    }
}

data class Change(val item: String, val at: Long, val by: String, val action: String, val detail: String) {
    fun json(): JSONObject = JSONObject().apply {
        put("item", item); put("at", at); put("by", by); put("action", action); put("detail", detail)
    }
}
