package com.hrishikesh.carnage

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer

/**
 * "Hey Jarvis" — the same three openWakeWord models the Pi runs, byte for byte
 * (release v0.5.1, fetched at build time into assets/wakeword/).
 *
 * A straight port of openWakeWord's streaming pipeline (utils.py, model.py),
 * one 80 ms chunk at a time:
 *
 *   1. melspectrogram.onnx over the chunk plus 480 samples of context
 *      (1760 samples -> 8 frames x 32 bins), scaled by x/10 + 2
 *   2. embedding_model.onnx over the latest 76 mel frames -> one 96-d vector
 *   3. hey_jarvis_v0.1.onnx over the latest 16 vectors -> a score in 0..1
 *
 * Pure arithmetic on arrays — no Android audio here — so it can be fed from
 * whichever microphone the caller holds.
 */
class WakeWord(context: Context) : AutoCloseable {

    private val env = OrtEnvironment.getEnvironment()
    private val mel = session(context, "melspectrogram.onnx")
    private val embed = session(context, "embedding_model.onnx")
    private val classify = session(context, "hey_jarvis_v0.1.onnx")

    // openWakeWord starts its mel buffer as 76 frames of ones.
    private val melFrames = ArrayDeque<FloatArray>().apply {
        repeat(MEL_WINDOW) { add(FloatArray(MEL_BINS) { 1f }) }
    }
    private val features = ArrayDeque<FloatArray>()
    private val raw = FloatArray(CHUNK + CONTEXT)
    private var scored = 0

    /** One 1280-sample chunk of 16 kHz mono PCM in; this chunk's score out. */
    fun process(chunk: ShortArray): Float {
        require(chunk.size == CHUNK) { "wake word needs $CHUNK-sample chunks" }
        // Slide: keep the last 480 samples as context, append the new chunk.
        System.arraycopy(raw, CHUNK, raw, 0, CONTEXT)
        for (i in 0 until CHUNK) raw[CONTEXT + i] = chunk[i].toFloat()

        // 1. mel frames
        val spectrum = run(mel, FloatBuffer.wrap(raw), longArrayOf(1, raw.size.toLong()))
        for (frame in 0 until spectrum.size / MEL_BINS) {
            melFrames.addLast(FloatArray(MEL_BINS) { bin ->
                spectrum[frame * MEL_BINS + bin] / 10f + 2f
            })
        }
        while (melFrames.size > MEL_WINDOW) melFrames.removeFirst()

        // 2. one embedding over the latest 76 frames
        val window = FloatBuffer.allocate(MEL_WINDOW * MEL_BINS)
        melFrames.forEach { window.put(it) }
        window.rewind()
        features.addLast(run(embed, window,
            longArrayOf(1, MEL_WINDOW.toLong(), MEL_BINS.toLong(), 1)))
        while (features.size > FEATURES) features.removeFirst()
        if (features.size < FEATURES) return 0f

        // 3. the classifier over the latest 16 embeddings
        val stacked = FloatBuffer.allocate(FEATURES * EMBEDDING)
        features.forEach { stacked.put(it) }
        stacked.rewind()
        val score = run(classify, stacked,
            longArrayOf(1, FEATURES.toLong(), EMBEDDING.toLong())).firstOrNull() ?: 0f
        // openWakeWord ignores its first five predictions while buffers fill.
        return if (++scored <= 5) 0f else score
    }

    /** Forget the audio so far — after a conversation, before listening again. */
    fun reset() {
        melFrames.clear()
        repeat(MEL_WINDOW) { melFrames.add(FloatArray(MEL_BINS) { 1f }) }
        features.clear()
        raw.fill(0f)
        scored = 0
    }

    override fun close() {
        runCatching { mel.close() }
        runCatching { embed.close() }
        runCatching { classify.close() }
    }

    private fun run(session: OrtSession, input: FloatBuffer, shape: LongArray): FloatArray {
        OnnxTensor.createTensor(env, input, shape).use { tensor ->
            session.run(mapOf(session.inputNames.first() to tensor)).use { result ->
                val out = result[0] as OnnxTensor
                val buffer = out.floatBuffer
                return FloatArray(buffer.remaining()).also { buffer.get(it) }
            }
        }
    }

    private fun session(context: Context, name: String): OrtSession {
        val bytes = context.assets.open("wakeword/$name").use { it.readBytes() }
        val options = OrtSession.SessionOptions().apply { setIntraOpNumThreads(1) }
        return env.createSession(bytes, options)
    }

    companion object {
        const val RATE = 16_000
        const val CHUNK = 1280          // 80 ms
        private const val CONTEXT = 480 // 3 x 160-sample hops of look-back
        private const val MEL_BINS = 32
        private const val MEL_WINDOW = 76
        private const val EMBEDDING = 96
        private const val FEATURES = 16
    }
}
