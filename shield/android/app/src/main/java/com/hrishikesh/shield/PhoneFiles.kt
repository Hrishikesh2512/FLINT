package com.hrishikesh.shield

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import android.webkit.MimeTypeMap
import androidx.core.content.FileProvider
import java.io.File

/**
 * The phone's own storage, for Shield as an everyday file explorer: Downloads,
 * Camera, Documents, WhatsApp media, everything. Needs "All files access",
 * which he grants once in Settings.
 *
 * These files stay where they are. "Save to Shield" copies one into Shield,
 * which is what makes it visible to his AIs; the rest of the phone is not.
 */
object PhoneFiles {

    val root: File get() = Environment.getExternalStorageDirectory()

    /** Quick ways in, shown at the top of the phone's storage. */
    fun places(): List<Pair<String, File>> = listOf(
        "Downloads" to Environment.DIRECTORY_DOWNLOADS,
        "Camera" to "${Environment.DIRECTORY_DCIM}/Camera",
        "Screenshots" to "${Environment.DIRECTORY_PICTURES}/Screenshots",
        "Documents" to Environment.DIRECTORY_DOCUMENTS,
        "WhatsApp" to "Android/media/com.whatsapp/WhatsApp/Media",
    ).map { (label, path) -> label to File(root, path) }.filter { it.second.isDirectory }

    fun allowed(context: Context): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) Environment.isExternalStorageManager()
        else context.checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) ==
            PackageManager.PERMISSION_GRANTED

    fun askAccess(activity: Activity) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val mine = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                Uri.parse("package:${activity.packageName}"))
            runCatching { activity.startActivity(mine) }.onFailure {
                activity.startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            activity.requestPermissions(arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE,
                Manifest.permission.WRITE_EXTERNAL_STORAGE), 7)
        }
    }

    /** A folder's contents: folders first, then by name; hidden files left out. */
    fun list(dir: File): List<File> =
        (dir.listFiles() ?: emptyArray())
            .filter { !it.name.startsWith(".") }
            .sortedWith(compareBy<File>({ !it.isDirectory }, { it.name.lowercase() }))

    /** Names under [dir] containing every word of [query]; stops at [limit]. */
    /** Other apps' private folders: Android shows them empty anyway. */
    private val sealed get() = setOf("${root.path}/Android/data", "${root.path}/Android/obb")

    fun search(dir: File, query: String, limit: Int = 200): List<File> {
        val words = query.lowercase().split(' ').filter { it.isNotBlank() }
        if (words.isEmpty()) return emptyList()
        val out = mutableListOf<File>()
        dir.walkTopDown()
            .onEnter { it == dir || (!it.name.startsWith(".") && it.path !in sealed) }
            .maxDepth(12)
            .forEach { file ->
                if (out.size >= limit) return out
                val name = file.name.lowercase()
                if (file != dir && words.all { it in name }) out += file
            }
        return out
    }

    fun mime(file: File): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension.lowercase())
            ?: "application/octet-stream"

    fun isImage(file: File) = mime(file).startsWith("image/")

    fun open(context: Context, file: File) {
        val uri = FileProvider.getUriForFile(context, "com.hrishikesh.shield.files", file)
        runCatching {
            context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime(file))
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        }.onFailure { Ui.toast(context, "No app can open ${file.name}") }
    }

    fun share(context: Context, file: File) {
        val uri = FileProvider.getUriForFile(context, "com.hrishikesh.shield.files", file)
        context.startActivity(Intent.createChooser(
            Intent(Intent.ACTION_SEND).setType(mime(file)).putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), file.name))
    }

    /** A copy into Shield's Inbox, so his AIs can use it. Blocking. */
    fun saveToShield(file: File): Item? = runCatching {
        Store.create(Store.resolve("/${Store.INBOX}")?.id, Kind.FILE, file.name,
            bytes = file.readBytes(), mime = mime(file), by = "you")
    }.getOrNull()

    fun meta(file: File): String =
        if (file.isDirectory) "Folder · ${file.list()?.count { !it.startsWith(".") } ?: 0} items"
        else "${Ui.size(file.length())} · ${Ui.ago(file.lastModified())}"
}
