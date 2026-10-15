package com.hrishikesh.shield

import android.util.Base64
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import org.json.JSONArray
import org.json.JSONObject
import java.io.FileInputStream

/**
 * Shield's API: how every AI he builds reads and writes what he can see.
 *
 * Plain HTTP + JSON on port [PORT], on every network the phone is on, which
 * with Tailscale means from anywhere, and only to his own devices. Every
 * request needs the token (`Authorization: Bearer <token>`); a wrong one
 * gets nothing, not even whether an item exists.
 *
 * Every request also says who is asking (`X-Shield-Agent: jarvis`). That name
 * is written against everything it adds or changes, which is how the app can
 * show "Jarvis added this", and why an AI cannot pass itself off as him: the
 * name "you" is refused here.
 *
 *   GET    /v1/health
 *   GET    /v1/items?parent=<id>|&path=/A/B&trash=1   a folder's contents
 *   GET    /v1/items/<id>                              one item, with its text
 *   GET    /v1/items/<id>/content                      a file's bytes
 *   GET    /v1/items/<id>/history                      who did what to it
 *   GET    /v1/items/<id>/links
 *   GET    /v1/search?q=&kind=&tag=&starred=1&by=&limit=
 *   GET    /v1/resolve?path=/A/B
 *   GET    /v1/tags · /v1/activity?limit=&by=
 *   POST   /v1/items        {parent|path, kind, name, text, data_b64, mime, tags}
 *   POST   /v1/upload?parent=|path=&name=&mime=        raw bytes as the body
 *   POST   /v1/items/<id>   {name, parent, path, text, about, starred, tags, add_tags, remove_tags}
 *   POST   /v1/items/<id>/restore
 *   DELETE /v1/items/<id>   to the Trash, never further; only he empties it
 *   POST   /v1/links        {a, b, label}
 */
