package com.hrishikesh.carnage

import android.app.KeyguardManager
import android.app.assist.AssistContent
import android.app.assist.AssistStructure
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.service.voice.VoiceInteractionSession
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Jarvis over whatever he was doing: what opens when he holds the power button
 * once Jarvis is the phone's assistant (see [AssistService]).
 *
 * A sheet along the bottom with the app still visible above it. She starts
 * listening at once, as Gemini does; typing instead stops the call and gets a
 * typed reply, as it does in the app. Either way she is handed his screen —
 * the app's text and a screenshot — so "what's this?", "reply to her" and
 * "remind me about this" mean the thing in front of him. A chip says what she
 * is looking at; tapping it takes the screen away from her.
 *
 * Nothing private shows over the lock screen: there only her state and the
 * core are visible, as on the app's talk screen.
 */
class AssistSession(context: Context) : VoiceInteractionSession(context) {

    private val main = Handler(Looper.getMainLooper())
    private val work = Executors.newSingleThreadExecutor()

    private lateinit var scroll: ScrollView
    private lateinit var log: LinearLayout
    private lateinit var input: EditText
    private lateinit var send: ImageButton
    private lateinit var mic: TextView
    private lateinit var state: TextView
    private lateinit var chip: TextView
    private lateinit var orb: View

    // ── what she was handed about the screen, this sitting ──────────────────
    @Volatile private var app = ""
    @Volatile private var screenText = ""
    @Volatile private var screenJpeg = ""
    /** He tapped the chip: answer without the screen from now on. */
    @Volatile private var useScreen = true
    /** The screen as context for typed turns, described once and kept. */
    @Volatile private var typedContext: String? = null

    private val shareSoon = Runnable { shareWithCall() }

    /** Why the sheet is hidden while the sitting goes on, if it is. */
    @Volatile private var aside = Aside.NONE

    private enum class Aside { NONE, HANDS, LAUNCH }

    companion object {
        private const val RESUME = "com.hrishikesh.carnage.RESUME"
        private val handler = Handler(Looper.getMainLooper())

        @Volatile private var current: AssistSession? = null

        /** True while the sheet is on screen. */
        @Volatile var showing = false
            private set

        /**
         * Out of the way while she works in another app (Hands), so what she
         * looks at and taps is his app, not her own sheet. The conversation
         * and any call carry on; [comeBack] brings the sheet back. Called on
         * Python's thread; returns once the sheet has left the screen.
         */
        fun stepAside(): Boolean {
            val session = current?.takeIf { showing } ?: return false
            val gone = CountDownLatch(1)
            handler.post {
                session.aside = Aside.HANDS
                session.hide()
                gone.countDown()
            }
            gone.await(1, TimeUnit.SECONDS)
            Thread.sleep(450)               // the window's exit animation
            return true
        }

        /** Back on screen, conversation intact, after [stepAside]. */
        fun comeBack(): Boolean {
            val session = current ?: return false
            if (session.aside != Aside.HANDS) return false
            handler.post {
                AssistService.instance?.showSession(Bundle().apply { putBoolean(RESUME, true) }, 0)
            }
            return true
        }

        /** She opened Maps or another app from the sheet: let him see it. */
        fun launched() {
            val session = current?.takeIf { showing } ?: return
            handler.post {
                session.aside = Aside.LAUNCH
                session.hide()
            }
        }
    }

    private val voice = object : LiveVoice.Listener {
        override fun onState(state: String) = show(state)
        override fun onLine(who: String, text: String) {
            turn(text, mine = who == "you")
        }
    }

