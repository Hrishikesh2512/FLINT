package com.hrishikesh.carnage

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.speech.RecognizerIntent
import android.speech.tts.TextToSpeech
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.isVisible
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.lifecycle.lifecycleScope
import java.util.Locale

/**
 * The screen. Plain Android views rather than Compose, deliberately: this is a
 * conversation — a scrolling list of turns and a box to type in — and classic
 * views carry no compiler-version coupling for a UI that simple.
 *
 * The Activity owns nothing important. She lives in the service, so closing
 * this screen stops the conversation being *visible* and changes nothing about
 * whether she is running.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var log: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var input: EditText
    private lateinit var send: Button
    private var speaker: TextToSpeech? = null

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { /* a refusal is not an error — the matching skill just stays off */ }

    private val dictation = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val heard = result.data
            ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            ?.firstOrNull()
        if (!heard.isNullOrBlank()) ask(heard)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        log = findViewById(R.id.log)
        scroll = findViewById(R.id.scroll)
        input = findViewById(R.id.input)
        send = findViewById(R.id.send)

        CarnageService.start(this)
        askForWhatSheNeeds()

        speaker = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) {
                // Hinglish reads far better on an Indian English voice; the
                // default sounds American and mangles the Hindi words.
                speaker?.language = Locale("en", "IN")
            }
        }

        send.setOnClickListener { ask(input.text.toString()) }
        findViewById<Button>(R.id.mic).setOnClickListener { listen() }
        findViewById<Button>(R.id.shade).setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        note(getString(R.string.opening_note))
        refreshStatus()
    }

    override fun onDestroy() {
        speaker?.shutdown()
        super.onDestroy()
    }

    // ── talking ─────────────────────────────────────────────────────────────
    private fun ask(said: String) {
        val text = said.trim()
        if (text.isEmpty()) return
        input.setText("")
        send.isEnabled = false
        turn(text, mine = true)
        val waiting = turn("…", mine = false)

        lifecycleScope.launch {
            val reply = withContext(Dispatchers.IO) { Brain.ask(text) }
            waiting.text = reply
            scrollDown()
            send.isEnabled = true
            speaker?.speak(reply, TextToSpeech.QUEUE_FLUSH, null, "her")
        }
    }

    private fun listen() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
            putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.listening))
        }
        runCatching { dictation.launch(intent) }
            .onFailure { note(getString(R.string.no_dictation)) }
    }

    // ── the transcript ──────────────────────────────────────────────────────
    private fun turn(text: String, mine: Boolean): TextView {
        val view = TextView(this).apply {
            this.text = text
            setPadding(36, 26, 36, 26)
            setTextColor(getColor(if (mine) R.color.him else R.color.ink))
            setBackgroundResource(if (mine) R.drawable.bubble_him else R.drawable.bubble_her)
            textSize = 16f
        }
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply {
            topMargin = 14
            marginStart = if (mine) 120 else 0
            marginEnd = if (mine) 0 else 120
            gravity = if (mine) android.view.Gravity.END else android.view.Gravity.START
        }
        log.addView(view, params)
        scrollDown()
        return view
    }

    private fun note(text: String) {
        log.addView(TextView(this).apply {
            this.text = text
            setPadding(24, 22, 24, 22)
            setTextColor(getColor(R.color.dim))
            textSize = 13f
            gravity = android.view.Gravity.CENTER
        })
    }

    private fun scrollDown() {
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun refreshStatus() {
        lifecycleScope.launch {
            val described = withContext(Dispatchers.IO) { Brain.describe() }
            findViewById<TextView>(R.id.status).apply {
                text = described
                isVisible = true
            }
        }
    }

    // ── permissions ─────────────────────────────────────────────────────────
    private fun askForWhatSheNeeds() {
        // Asked together, up front, because each one maps to a skill she will
        // otherwise have to apologise for mid-conversation. The shade is not
        // here: it needs a settings screen, and there is a button for it.
        val wanted = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.SEND_SMS,
            Manifest.permission.RECORD_AUDIO,
        )
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        permissions.launch(wanted.toTypedArray())
    }
}