class Api(
    private val assets: android.content.res.AssetManager,
    private val token: () -> String,
    private val owner: () -> String,
) : NanoHTTPD(PORT) {

    companion object {
        const val PORT = 8791
        private const val TAG = "shield.api"
        private const val MAX_BODY = 60 * 1024 * 1024
        private val AGENT = Regex("^[a-z0-9][a-z0-9 _.-]{0,39}$")
    }

    private class Problem(val status: Response.Status, message: String) : Exception(message)

    override fun serve(session: IHTTPSession): Response = try {
        if (session.method == Method.GET && session.uri in setOf("/", "/index.html")) {
            page()
        } else {
            val agent = if (isOwner(session)) "you" else {
                authorise(session)
                agent(session)
            }
            route(session, agent)
        }
    } catch (p: Problem) {
        error(p.status, p.message ?: "error")
    } catch (e: IllegalArgumentException) {
        error(Response.Status.BAD_REQUEST, e.message ?: "bad request")
    } catch (e: NoSuchElementException) {
        error(Response.Status.NOT_FOUND, e.message ?: "not found")
    } catch (e: Exception) {
        Log.w(TAG, "request failed: ${session.method} ${session.uri}", e)
        error(Response.Status.INTERNAL_ERROR, "Shield could not do that: ${e.message}")
    }

    /** Shield in a browser: the page itself is public, everything it shows is not. */
    private fun page(): Response = newFixedLengthResponse(Response.Status.OK, "text/html; charset=utf-8",
        assets.open("web/index.html").use { String(it.readBytes()) })

    private fun isOwner(session: IHTTPSession): Boolean {
        val given = session.headers["x-shield-owner"]?.trim()
        return !given.isNullOrEmpty() && constantTimeEquals(given, owner())
    }

    private fun authorise(session: IHTTPSession) {
        val headers = session.headers
        val given = headers["authorization"]?.removePrefix("Bearer ")?.trim()
            ?: headers["x-shield-token"]?.trim()
            ?: session.parms["token"]
        if (given.isNullOrEmpty() || !constantTimeEquals(given, token())) {
            throw Problem(Response.Status.UNAUTHORIZED, "a valid Shield token is needed")
        }
    }

    private fun agent(session: IHTTPSession): String {
        val name = (session.headers["x-shield-agent"] ?: "ai").trim().lowercase()
        if (!AGENT.matches(name) || name == "you" || name == "shield") {
            throw Problem(Response.Status.BAD_REQUEST,
                "X-Shield-Agent must be your AI's name: lowercase letters, digits, _ . -")
        }
        return name
    }

    private fun route(s: IHTTPSession, agent: String): Response {
        val parts = s.uri.trim('/').split('/')
        val q = s.parms
        if (parts.firstOrNull() != "v1") throw Problem(Response.Status.NOT_FOUND, "unknown path")
        val method = s.method
        return when {
            parts.size == 2 && parts[1] == "health" ->
                ok(JSONObject().put("ok", true).put("items", Store.count()).put("you_are", agent))

            parts.size == 2 && parts[1] == "items" && method == Method.GET -> {
                val folder = folderFrom(q["parent"], q["path"], create = false, agent = agent)
                val trash = q["trash"] == "1"
                ok(JSONObject()
                    .put("folder", folder?.json() ?: JSONObject.NULL)
                    .put("path", JSONArray(Store.path(folder?.id).map { it.json() } +
                        listOfNotNull(folder?.json())))
                    .put("items", JSONArray(Store.list(folder?.id, trash).map { it.json() })))
            }

            parts.size == 2 && parts[1] == "items" && method == Method.POST -> {
                val body = json(s)
                val folder = into(body.optString("parent"), body.optString("path"), agent)
                val data = body.optString("data_b64").ifEmpty { null }
                    ?.let { Base64.decode(it, Base64.DEFAULT) }
                val kind = body.optString("kind").ifEmpty { if (data != null) Kind.FILE else Kind.NOTE }
                val item = Store.create(
                    parent = folder?.id, kind = kind, name = body.optString("name"),
                    body = body.optString("text").ifEmpty { null }, bytes = data,
                    mime = body.optString("mime"), tags = strings(body.optJSONArray("tags")), by = agent)
                ok(item.json(full = true), Response.Status.CREATED)
            }

            parts.size == 2 && parts[1] == "upload" && method == Method.POST -> {
                val folder = into(q["parent"].orEmpty(), q["path"].orEmpty(), agent)
                val item = Store.create(
                    parent = folder?.id, kind = Kind.FILE, name = q["name"] ?: "upload",
                    bytes = raw(s), mime = q["mime"].orEmpty(),
                    tags = q["tags"]?.split(',').orEmpty(), by = agent)
                ok(item.json(), Response.Status.CREATED)
            }

            parts.size == 3 && parts[1] == "items" -> {
                val id = parts[2]
                when (method) {
                    Method.GET -> ok(found(id).json(full = true))
                    Method.POST -> {
                        found(id)
                        val body = json(s)
                        val target = body.optString("path").ifEmpty { null }
                            ?.let { Store.folderAt(it, agent)?.id }
                            ?: body.optString("parent").ifEmpty { null }
                        val toTop = body.has("parent") && body.isNull("parent")
                        ok(Store.update(
                            id, by = agent,
                            name = body.optString("name").ifEmpty { null },
                            parent = target, moveToTop = toTop,
                            body = if (body.has("text")) body.optString("text") else null,
                            starred = if (body.has("starred")) body.optBoolean("starred") else null,
                            about = if (body.has("about")) body.optString("about") else null,
                            tags = body.optJSONArray("tags")?.let(::strings),
                            addTags = strings(body.optJSONArray("add_tags")),
                            removeTags = strings(body.optJSONArray("remove_tags")),
                        ).json(full = true))
                    }
                    Method.DELETE -> {
                        // To the Trash, never further: only he empties it, in
                        // the app. An AI that gets something wrong can always
                        // be undone.
                        val item = found(id)
                        if (item.trashed != null && agent == "you" && q["forever"] == "1") {
                            // Him, from the browser: the one way past the Trash.
                            Store.purge(id, agent)
                            return ok(JSONObject().put("ok", true))
                        }
                        if (item.trashed != null) {
                            throw Problem(Response.Status.CONFLICT,
                                "already in the Trash; only he can delete things for good")
                        }
                        Store.trash(id, agent)
                        ok(JSONObject().put("ok", true))
                    }
                    else -> throw Problem(Response.Status.METHOD_NOT_ALLOWED, "not allowed")
                }
            }

            parts.size == 4 && parts[1] == "items" -> {
                val item = found(parts[2])
                when (parts[3]) {
                    "content" -> {
                        val file = Store.content(item.id)
                        if (item.kind != Kind.FILE || !file.exists()) {
                            throw Problem(Response.Status.NOT_FOUND, "this item has no file")
                        }
                        newFixedLengthResponse(Response.Status.OK, item.mime,
                            FileInputStream(file), file.length())
                    }
                    "history" -> ok(JSONObject().put("history",
                        JSONArray(Store.history(item.id).map { it.json() })))
                    "links" -> ok(JSONObject().put("links", JSONArray(Store.links(item.id).map { (other, label) ->
                        other.json().put("label", label)
                    })))
                    "restore" -> {
                        Store.restore(item.id, agent)
                        ok(Store.get(item.id)!!.json())
                    }
                    else -> throw Problem(Response.Status.NOT_FOUND, "unknown path")
                }
            }

            parts.size == 2 && parts[1] == "search" -> ok(JSONObject().put("items", JSONArray(
                Store.search(
                    query = q["q"].orEmpty(), kind = q["kind"], tag = q["tag"],
                    starred = q["starred"] == "1", by = q["by"],
                    limit = q["limit"]?.toIntOrNull()?.coerceIn(1, 200) ?: 50,
                ).map { it.json() })))

            parts.size == 2 && parts[1] == "resolve" -> {
                val item = Store.resolve(q["path"].orEmpty())
                    ?: throw Problem(Response.Status.NOT_FOUND, "nothing at ${q["path"]}")
                ok(item.json(full = true))
            }

            parts.size == 2 && parts[1] == "tags" -> ok(JSONObject().put("tags", JSONArray(
                Store.tags().map { (tag, n) -> JSONObject().put("tag", tag).put("count", n) })))

            parts.size == 2 && parts[1] == "activity" -> ok(JSONObject().put("activity", JSONArray(
                Store.activity(q["limit"]?.toIntOrNull()?.coerceIn(1, 500) ?: 100, q["by"])
                    .map { it.json() })))

            parts.size == 2 && parts[1] == "links" && method == Method.POST -> {
                val body = json(s)
                Store.link(body.getString("a"), body.getString("b"), body.optString("label"), agent)
                ok(JSONObject().put("ok", true))
            }

            else -> throw Problem(Response.Status.NOT_FOUND, "unknown path")
        }
    }

    // ── the details ─────────────────────────────────────────────────────────
    private fun folderFrom(parent: String?, path: String?, create: Boolean, agent: String): Item? {
        if (!path.isNullOrBlank()) {
            return if (create) Store.folderAt(path, agent)
            else Store.resolve(path)?.takeIf { it.kind == Kind.FOLDER }
                ?: throw Problem(Response.Status.NOT_FOUND, "no folder at $path")
        }
        if (parent.isNullOrBlank() || parent == "root") return null
        return Store.get(parent)?.takeIf { it.kind == Kind.FOLDER }
            ?: throw Problem(Response.Status.NOT_FOUND, "no folder $parent")
    }

    /**
     * Where a new item goes: the folder named, the top level for "/" (or
     * parent "root"), and the Inbox when nothing is said at all.
     */
    private fun into(parent: String, path: String, agent: String): Item? {
        if (path.trim() == "/" || parent == "root") return null
        if (parent.isBlank() && path.isBlank()) return Store.resolve("/${Store.INBOX}")
        return folderFrom(parent.ifBlank { null }, path.ifBlank { null }, create = true, agent = agent)
    }

    private fun found(id: String): Item =
        Store.get(id) ?: throw Problem(Response.Status.NOT_FOUND, "no item $id")

    private fun raw(s: IHTTPSession): ByteArray {
        val length = s.headers["content-length"]?.toIntOrNull()
            ?: throw Problem(Response.Status.LENGTH_REQUIRED, "Content-Length is needed")
        if (length > MAX_BODY) throw Problem(Response.Status.PAYLOAD_TOO_LARGE, "60 MB at most")
        val bytes = ByteArray(length)
        var read = 0
        while (read < length) {
            val n = s.inputStream.read(bytes, read, length - read)
            if (n < 0) break
            read += n
        }
        return if (read == length) bytes else bytes.copyOf(read)
    }

    private fun json(s: IHTTPSession): JSONObject {
        val text = String(raw(s))
        return runCatching { JSONObject(text.ifBlank { "{}" }) }
            .getOrElse { throw Problem(Response.Status.BAD_REQUEST, "the body must be JSON") }
    }

    private fun strings(array: JSONArray?): List<String> =
        array?.let { a -> List(a.length()) { a.optString(it) }.filter { it.isNotBlank() } }.orEmpty()

    private fun ok(body: JSONObject, status: Response.Status = Response.Status.OK): Response =
        newFixedLengthResponse(status, "application/json; charset=utf-8", body.toString())

    private fun error(status: Response.Status, message: String): Response =
        newFixedLengthResponse(status, "application/json; charset=utf-8",
            JSONObject().put("error", message).toString())

    private fun constantTimeEquals(a: String, b: String): Boolean {
        if (a.length != b.length) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].code xor b[i].code)
        return diff == 0
    }
}