    override fun onCreateContentView(): View {
        val root = layoutInflater.inflate(R.layout.assist_overlay, null)
        scroll = root.findViewById(R.id.scroll)
        log = root.findViewById(R.id.log)
        input = root.findViewById(R.id.input)
        send = root.findViewById(R.id.send)
        mic = root.findViewById(R.id.mic)
        state = root.findViewById(R.id.state)
        chip = root.findViewById(R.id.screenChip)
        orb = root.findViewById(R.id.orb)

        // The dim above the sheet is the app he was in: touching it means
        // "go away". The sheet itself swallows its own touches.
        root.findViewById<View>(R.id.scrim).setOnClickListener { hide() }

        send.setOnClickListener { ask(input.text.toString()) }
        input.setOnEditorActionListener { _, action, event ->
            val enter = event?.keyCode == KeyEvent.KEYCODE_ENTER &&
                event.action == KeyEvent.ACTION_DOWN
            if (action == EditorInfo.IME_ACTION_SEND || enter) {
                ask(input.text.toString()); true
            } else false
        }
        // Reaching for the keyboard means he would rather type: a live call
        // left running would answer out loud what he is typing about.
        input.setOnFocusChangeListener { _, focused ->
            if (focused && LiveVoice.active) LiveVoice.stop()
        }
        mic.setOnClickListener {
            if (LiveVoice.active) LiveVoice.stop() else talk()
        }
        chip.setOnClickListener { dropScreen() }
        root.findViewById<View>(R.id.openApp).setOnClickListener {
            startAssistantActivity(
                Intent(context, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            hide()
        }
        return root
    }

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        current = this
        showing = true
        if (!::scroll.isInitialized) return      // shown with no UI at all
        if (args?.getBoolean(RESUME) == true && aside == Aside.HANDS) {
            // Back from working in another app: the same sitting, carried on.
            aside = Aside.NONE
            sizeConversation()
            return
        }
        aside = Aside.NONE
        // A fresh sitting: the last screen and conversation are not this one's.
        app = ""; screenText = ""; screenJpeg = ""
        useScreen = true; typedContext = null
        log.removeAllViews()
        input.setText("")
        chip.visibility = View.GONE
        sizeConversation()

        val locked = context.getSystemService(KeyguardManager::class.java).isKeyguardLocked
        scroll.visibility = if (locked) View.GONE else View.VISIBLE
        input.isEnabled = !locked
        send.isEnabled = !locked

        LiveVoice.addListener(voice)
        CarnageService.start(context)
        show(if (LiveVoice.active) "listening" else "off")
        talk()
    }

    override fun onHide() {
        showing = false
        if (aside != Aside.NONE) {
            // Stepping aside for Hands or for an app she opened: the
            // conversation, and a call if there is one, goes on.
            super.onHide()
            return
        }
        // Closing the sheet is hanging up: a call with no face, still holding
        // the mic, is not what dismissing her should leave behind.
        main.removeCallbacks(shareSoon)
        LiveVoice.removeListener(voice)
        if (LiveVoice.active) LiveVoice.stop()
        super.onHide()
    }

    override fun onDestroy() {
        if (current === this) {
            current = null
            showing = false
        }
        work.shutdown()
        super.onDestroy()
    }

    // ── the screen, as Android hands it over ────────────────────────────────
    // The one-argument AssistState version (Android 10+) forwards the first
    // activity's data here by default, so this covers every version.
    @Suppress("OVERRIDE_DEPRECATION")
    override fun onHandleAssist(data: Bundle?, structure: AssistStructure?, content: AssistContent?) {
        structure ?: return
        // Walking the tree can block on the app it came from: never on main.
        work.execute {
            app = Screen.appName(context, structure)
            screenText = Screen.text(structure)
            main.post { screenArrived() }
        }
    }

    override fun onHandleScreenshot(screenshot: Bitmap?) {
        screenshot ?: return
        work.execute {
            screenJpeg = Screen.jpeg(screenshot)
            main.post { screenArrived() }
        }
    }

    /** Text and screenshot arrive separately: settle, then pass both on once. */
    private fun screenArrived() {
        if (!useScreen || (screenText.isEmpty() && screenJpeg.isEmpty())) return
        typedContext = null
        chip.text = if (app.isNotEmpty()) {
            context.getString(R.string.looking_at, app)
        } else context.getString(R.string.looking_at_screen)
        chip.append("  ✕")
        chip.visibility = View.VISIBLE
        main.removeCallbacks(shareSoon)
        main.postDelayed(shareSoon, 350)
    }

    private fun dropScreen() {
        useScreen = false
        typedContext = null
        main.removeCallbacks(shareSoon)
        chip.visibility = View.GONE
    }

    // ── talking ─────────────────────────────────────────────────────────────
    private fun talk() {
        if (LiveVoice.active) return
        LiveVoice.start(context)
        shareWithCall()
    }

    /** Give the live call the screen: its text as context, one frame to see. */
    private fun shareWithCall() {
        if (!useScreen || !LiveVoice.active) return
        val (name, text, jpeg) = Triple(app, screenText, screenJpeg)
        if (text.isEmpty() && jpeg.isEmpty()) return
        work.execute {
            // No description wanted here: the call gets the screenshot itself.
            val framed = if (text.isNotEmpty()) Brain.readScreen(name, text, "") else ""
            if (useScreen) LiveVoice.share(framed, jpeg)
        }
    }

    // ── typing ──────────────────────────────────────────────────────────────
    private fun ask(said: String) {
        val text = said.trim()
        if (text.isEmpty()) return
        if (LiveVoice.active) LiveVoice.stop()
        input.setText("")
        send.isEnabled = false
        turn(text, mine = true)
        val waiting = turn("…", mine = false)
        work.execute {
            val screen = if (useScreen) {
                typedContext ?: Brain.readScreen(app, screenText, "")
                    .also { typedContext = it }
            } else ""
            // The screenshot goes with the question: she looks at it while
            // answering, rather than having it described to her first.
            val shot = if (useScreen && screenJpeg.isNotEmpty()) {
                org.json.JSONArray().put(
                    org.json.JSONObject().put("name", "screenshot")
                        .put("mime", "image/jpeg").put("data", screenJpeg)
                ).toString()
            } else "[]"
            var streamed = false
            val reply = Brain.ask(text, screen, shot) { words ->
                streamed = true
                main.post { waiting.text = words; sizeConversation() }
            }
            main.post {
                if (streamed) Chat.show(waiting, reply) else Chat.reveal(waiting, reply) { sizeConversation() }
                sizeConversation()
                send.isEnabled = true
            }
        }
    }

    // ── what he sees ────────────────────────────────────────────────────────
    private fun show(now: String) {
        mic.text = context.getString(if (LiveVoice.active) R.string.hang_up else R.string.talk)
        val label = when (now) {
            "connecting" -> context.getString(R.string.connecting)
            "listening" -> context.getString(R.string.listening)
            "speaking" -> context.getString(R.string.speaking)
            "off" -> ""
            else -> now                      // a sentence from LiveVoice
        }
        state.text = label
        val live = now == "listening" || now == "speaking"
        orb.animate().scaleX(if (live) 1.25f else 1f).scaleY(if (live) 1.25f else 1f)
            .alpha(if (now == "off") 0.6f else 1f).setDuration(200).start()
    }

    private fun turn(text: String, mine: Boolean): TextView {
        val view = TextView(context).apply {
            this.text = text
            setPadding(dp(14), dp(9), dp(14), dp(9))
            setTextColor(context.getColor(R.color.ink))
            setBackgroundResource(if (mine) R.drawable.bubble_him else R.drawable.bubble_her)
            textSize = 15f
            setLineSpacing(0f, 1.15f)
            setTextIsSelectable(!mine)
            // Selectable makes it focusable, and Android then paints a grey
            // focus box around the bubble.
            defaultFocusHighlightEnabled = false
        }
        log.addView(view, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply {
            topMargin = dp(8)
            marginStart = if (mine) dp(40) else 0
            marginEnd = if (mine) 0 else dp(40)
            gravity = if (mine) Gravity.END else Gravity.START
        })
        sizeConversation()
        return view
    }

    /**
     * The conversation grows with what is said, up to about half the screen;
     * past that it scrolls, so the app he asked about stays in view above.
     */
    private fun sizeConversation() {
        scroll.post {
            val cap = context.resources.displayMetrics.heightPixels * 45 / 100
            val wanted = if (log.height > cap) cap else LinearLayout.LayoutParams.WRAP_CONTENT
            if (scroll.layoutParams.height != wanted) {
                scroll.layoutParams = scroll.layoutParams.apply { height = wanted }
            }
            scroll.fullScroll(View.FOCUS_DOWN)
        }
    }

    private fun dp(value: Int): Int = (value * context.resources.displayMetrics.density).toInt()
}
