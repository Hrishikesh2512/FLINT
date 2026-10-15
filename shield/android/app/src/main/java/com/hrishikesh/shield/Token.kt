package com.hrishikesh.shield

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import java.security.SecureRandom

/**
 * The one secret the API needs. Made on first use, kept in the app's private
 * storage, shown (and replaceable) in Settings. Replacing it locks out every
 * AI that had the old one until it is given the new one.
 */
object Token {
    private const val PREFS = "shield"
    private const val KEY = "api_token"
    private const val OWNER = "owner_key"

    /**
     * His own key, for Shield in a browser: requests with it act as "you".
     * Separate from the AI token on purpose, so no AI can pass as him.
     */
    fun owner(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString(OWNER, null) ?: renewOwner(context)
    }

    fun renewOwner(context: Context): String {
        val key = ByteArray(24).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(OWNER, key).apply()
        return key
    }

    fun get(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString(KEY, null) ?: renew(context)
    }

    fun renew(context: Context): String {
        val bytes = ByteArray(24).also { SecureRandom().nextBytes(it) }
        val token = bytes.joinToString("") { "%02x".format(it) }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, token).apply()
        return token
    }
}

/**
 * Hands the token to Jarvis on the same phone, so he never has to paste it
 * there. Readable only by apps holding Shield's signature permission, apps
 * signed with the same key as Shield, which means his own.
 */
class TokenProvider : ContentProvider() {
    override fun onCreate() = true

    override fun query(
        uri: Uri, projection: Array<out String>?, selection: String?,
        selectionArgs: Array<out String>?, sortOrder: String?,
    ): Cursor = MatrixCursor(arrayOf("token", "port")).apply {
        addRow(arrayOf<Any>(Token.get(context!!), Api.PORT))
    }

    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?) = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?,
                        selectionArgs: Array<out String>?) = 0
}
