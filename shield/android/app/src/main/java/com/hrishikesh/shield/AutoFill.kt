package com.hrishikesh.shield

import android.content.Context
import android.os.Environment
import android.util.Log
import java.io.File

/**
 * Shield fills itself: new screenshots, downloads, documents and WhatsApp
 * documents are copied into the Inbox as they appear, tagged "auto", for
 * Jarvis to name, sort and read (carnage/shield_organizer.py).
 *
 * Only what is new since it was switched on: turning it on does not pour the
 * phone's whole history in. Videos and anything over [MAX_BYTES] stay out;
 * they are big, and rarely what he means to keep.
 */
object AutoFill {

    private const val TAG = "shield.autofill"
    private const val PREFS = "shield"
    private const val ON = "autofill_on"
    private const val SINCE = "autofill_since"
    const val MAX_BYTES = 25L * 1024 * 1024

    private val KEEP = setOf("jpg", "jpeg", "png", "webp", "heic", "pdf", "doc", "docx", "xls",
        "xlsx", "ppt", "pptx", "txt", "md", "csv", "json", "zip", "epub")

    /** Where new things come from, by what he would call them. */
    fun watched(): List<Pair<String, File>> {
        val root = Environment.getExternalStorageDirectory()
        return listOf(
            "Screenshots" to File(root, "${Environment.DIRECTORY_PICTURES}/Screenshots"),
            "Screenshots" to File(root, "${Environment.DIRECTORY_DCIM}/Screenshots"),
            "Downloads" to File(root, Environment.DIRECTORY_DOWNLOADS),
            "Documents" to File(root, Environment.DIRECTORY_DOCUMENTS),
            "WhatsApp" to File(root, "Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Documents"),
        ).filter { it.second.isDirectory }
    }

    fun isOn(context: Context) = prefs(context).getBoolean(ON, true)

    fun setOn(context: Context, on: Boolean) {
        prefs(context).edit().putBoolean(ON, on).putLong(SINCE, System.currentTimeMillis()).apply()
    }

    /** Bring in what is new. Blocking; returns how many came in. */
    fun scan(context: Context): Int {
        if (!isOn(context) || !PhoneFiles.allowed(context)) return 0
        val prefs = prefs(context)
        val since = prefs.getLong(SINCE, 0L)
        if (since == 0L) {
            // First run: start from now, not from the phone's whole past.
            prefs.edit().putLong(SINCE, System.currentTimeMillis()).apply()
            return 0
        }
        val now = System.currentTimeMillis()
        val inbox = Store.resolve("/${Store.INBOX}")?.id
        var added = 0
        watched().forEach { (from, dir) ->
            dir.listFiles()?.filter { file ->
                file.isFile && file.lastModified() > since && file.lastModified() <= now &&
                    file.length() in 1..MAX_BYTES && file.extension.lowercase() in KEEP &&
                    !file.name.startsWith(".")
            }?.forEach { file ->
                runCatching {
                    Store.create(inbox, Kind.FILE, file.name, bytes = file.readBytes(),
                        mime = PhoneFiles.mime(file), tags = listOf("auto", from.lowercase()),
                        by = "shield")
                    added++
                }.onFailure { Log.w(TAG, "could not bring in ${file.name}", it) }
            }
        }
        prefs.edit().putLong(SINCE, now).apply()
        if (added > 0) Log.i(TAG, "brought in $added new files")
        return added
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}
