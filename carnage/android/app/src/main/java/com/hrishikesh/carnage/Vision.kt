package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.Base64
import android.util.Log
import android.util.Size
import android.view.Surface
import android.view.TextureView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream

/**
 * Live video into a voice call: the back camera, or his screen.
 *
 * Gemini Live watches a video as a stream of still frames; about one a second
 * is what it is built for, and more only costs data. So this keeps the
 * camera or screen capture running and hands the call the newest frame once
 * a second, small (768 px on the long side) and as JPEG.
 *
 * One source at a time, and only during a call: [LiveVoice] stops it when the
 * call ends, so nothing is ever watched with nobody to watch it.
 */
object Vision {

    enum class Source { NONE, CAMERA, SCREEN }

    private const val TAG = "carnage.vision"
    private const val FRAME_MS = 1000L
    private const val MAX_EDGE = 768
    private const val QUALITY = 70

    @Volatile var source = Source.NONE
        private set

    /** Told whenever the source changes, on the main thread. */
    var onChange: ((Source) -> Unit)? = null

    /** The service, told when screen sharing ends so it can drop the type. */
    var onScreenEnded: (() -> Unit)? = null

    private val main = Handler(Looper.getMainLooper())
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var lastFrame = 0L

    // camera
    private var camera: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null

    // screen
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null

