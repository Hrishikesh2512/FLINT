package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * A live voice conversation — the phone doing what Venom does on the Pi.
 *
 * Audio in from the mic (or the Bluetooth earphone's mic), audio out to the
 * speaker (or the earphone), both streamed over one Gemini Live websocket. She
 * can be interrupted mid-sentence, uses her tools while talking, and the call
 * ends when she is told goodbye or nobody speaks for a while.
 *
 * Kotlin carries the audio because that is where audio belongs on Android. It
 * does not know who she is or what a tool does: the opening `setup` frame and
 * every tool answer come from the same Python the Pi runs (`carnage/live.py`),
 * through [Brain]. That keeps her one assistant rather than two that drift.
 */
object LiveVoice {

    interface Listener {
        /** "connecting", "listening", "speaking", "off", or an error sentence. */
        fun onState(state: String) {}

        /** A finished line of the conversation; [who] is "you" or "jarvis". */
        fun onLine(who: String, text: String) {}
    }

    private const val TAG = "carnage.live"
    private const val IN_RATE = 16_000            // what the Live API listens at
    private const val OUT_RATE = 24_000           // what it speaks at
    private const val FRAME_BYTES = IN_RATE / 25 * 2   // 40 ms of 16-bit mono
    private const val IDLE_MS = 45_000L           // Venom's idle window

    private val main = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<Listener>()
    private val tools = Executors.newSingleThreadExecutor()
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    @Volatile private var call: Call? = null
    @Volatile private var changedAt = 0L

    /** Milliseconds since a call last started or ended. */
    fun msSinceChange(): Long = android.os.SystemClock.elapsedRealtime() - changedAt

    val active: Boolean get() = call != null

    fun addListener(listener: Listener) = synchronized(listeners) { listeners.add(listener) }
    fun removeListener(listener: Listener) = synchronized(listeners) { listeners.remove(listener) }

    /**
     * What is on his screen, for a call started over another app: [text] is
     * the screen as context, [jpegB64] one frame of it so she can see it too.
     * Held until the call is up, so it can arrive before or after it starts.
     * Cleared with [stop]: a screen he has left is not his context any more.
     */
    @Volatile private var screen: Pair<String, String>? = null

    fun share(text: String, jpegB64: String) {
        if (text.isEmpty() && jpegB64.isEmpty()) return
        screen = text to jpegB64
        call?.sendScreen()
    }

    /** One frame of live video (Vision.kt): what the camera or screen shows. */
    fun frame(jpegB64: String) {
        call?.sendFrame(jpegB64)
    }

    /**
     * A line of context she reads but does not answer: "he turned on his
     * camera". Without it she would see frames start with no idea why.
     */
    fun note(text: String) {
        call?.sendNote(text)
    }

    /** Start talking if quiet, hang up if talking. What every button does. */
    fun toggle(context: Context) {
        if (active) stop() else start(context)
    }

    @Synchronized
    fun start(context: Context) {
        if (call != null) return
        val app = context.applicationContext
        if (ContextCompat.checkSelfPermission(app, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            state("Allow the microphone to talk.")
            return
        }
        changedAt = android.os.SystemClock.elapsedRealtime()
        call = Call(app).also { it.open() }
    }

    @Synchronized
    fun stop() {
        if (call == null) return
        // Who hung up, so a call that ends by itself can be traced.
        Log.i(TAG, "hanging up", Throwable("stop() called from"))
        changedAt = android.os.SystemClock.elapsedRealtime()
        screen = null
        // Nothing is watched with nobody on the call to watch it.
        Vision.stop()
        call?.close()
        call = null
    }

    private fun state(text: String) = main.post {
        synchronized(listeners) { listeners.toList() }.forEach { it.onState(text) }
    }

    private fun line(who: String, text: String) = main.post {
        synchronized(listeners) { listeners.toList() }.forEach { it.onLine(who, text) }
    }

    /** One conversation, from the first chime to hanging up. */
    private class Call(val context: Context) : WebSocketListener() {

        private val audio = context.getSystemService(AudioManager::class.java)
        private var socket: WebSocket? = null
        private var recorder: AudioRecord? = null
        private var track: AudioTrack? = null
        private val outgoing = LinkedBlockingQueue<ByteArray>()
        @Volatile private var open = true
        @Volatile private var streaming = false
        @Volatile private var lastActivity = System.currentTimeMillis()
        @Volatile private var hangUpAfterReply = false
        private val heard = StringBuilder()
        private val said = StringBuilder()
        private var previousMode = AudioManager.MODE_NORMAL

        fun open() {
            state("connecting")
            Thread({
                // Opened from the lock screen or a tile, the process may be
                // brand new: make sure the brain is up before asking it anything.
                Brain.start(context)
                val url = Brain.liveUrl()
                val setup = Brain.liveSetup()
                if (url.isEmpty() || setup == null) {
                    state("Add a Gemini key in Settings.")
                    LiveVoice.stop()
                    return@Thread
                }
                routeAudio()
                socket = client.newWebSocket(Request.Builder().url(url).build(), this)
                socket?.send(setup)
            }, "carnage-live-open").start()
        }

        fun close() {
            if (!open) return
            open = false
            streaming = false
            runCatching { socket?.close(1000, "bye") }
            runCatching { recorder?.stop(); recorder?.release() }
            outgoing.clear()
            runCatching { track?.stop(); track?.release() }
            restoreAudio()
            flushTranscript()
            tools.execute { Brain.liveFinish() }
            state("off")
        }

        // ── the socket ──────────────────────────────────────────────────────
        override fun onMessage(webSocket: WebSocket, text: String) = handle(text)

        // The Live API sends its JSON in binary frames.
        override fun onMessage(webSocket: WebSocket, bytes: ByteString) = handle(bytes.utf8())

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(TAG, "live socket failed (${response?.code})", t)
            if (open) {
                state("Connection lost. Tap to retry.")
                main.post { LiveVoice.stop() }
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "live socket closed: $code $reason")
            if (open) main.post { LiveVoice.stop() }
        }

        private fun handle(raw: String) {
            val message = runCatching { JSONObject(raw) }.getOrNull() ?: return
            when {
                message.has("setupComplete") -> begin()
                message.has("serverContent") -> content(message.getJSONObject("serverContent"))
                message.has("toolCall") -> {
                    lastActivity = System.currentTimeMillis()
                    tools.execute { answerTools(raw) }
                }
                message.has("goAway") -> Log.i(TAG, "server will close soon")
            }
        }

        private fun begin() {
            chime()
            startSpeaker()
            startMic()
            began = true
            sendScreen()
            state("listening")
            watchIdle()
        }

        @Volatile private var began = false
        private var screenSent: Pair<String, String>? = null

        fun sendFrame(jpeg: String) {
            if (!began || !open) return
            lastActivity = System.currentTimeMillis()   // watching is not idle
            socket?.send(
                """{"realtimeInput":{"video":{"data":"$jpeg","mimeType":"image/jpeg"}}}"""
            )
        }

        fun sendNote(text: String) {
            if (!began || !open) return
            val turn = JSONObject().put("role", "user")
                .put("parts", org.json.JSONArray().put(JSONObject().put("text", text)))
            socket?.send(
                JSONObject().put(
                    "clientContent",
                    JSONObject().put("turns", org.json.JSONArray().put(turn))
                        .put("turnComplete", false),
                ).toString()
            )
        }

        /**
         * His screen into the call: the text as context that does not ask for
         * a reply (turnComplete false — she waits for him to speak), the
         * screenshot as one video frame so she can see what text cannot say.
         */
        @Synchronized
        fun sendScreen() {
            val shared = screen ?: return
            if (!began || !open || shared === screenSent) return
            screenSent = shared
            val (text, jpeg) = shared
            if (text.isNotEmpty()) {
                val turn = JSONObject()
                    .put("role", "user")
                    .put("parts", org.json.JSONArray().put(JSONObject().put("text", text)))
                socket?.send(
                    JSONObject().put(
                        "clientContent",
                        JSONObject()
                            .put("turns", org.json.JSONArray().put(turn))
                            .put("turnComplete", false),
                    ).toString()
                )
            }
            if (jpeg.isNotEmpty()) {
                socket?.send(
                    """{"realtimeInput":{"video":{"data":"$jpeg","mimeType":"image/jpeg"}}}"""
                )
            }
        }

        private fun content(content: JSONObject) {
            lastActivity = System.currentTimeMillis()
            content.optJSONObject("modelTurn")?.optJSONArray("parts")?.let { parts ->
                for (i in 0 until parts.length()) {
                    val data = parts.optJSONObject(i)?.optJSONObject("inlineData")
                        ?.optString("data").orEmpty()
                    if (data.isNotEmpty()) {
                        // Her voice only goes into headphones; without them the
                        // reply still arrives as text (the transcript below).
                        if (Headphones.connected(context)) {
                            outgoing.offer(Base64.decode(data, Base64.DEFAULT))
                        }
                        state("speaking")
                    }
                }
            }
            content.optJSONObject("inputTranscription")?.optString("text")
                ?.let { synchronized(heard) { heard.append(it) } }
            content.optJSONObject("outputTranscription")?.optString("text")
                ?.let { synchronized(said) { said.append(it) } }
            if (content.optBoolean("interrupted")) {
                // He talked over her: drop what she had queued to say.
                outgoing.clear()
                runCatching { track?.pause(); track?.flush(); track?.play() }
            }
            if (content.optBoolean("turnComplete")) {
                flushTranscript()
                if (hangUpAfterReply) {
                    main.postDelayed({ if (call === this) LiveVoice.stop() }, drainMillis())
                } else {
                    state("listening")
                }
            }
        }

        private fun answerTools(raw: String) {
            // Watching live already: a one-photo look would be slower and
            // older than what she sees. Answer those calls here, pointing
            // her back to the live view.
            if (Vision.source != Vision.Source.NONE) {
                val calls = JSONObject(raw).optJSONObject("toolCall")
                    ?.optJSONArray("functionCalls") ?: org.json.JSONArray()
                val names = List(calls.length()) { calls.getJSONObject(it).optString("name") }
                if (names.isNotEmpty() && names.all { it == "look_around" || it == "find_thing" }) {
                    val replies = org.json.JSONArray()
                    for (i in 0 until calls.length()) {
                        val call = calls.getJSONObject(i)
                        replies.put(JSONObject()
                            .put("id", call.optString("id"))
                            .put("name", call.optString("name"))
                            .put("response", JSONObject().put("result",
                                "You are already seeing his ${if (Vision.source == Vision.Source.SCREEN) "screen" else "camera"} " +
                                    "live. Answer from what you see right now.")))
                    }
                    if (open) socket?.send(JSONObject().put("toolResponse",
                        JSONObject().put("functionResponses", replies)).toString())
                    return
                }
            }
            val answer = Brain.liveTool(raw)?.let { runCatching { JSONObject(it) }.getOrNull() }
                ?: return
            if (answer.optBoolean("end")) hangUpAfterReply = true
            if (open) socket?.send(answer.getJSONObject("response").toString())
        }

        private fun flushTranscript() {
            val you = synchronized(heard) { heard.toString().trim().also { heard.clear() } }
            val her = synchronized(said) { said.toString().trim().also { said.clear() } }
            if (you.isNotEmpty()) { Brain.liveRecord("you", you); line("you", you) }
            if (her.isNotEmpty()) { Brain.liveRecord("jarvis", her); line("jarvis", her) }
        }

        // ── the microphone ──────────────────────────────────────────────────
        @SuppressLint("MissingPermission")    // checked in start()
        private fun startMic() {
            val minimum = AudioRecord.getMinBufferSize(
                IN_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            // VOICE_COMMUNICATION turns on the phone's own echo cancelling, so
            // she does not hear herself through the speaker and interrupt.
            val mic = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION, IN_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minimum, FRAME_BYTES * 4))
            if (AcousticEchoCanceler.isAvailable()) {
                AcousticEchoCanceler.create(mic.audioSessionId)?.enabled = true
            }
            if (NoiseSuppressor.isAvailable()) {
                NoiseSuppressor.create(mic.audioSessionId)?.enabled = true
            }
            recorder = mic
            streaming = true
            mic.startRecording()
            Thread({
                val frame = ByteArray(FRAME_BYTES)
                while (streaming) {
                    val read = mic.read(frame, 0, frame.size)
                    if (read <= 0) continue
                    val data = Base64.encodeToString(frame, 0, read, Base64.NO_WRAP)
                    socket?.send(
                        """{"realtimeInput":{"audio":{"data":"$data",""" +
                            """"mimeType":"audio/pcm;rate=$IN_RATE"}}}"""
                    )
                }
            }, "carnage-live-mic").start()
        }

        // ── the speaker ─────────────────────────────────────────────────────
        private fun startSpeaker() {
            val minimum = AudioTrack.getMinBufferSize(
                OUT_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val player = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(OUT_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build()
                )
                .setBufferSizeInBytes(minimum * 4)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            track = player
            player.play()
            Thread({
                while (open) {
                    val chunk = runCatching { outgoing.poll(200, TimeUnit.MILLISECONDS) }
                        .getOrNull() ?: continue
                    runCatching { player.write(chunk, 0, chunk.size) }
                }
            }, "carnage-live-speaker").start()
        }

        /** Roughly how long until what she has queued has been heard. */
        private fun drainMillis(): Long =
            outgoing.sumOf { it.size }.toLong() * 1000 / (OUT_RATE * 2) + 800

        private fun chime() {
            if (!Headphones.connected(context)) return
            runCatching {
                val tone = ToneGenerator(AudioManager.STREAM_VOICE_CALL, 70)
                tone.startTone(ToneGenerator.TONE_PROP_BEEP2, 180)
                main.postDelayed({ tone.release() }, 500)
            }
        }

        private fun watchIdle() {
            main.postDelayed(object : Runnable {
                override fun run() {
                    if (!open) return
                    val quiet = System.currentTimeMillis() - lastActivity
                    if (quiet > IDLE_MS && outgoing.isEmpty()) {
                        Log.i(TAG, "idle ${quiet / 1000}s — hanging up")
                        LiveVoice.stop()
                    } else {
                        main.postDelayed(this, 1000)
                    }
                }
            }, 1000)
        }

        // ── where the sound goes ────────────────────────────────────────────
        /**
         * Communication mode, routed to the Bluetooth earphone when one is
         * connected (its mic only exists in the call profile), else a wired
         * or USB headset. Never the loudspeaker: without headphones she is
         * silent and answers in text.
         */
        private fun routeAudio() {
            previousMode = audio.mode
            audio.mode = AudioManager.MODE_IN_COMMUNICATION
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val devices = audio.availableCommunicationDevices
                val pick = listOf(
                    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
                    AudioDeviceInfo.TYPE_BLE_HEADSET,
                    AudioDeviceInfo.TYPE_WIRED_HEADSET,
                    AudioDeviceInfo.TYPE_USB_HEADSET,
                ).firstNotNullOfOrNull { type -> devices.firstOrNull { it.type == type } }
                if (pick != null) audio.setCommunicationDevice(pick)
            } else {
                @Suppress("DEPRECATION")
                if (audio.isBluetoothScoAvailableOffCall && bluetoothHeadsetConnected()) {
                    audio.startBluetoothSco()
                    audio.isBluetoothScoOn = true
                }
            }
        }

        private fun restoreAudio() {
            runCatching {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    audio.clearCommunicationDevice()
                } else {
                    @Suppress("DEPRECATION")
                    run {
                        audio.stopBluetoothSco()
                        audio.isBluetoothScoOn = false
                        audio.isSpeakerphoneOn = false
                    }
                }
                audio.mode = previousMode
            }
        }

        private fun bluetoothHeadsetConnected(): Boolean =
            audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
            }
    }
}
