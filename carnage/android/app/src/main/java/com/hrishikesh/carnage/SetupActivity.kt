package com.hrishikesh.carnage

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Where the key gets in.
 *
 * Not a nicety. Her config lives in app-private storage, and Android 11 put
 * that out of reach of every file manager — so without this screen there is no
 * way to give her a key at all, on any phone, and the app can be installed but
 * never used. That is the whole reason it exists.
 *
 * It doubles as the pairing screen, because the sync token is generated on the
 * device and is otherwise equally unreachable: the Pi needs it, and reading it
 * off this screen is the only way anyone is going to get it.
 */
class SetupActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_setup)

        val name = findViewById<EditText>(R.id.name)
        val key = findViewById<EditText>(R.id.key)
        val save = findViewById<Button>(R.id.save)
        val pairing = findViewById<TextView>(R.id.pairing)

        lifecycleScope.launch {
            val details = withContext(Dispatchers.IO) {
                Brain.start(applicationContext)
                Brain.pairing()
            }
            val json = runCatching { JSONObject(details) }.getOrNull()
            name.setText(json?.optString("user_name").orEmpty())
            pairing.text = describePairing(json)
        }

        findViewById<Button>(R.id.copyToken).setOnClickListener {
            val token = runCatching { JSONObject(Brain.pairing()).optString("token") }
                .getOrNull().orEmpty()
            if (token.isBlank()) {
                toast(getString(R.string.no_token_yet))
            } else {
                (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                    .setPrimaryClip(ClipData.newPlainText("carnage token", token))
                toast(getString(R.string.token_copied))
            }
        }

        save.setOnClickListener {
            val typedKey = key.text.toString().trim()
            val typedName = name.text.toString().trim()
            if (typedKey.isEmpty() && Brain.needsSetup()) {
                toast(getString(R.string.key_needed))
                return@setOnClickListener
            }
            save.isEnabled = false
            lifecycleScope.launch {
                val described = withContext(Dispatchers.IO) {
                    Brain.configure(typedName, typedKey)
                }
                save.isEnabled = true
                toast(described)
                // Restarting the service picks up the rebuilt assistant, so the
                // hub and her capabilities match the config that was just saved.
                CarnageService.start(this@SetupActivity)
                startActivity(Intent(this@SetupActivity, MainActivity::class.java))
                finish()
            }
        }
    }

    private fun describePairing(json: JSONObject?): String {
        if (json == null) return getString(R.string.pairing_unavailable)
        val token = json.optString("token")
        val port = json.optInt("port", 8790)
        return getString(R.string.pairing_body, token.ifBlank { "—" }, port)
    }

    private fun toast(text: String) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show()
    }
}