    // ── the camera ──────────────────────────────────────────────────────────
    /**
     * The back camera, streamed; [preview] shows him what she sees, so he
     * can aim. False without the permission or a camera.
     */
    @SuppressLint("MissingPermission")            // checked below
    @Synchronized
    fun startCamera(context: Context, preview: TextureView?): Boolean {
        stop()
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) return false
        val manager = context.getSystemService(CameraManager::class.java)
        val id = manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        } ?: return false
        val traits = manager.getCameraCharacteristics(id)
        val rotation = traits.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val size = traits.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?.getOutputSizes(ImageFormat.YUV_420_888)
            ?.filter { it.width <= 1280 && it.width * 3 == it.height * 4 }
            ?.maxByOrNull { it.width }
            ?: Size(640, 480)

        val work = start()
        val frames = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
        frames.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            image.use { if (due()) send(cameraJpeg(it, rotation)) }
        }, work)
        reader = frames

        val shown = preview?.surfaceTexture?.let { texture ->
            texture.setDefaultBufferSize(size.width, size.height)
            fitPreview(preview, size)
            Surface(texture)
        }
        val targets = listOfNotNull(frames.surface, shown)

        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(device: CameraDevice) {
                camera = device
                @Suppress("DEPRECATION")
                device.createCaptureSession(targets, object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) {
                        session = s
                        val request = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                            targets.forEach { addTarget(it) }
                            set(CaptureRequest.CONTROL_AF_MODE,
                                CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                        }.build()
                        runCatching { s.setRepeatingRequest(request, null, work) }
                            .onFailure { Log.w(TAG, "camera stream failed", it); stop() }
                    }

                    override fun onConfigureFailed(s: CameraCaptureSession) = stop()
                }, work)
            }

            override fun onDisconnected(device: CameraDevice) = stop()
            override fun onError(device: CameraDevice, error: Int) {
                Log.w(TAG, "camera error $error")
                stop()
            }
        }, work)
        changed(Source.CAMERA)
        LiveVoice.note("[He turned on his camera: you are seeing it live now. " +
            "Answer from what you see; you don't need look_around.]")
        return true
    }

    /** The preview fills its square without stretching: cropped, not squashed. */
    private fun fitPreview(view: TextureView, size: Size) {
        // Portrait: the sensor's landscape frame arrives turned, h × w.
        val aspect = size.width.toFloat() / size.height
        main.post {
            view.setTransform(Matrix().apply {
                setScale(1f, aspect, view.width / 2f, view.height / 2f)
            })
        }
    }

    private fun cameraJpeg(image: Image, rotation: Int): ByteArray? = runCatching {
        val nv21 = nv21(image)
        val raw = ByteArrayOutputStream()
        YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)
            .compressToJpeg(Rect(0, 0, image.width, image.height), 85, raw)
        val bitmap = BitmapFactory.decodeByteArray(raw.toByteArray(), 0, raw.size())
        val turned = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height,
            Matrix().apply { postRotate(rotation.toFloat()) }, true)
        jpeg(turned)
    }.onFailure { Log.w(TAG, "camera frame failed", it) }.getOrNull()

    /** YUV_420_888 → NV21, honouring row and pixel strides. */
    private fun nv21(image: Image): ByteArray {
        val w = image.width
        val h = image.height
        val out = ByteArray(w * h * 3 / 2)
        val y = image.planes[0]
        var pos = 0
        for (row in 0 until h) {
            y.buffer.position(row * y.rowStride)
            y.buffer.get(out, pos, w)
            pos += w
        }
        val u = image.planes[1]
        val v = image.planes[2]
        for (row in 0 until h / 2) {
            for (col in 0 until w / 2) {
                val vu = row * v.rowStride + col * v.pixelStride
                val uu = row * u.rowStride + col * u.pixelStride
                out[pos++] = v.buffer.get(vu)
                out[pos++] = u.buffer.get(uu)
            }
        }
        return out
    }

    // ── the screen ──────────────────────────────────────────────────────────
    /**
     * His screen, streamed, from a capture he has just agreed to (the
     * service holds it, as Android requires). He can go to any app and keep
     * talking about what is on it.
     */
    @Synchronized
    fun startScreen(context: Context, capture: MediaProjection) {
        stop()
        val metrics = context.resources.displayMetrics
        val scale = MAX_EDGE.toFloat() / maxOf(metrics.widthPixels, metrics.heightPixels)
        val w = (metrics.widthPixels * scale).toInt() / 2 * 2
        val h = (metrics.heightPixels * scale).toInt() / 2 * 2
        val work = start()
        val frames = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        frames.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            image.use { if (due()) send(screenJpeg(it)) }
        }, work)
        reader = frames
        projection = capture
        // Android 14 insists on a callback before a display is made.
        capture.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                main.post { if (projection === capture) stop() }
            }
        }, work)
        display = capture.createVirtualDisplay(
            "jarvis-sees", w, h, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, frames.surface, null, work)
        changed(Source.SCREEN)
        LiveVoice.note("[He is sharing his screen: you are seeing it live now, " +
            "about once a second, as he moves between apps.]")
    }

    private fun screenJpeg(image: Image): ByteArray? = runCatching {
        val plane = image.planes[0]
        val padding = (plane.rowStride - plane.pixelStride * image.width) / plane.pixelStride
        val wide = Bitmap.createBitmap(image.width + padding, image.height, Bitmap.Config.ARGB_8888)
        wide.copyPixelsFromBuffer(plane.buffer)
        jpeg(Bitmap.createBitmap(wide, 0, 0, image.width, image.height))
    }.onFailure { Log.w(TAG, "screen frame failed", it) }.getOrNull()

    // ── shared ──────────────────────────────────────────────────────────────
    /** Camera or screen off; the call carries on. Safe to call any time. */
    @Synchronized
    fun stop() {
        val was = source
        runCatching { session?.close() }
        runCatching { camera?.close() }
        runCatching { display?.release() }
        runCatching { projection?.stop() }
        runCatching { reader?.close() }
        session = null; camera = null; display = null; projection = null; reader = null
        thread?.quitSafely()
        thread = null; handler = null
        if (was == Source.SCREEN) main.post { onScreenEnded?.invoke() }
        if (was != Source.NONE) {
            changed(Source.NONE)
            LiveVoice.note("[He stopped sharing video.]")
        }
    }

    private fun start(): Handler {
        val t = HandlerThread("carnage-vision").apply { start() }
        thread = t
        lastFrame = 0L
        return Handler(t.looper).also { handler = it }
    }

    private fun due(): Boolean {
        val now = android.os.SystemClock.elapsedRealtime()
        if (now - lastFrame < FRAME_MS) return false
        lastFrame = now
        return true
    }

    private fun jpeg(bitmap: Bitmap): ByteArray {
        val scale = MAX_EDGE.toFloat() / maxOf(bitmap.width, bitmap.height)
        val small = if (scale < 1f) {
            Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt(),
                (bitmap.height * scale).toInt(), true)
        } else bitmap
        return ByteArrayOutputStream().also {
            small.compress(Bitmap.CompressFormat.JPEG, QUALITY, it)
        }.toByteArray()
    }

    private fun send(jpeg: ByteArray?) {
        jpeg ?: return
        LiveVoice.frame(Base64.encodeToString(jpeg, Base64.NO_WRAP))
    }

    private fun changed(now: Source) {
        source = now
        main.post { onChange?.invoke(now) }
    }
}
