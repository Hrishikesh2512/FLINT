package com.hrishikesh.carnage

import android.Manifest
import android.animation.Animator
import android.animation.AnimatorSet
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import android.graphics.RenderEffect
import android.graphics.Shader
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import android.view.TextureView
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
    private lateinit var visionRow: View
    private lateinit var shareCamera: TextView
    private lateinit var shareScreen: TextView
    private lateinit var cameraView: TextureView
    private lateinit var attachedRow: View
    private lateinit var attached: LinearLayout
    private var pulse: Animator? = null

    /** What he has attached or shared, kept until he removes it. */
    private data class Staged(val name: String, val mime: String, val uri: Uri?, val text: String?)
    private val staged = mutableListOf<Staged>()
    /** [staged] read and packed, so follow-ups do not read it all again. */
    @Volatile private var stagedItems: String? = null

    /** Android's "start recording your screen?" — then the service shares it. */
    private val screenConsent = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val consent = result.data
        if (result.resultCode == RESULT_OK && consent != null && LiveVoice.active) {
            CarnageService.shareScreen(this, result.resultCode, consent)
        }
    }

    private val pickFiles = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris -> uris.forEach(::stageUri) }
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
        visionRow = findViewById(R.id.visionRow)
        shareCamera = findViewById(R.id.shareCamera)
        shareScreen = findViewById(R.id.shareScreen)
        cameraView = findViewById(R.id.cameraView)
        shareCamera.setOnClickListener {
            if (Vision.source == Vision.Source.CAMERA) Vision.stop() else showCamera()
        }
        shareScreen.setOnClickListener {
            if (Vision.source == Vision.Source.SCREEN) {
                Vision.stop()
            } else {
                screenConsent.launch(
                    getSystemService(android.media.projection.MediaProjectionManager::class.java)
                        .createScreenCaptureIntent()
                )
            }
        }
        Vision.onChange = { showVision() }
        attachedRow = findViewById(R.id.attachedRow)
        attached = findViewById(R.id.attached)
        findViewById<View>(R.id.attach).setOnClickListener {
            pickFiles.launch(arrayOf("image/*", "application/pdf"))
        }

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
        takeShare(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        takeShare(intent)
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

    override fun onPause() {
        Actions.onScreen = false
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        Actions.onScreen = true
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

    override fun onStop() {
        // The preview's surface goes with the screen: the camera cannot keep
        // streaming into it. Sharing the screen carries on — that is its point.
        if (Vision.source == Vision.Source.CAMERA) Vision.stop()
        super.onStop()
    }

    override fun onDestroy() {
        LiveVoice.removeListener(voice)
        Vision.onChange = null
        pulse?.cancel()
        super.onDestroy()
    }

    // ── the core: idle, listening, speaking ─────────────────────────────────
    private fun show(state: String) {
        mic.text = getString(if (LiveVoice.active) R.string.hang_up else R.string.talk)
        showVision()
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
        val items = staged.toList()
        if (text.isEmpty() && items.isEmpty()) return
        val question = text.ifEmpty { getString(R.string.whats_this) }
        input.setText("")
        send.isEnabled = false
        val names = items.joinToString("\n") { "📎 ${it.name}" }
        turn(if (names.isEmpty()) question else "$names\n$question", mine = true)
        val waiting = turn("…", mine = false)

        lifecycleScope.launch {
            var streamed = false
            val reply = withContext(Dispatchers.IO) {
                val packed = if (items.isEmpty()) "[]" else stagedItems
                    ?: itemsJson(items).also { if (staged == items) stagedItems = it }
                Brain.ask(question, "", packed) { words ->
                    streamed = true
                    runOnUiThread { waiting.text = words; scrollDown() }
                }
            }
            if (streamed) Chat.show(waiting, reply) else Chat.reveal(waiting, reply) { scrollDown() }
            scrollDown()
            send.isEnabled = true
        }
    }

    // ── live video in a call: camera, or his screen ────────────────────────
    private fun showCamera() {
        cameraView.isVisible = true
        if (cameraView.isAvailable) {
            Vision.startCamera(this, cameraView)
            return
        }
        cameraView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(t: android.graphics.SurfaceTexture, w: Int, h: Int) {
                if (LiveVoice.active) Vision.startCamera(this@MainActivity, cameraView)
            }
            override fun onSurfaceTextureSizeChanged(t: android.graphics.SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(t: android.graphics.SurfaceTexture) = true
            override fun onSurfaceTextureUpdated(t: android.graphics.SurfaceTexture) {}
        }
    }

    /** The buttons only during a call, and never over the lock screen. */
    private fun showVision() {
        val source = Vision.source
        visionRow.isVisible = LiveVoice.active && !locked()
        cameraView.isVisible = source == Vision.Source.CAMERA
        shareCamera.setText(if (source == Vision.Source.CAMERA) R.string.camera_off else R.string.camera_on)
        shareScreen.setText(if (source == Vision.Source.SCREEN) R.string.screen_off else R.string.screen_on)
    }

    // ── attachments: the paperclip, and Share → Jarvis ─────────────────────
    private fun takeShare(intent: Intent?) {
        when (intent?.action) {
            Intent.ACTION_SEND -> {
                val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT).orEmpty()
                val (chats, others) = streams(intent).partition { maybeChat(it) }
                chats.forEach { importChat(it, subject) }
                others.forEach(::stageUri)
                intent.getStringExtra(Intent.EXTRA_TEXT)?.takeIf { it.isNotBlank() }?.let {
                    stage(Staged(intent.getStringExtra(Intent.EXTRA_SUBJECT)
                        ?: getString(R.string.shared_text), "text/plain", null, it))
                }
            }
            Intent.ACTION_SEND_MULTIPLE -> streams(intent).forEach(::stageUri)
            else -> return
        }
        // Handled once: a rotation must not attach it all over again.
        intent.action = Intent.ACTION_MAIN
        input.requestFocus()
    }

    // ── old WhatsApp chats: Export chat → Jarvis ───────────────────────────
    /** A text file or a zip: possibly a WhatsApp export, worth a look. */
    private fun maybeChat(uri: Uri): Boolean {
        val mime = contentResolver.getType(uri).orEmpty()
        return mime == "text/plain" || mime == "application/zip" ||
            displayName(uri).endsWith(".zip", ignoreCase = true)
    }

    /**
     * Files the chat in her memory if it is a WhatsApp export; anything else
     * is attached as usual. Plain parsing on the phone — no model involved,
     * so a long chat costs nothing to bring in.
     */
    private fun importChat(uri: Uri, subject: String) {
        val name = displayName(uri)
        val working = turn("…", mine = false)
        lifecycleScope.launch {
            val said = withContext(Dispatchers.IO) {
                val text = readChat(uri) ?: return@withContext null
                Brain.importWhatsApp(text, name, subject)
            }
            if (said != null) {
                Chat.show(working, said)
            } else {
                log.removeView(working)
                stageUri(uri)
            }
            scrollDown()
        }
    }

    /** The export's text, from the .txt itself or the one inside its zip. */
    private fun readChat(uri: Uri): String? = runCatching {
        contentResolver.openInputStream(uri)?.use { stream ->
            val mime = contentResolver.getType(uri).orEmpty()
            if (mime == "application/zip" || displayName(uri).endsWith(".zip", true)) {
                val zip = java.util.zip.ZipInputStream(stream)
                generateSequence { zip.nextEntry }
                    .firstOrNull { it.name.endsWith(".txt", ignoreCase = true) }
                    ?.let { zip.reader().readText() }
            } else {
                stream.reader().readText()
            }
        }
    }.getOrNull()

    private fun displayName(uri: Uri): String = runCatching {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { if (it.moveToFirst()) it.getString(0) else null }
    }.getOrNull() ?: uri.lastPathSegment.orEmpty()

    private fun streams(intent: Intent): List<Uri> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                ?: listOfNotNull(intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java))
        } else {
            @Suppress("DEPRECATION")
            runCatching { intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) }.getOrNull()
                ?: listOfNotNull(intent.getParcelableExtra(Intent.EXTRA_STREAM) as? Uri)
        }

    private fun stageUri(uri: Uri) {
        val mime = contentResolver.getType(uri).orEmpty()
        if (!mime.startsWith("image/") && mime != "application/pdf" && mime != "text/plain") {
            note(getString(R.string.only_photos_pdfs))
            return
        }
        val name = runCatching {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { if (it.moveToFirst()) it.getString(0) else null }
        }.getOrNull() ?: uri.lastPathSegment ?: "file"
        stage(Staged(name, mime, uri, null))
    }

    private fun stage(item: Staged) {
        staged += item
        stagedItems = null
        showStaged()
    }

    private fun showStaged() {
        attached.removeAllViews()
        staged.toList().forEach { item ->
            attached.addView(TextView(this).apply {
                text = "📎 ${item.name.take(24)}  ✕"
                setTextColor(getColor(R.color.jarvis))
                textSize = 13f
                setBackgroundResource(R.drawable.glass_pill)
                setPadding(dp(14), dp(7), dp(14), dp(7))
                contentDescription = getString(R.string.remove_attachment) + " " + item.name
                setOnClickListener {
                    staged.remove(item)
                    stagedItems = null
                    showStaged()
                }
            }, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { marginEnd = dp(8) })
        }
        attachedRow.isVisible = staged.isNotEmpty()
        input.setHint(if (staged.isEmpty()) R.string.hint else R.string.ask_about_this)
    }

    /** What was attached, read and packed for the brain. Blocking. */
    private fun itemsJson(items: List<Staged>): String {
        val out = org.json.JSONArray()
        for (item in items) {
            val row = org.json.JSONObject().put("name", item.name).put("mime", item.mime)
            when {
                item.text != null -> row.put("text", item.text)
                item.uri == null -> continue
                item.mime == "text/plain" -> row.put("text", readText(item.uri))
                item.mime.startsWith("image/") -> {
                    row.put("mime", "image/jpeg")
                    row.put("data", readPhoto(item.uri) ?: continue)
                }
                else -> readFile(item.uri)?.let { row.put("data", it) } ?: row.put("too_big", true)
            }
            out.put(row)
        }
        return out.toString()
    }

    private fun readText(uri: Uri): String =
        runCatching {
            contentResolver.openInputStream(uri)?.use { it.reader().readText().take(20_000) }
        }.getOrNull().orEmpty()

    /** A photo, shrunk to what the model needs to read it, as base64 JPEG. */
    private fun readPhoto(uri: Uri): String? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= 1600) sample *= 2
        val bitmap = contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inSampleSize = sample })
        } ?: return null
        val bytes = java.io.ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, 85, bytes)
        Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)
    }.getOrNull()

    /** A PDF as base64, or null when too big to send (about 14 MB). */
    private fun readFile(uri: Uri): String? = runCatching {
        val limit = 14 * 1024 * 1024
        val bytes = contentResolver.openInputStream(uri)?.use { stream ->
            val buffer = java.io.ByteArrayOutputStream()
            val chunk = ByteArray(64 * 1024)
            while (true) {
                val read = stream.read(chunk)
                if (read < 0) break
                buffer.write(chunk, 0, read)
                if (buffer.size() > limit) return null
            }
            buffer.toByteArray()
        } ?: return null
        Base64.encodeToString(bytes, Base64.NO_WRAP)
    }.getOrNull()


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
            Manifest.permission.READ_CONTACTS,
            Manifest.permission.CALL_PHONE,
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
