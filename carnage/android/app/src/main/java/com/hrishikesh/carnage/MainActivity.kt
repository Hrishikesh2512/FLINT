package com.hrishikesh.carnage

import android.Manifest
import android.animation.Animator
import android.animation.AnimatorSet
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.Intent
import android.graphics.RenderEffect
import android.graphics.Shader
import android.os.Build
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.view.Gravity
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

/**
 * Carnage's one screen: Jarvis, behind glass.
 *
 * Plain Android views rather than Compose, deliberately — a header, a
 * conversation, a glowing core to tap and a box to type in carry no
 * compiler-version coupling. The glass is drawables (bg_app, glass_card,
 * orb_core); the life in it is a couple of animators on the core.
 *
 * The Activity owns nothing important. Jarvis lives in the service, so closing
 * this screen stops the conversation being *visible* and changes nothing about
 * whether Jarvis is running.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        /** Open straight into a conversation (the tile, the notification). */
        const val ACTION_TALK = "com.hrishikesh.carnage.action.TALK"
    }

    private lateinit var log: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var input: EditText
    private lateinit var send: ImageButton
    private lateinit var mic: TextView
    private lateinit var glow: View
    private lateinit var ring: View
    private lateinit var voiceState: TextView
    private var speaker: TextToSpeech? = null
    private var pulse: Animator? = null
    private var shown = ""
    /** Opened to talk (tile, Jarvis Talk, side button): close when the call ends. */
    private var talkMode = false

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        // A refusal is not an error — the matching skill just stays off. A
        // grant of the mic or camera is claimed by the service now, while this
        // screen is visible, so both keep working with the screen off.
        if (granted[Manifest.permission.RECORD_AUDIO] == true ||
            granted[Manifest.permission.CAMERA] == true
        ) {
            CarnageService.claimMic(this)
        }
    }

    // The live call reports here while this screen is open.
    private val voice = object : LiveVoice.Listener {
        override fun onState(state: String) = show(state)

        override fun onLine(who: String, text: String) {
            turn(text, mine = who == "you")
            // No headphones means no voice: put her reply under the core too,
            // where it can be read even over the lock screen.
            if (who != "you" && !Headphones.connected(this@MainActivity)) {
                voiceState.text = text
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Opened while the phone is locked (Samsung's side-button double
        // press, a lock-screen shortcut) means "let me talk": show over the
        // lock screen and start listening, no unlock needed.
        talkMode = wantsTalk(intent) || locked()
        if (talkMode) showOverLockScreen()
        setContentView(R.layout.activity_main)

        log = findViewById(R.id.log)
        scroll = findViewById(R.id.scroll)
        input = findViewById(R.id.input)
        send = findViewById(R.id.send)
        mic = findViewById(R.id.mic)
        glow = findViewById(R.id.glow)
        ring = findViewById(R.id.ring)
        voiceState = findViewById(R.id.voiceState)

        // Real blur on the halo where the platform has it (Android 12+), so
        // the core glows through the glass instead of sitting on it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            glow.setRenderEffect(RenderEffect.createBlurEffect(36f, 36f, Shader.TileMode.DECAL))
        }

        // While typing, the keyboard takes half the screen: tuck the core away
        // so the conversation keeps the room instead of being squeezed out.
        // Listened for on the outermost view: the inner column fits system
        // windows and would consume the keyboard insets before the core saw them.
        val core = findViewById<View>(R.id.core)
        val root = findViewById<android.view.ViewGroup>(android.R.id.content).getChildAt(0)
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val typing = insets.isVisible(WindowInsetsCompat.Type.ime())
            core.isVisible = !typing
            voiceState.isVisible = !typing
            insets
        }

        CarnageService.start(this)
        askForWhatJarvisNeeds()

        // A first launch has no key, and the config it lives in is unreachable
        // from any file manager on Android 11+. Sending him to setup is not a
        // nicety — it is the only route there is.
        lifecycleScope.launch {
            val fresh = withContext(Dispatchers.IO) {
                Brain.start(applicationContext)
                Brain.needsSetup()
            }
            if (fresh) {
                startActivity(Intent(this@MainActivity, SetupActivity::class.java))
            }
        }

        speaker = TextToSpeech(this) { result ->
            if (result == TextToSpeech.SUCCESS) {
                // Hinglish reads far better on an Indian English voice; the
                // default sounds American and mangles the Hindi words.
                speaker?.language = Locale("en", "IN")
            }
        }

        send.setOnClickListener { ask(input.text.toString()) }
        mic.setOnClickListener {
            it.animate().scaleX(0.92f).scaleY(0.92f).setDuration(90).withEndAction {
                it.animate().scaleX(1f).scaleY(1f).setDuration(160).start()
            }.start()
            LiveVoice.toggle(this)
        }
        LiveVoice.addListener(voice)
        findViewById<View>(R.id.setup).setOnClickListener {
            startActivity(Intent(this, SetupActivity::class.java))
        }

        show(if (LiveVoice.active) "listening" else "off")
        if (talkMode) startTalking()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (wantsTalk(intent) || locked()) {
            talkMode = true
            showOverLockScreen()
            startTalking()
        }
    }

    // ── talk mode: one tap from the lock screen ─────────────────────────────
    private fun wantsTalk(intent: Intent?): Boolean = intent?.action == ACTION_TALK

    /** Over the lock screen, screen on — the conversation stays hidden there. */
    private fun showOverLockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
            )
        }
    }

    private fun startTalking() {
        if (!LiveVoice.active) LiveVoice.start(this)
    }

    /** True while the phone is locked: nothing private may be on screen. */
    private fun locked(): Boolean =
        getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked

    override fun onResume() {
        super.onResume()
        // Over the lock screen only the core shows — never the conversation.
        val unlocked = !locked()
        findViewById<View>(R.id.conversation).isVisible = unlocked
        findViewById<View>(R.id.composer).isVisible = unlocked
        // Claim the mic and camera for screen-off use while this screen is
        // visible — the only moment Android allows it. Done on every open, so
        // a grant made in Settings later is picked up too.
        val mic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        val camera = checkSelfPermission(Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        if (mic || camera) CarnageService.claimMic(this)
    }

    override fun onDestroy() {
        LiveVoice.removeListener(voice)
        pulse?.cancel()
        speaker?.shutdown()
        super.onDestroy()
    }

    // ── the core: idle, listening, speaking ─────────────────────────────────
    private fun show(state: String) {
        mic.text = getString(if (LiveVoice.active) R.string.hang_up else R.string.talk)
        // "speaking" arrives with every chunk of audio: only a real change of
        // state may restart the animation, or the core would stutter.
        if (state == shown) return
        shown = state
        when (state) {
            "connecting" -> {
                voiceState.text = getString(R.string.connecting)
                animateCore(period = 700, reach = 1.10f, brightest = 0.85f)
            }
            "listening" -> {
                voiceState.text = getString(R.string.listening)
                animateCore(period = 1100, reach = 1.16f, brightest = 1f)
            }
            "speaking" -> {
                if (Headphones.connected(this)) voiceState.text = getString(R.string.speaking)
                animateCore(period = 420, reach = 1.22f, brightest = 1f)
            }
            "off" -> {
                voiceState.text = getString(R.string.tap_to_talk)
                // Opened just to talk: when the call is over, get out of the way
                // (back to the lock screen, or wherever he was).
                if (talkMode && shown.isNotEmpty() && pulse != null) {
                    talkMode = false
                    finish()
                }
                animateCore(period = 3200, reach = 1.04f, brightest = 0.55f)
            }
            else -> note(state)        // a sentence from LiveVoice: say it
        }
    }

    /** Breathe the halo and ring: slow when idle, quick when she speaks. */
    private fun animateCore(period: Long, reach: Float, brightest: Float) {
        pulse?.cancel()
        fun breathe(view: View, scale: Float, alpha: Float): List<ObjectAnimator> =
            listOf(
                ObjectAnimator.ofFloat(view, View.SCALE_X, 1f, scale),
                ObjectAnimator.ofFloat(view, View.SCALE_Y, 1f, scale),
                ObjectAnimator.ofFloat(view, View.ALPHA, alpha * 0.55f, alpha),
            ).onEach {
                it.duration = period
                it.repeatMode = ValueAnimator.REVERSE
                it.repeatCount = ValueAnimator.INFINITE
                it.interpolator = AccelerateDecelerateInterpolator()
            }
        pulse = AnimatorSet().apply {
            playTogether(breathe(glow, reach, brightest) + breathe(ring, 1f + (reach - 1f) / 2, brightest))
            start()
        }
    }

    // ── typing ──────────────────────────────────────────────────────────────
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
            if (Headphones.connected(this@MainActivity)) {
                speaker?.speak(reply, TextToSpeech.QUEUE_FLUSH, null, "jarvis")
            }
        }
    }

    // ── the transcript ──────────────────────────────────────────────────────
    private fun turn(text: String, mine: Boolean): TextView {
        val view = TextView(this).apply {
            this.text = text
            setPadding(dp(16), dp(11), dp(16), dp(11))
            setTextColor(getColor(R.color.ink))
            setBackgroundResource(if (mine) R.drawable.bubble_him else R.drawable.bubble_her)
            textSize = 15f
            setLineSpacing(0f, 1.15f)
            alpha = 0f
            translationY = dp(10).toFloat()
        }
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply {
            topMargin = dp(10)
            marginStart = if (mine) dp(48) else 0
            marginEnd = if (mine) 0 else dp(48)
            gravity = if (mine) Gravity.END else Gravity.START
        }
        log.addView(view, params)
        view.animate().alpha(1f).translationY(0f).setDuration(220).start()
        scrollDown()
        return view
    }

    private fun note(text: String) {
        log.addView(TextView(this).apply {
            this.text = text
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setTextColor(getColor(R.color.dim))
            textSize = 12f
            gravity = Gravity.CENTER
        }, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ))
        scrollDown()
    }

    private fun scrollDown() {
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }


    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    // ── permissions ─────────────────────────────────────────────────────────
    private fun askForWhatJarvisNeeds() {
        // Asked together, up front, because each one maps to a skill Jarvis
        // would otherwise have to apologise for mid-conversation. The shade is
        // not here: it needs a settings screen, and there is a button for it.
        val wanted = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.SEND_SMS,
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CAMERA,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            // Routing a call to the Bluetooth earphone's mic.
            wanted += Manifest.permission.BLUETOOTH_CONNECT
        }
        // Never over the lock screen (a dialog there is in the way of talking),
        // and only what is still missing — once answered, not asked again on
        // every open.
        if (locked()) return
        val missing = wanted.filter {
            checkSelfPermission(it) != android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        val asked = getSharedPreferences("carnage", MODE_PRIVATE)
        val fresh = missing.filterNot { asked.getBoolean("asked:$it", false) }
        if (fresh.isEmpty()) return
        asked.edit().apply { fresh.forEach { putBoolean("asked:$it", true) } }.apply()
        permissions.launch(fresh.toTypedArray())
    }
}
