package com.hrishikesh.carnage

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import android.util.Log
import android.util.Size
import android.view.Surface
import androidx.core.content.ContextCompat
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * One photo from the back camera, with no screen — Venom's `capture_jpeg`.
 *
 * A short invisible preview runs first so auto-exposure and focus settle; a
 * still taken the instant the camera opens comes out dark and soft. Blocking,
 * and bounded: a camera that never answers costs a few seconds, not the call.
 */
object Camera {

    private const val TAG = "carnage.camera"
    private const val MAX_WIDTH = 1600            // plenty for Gemini, small to send
    private const val SETTLE_MS = 900L
    private const val TIMEOUT_S = 8L

    @Synchronized
    @SuppressLint("MissingPermission")            // checked below
    fun photo(context: Context): String? {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Log.i(TAG, "no camera permission")
            return null
        }
        val manager = context.getSystemService(CameraManager::class.java)
        val id = manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        } ?: manager.cameraIdList.firstOrNull() ?: return null
        val traits = manager.getCameraCharacteristics(id)
        val size = traits.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?.getOutputSizes(ImageFormat.JPEG)
            ?.filter { it.width <= MAX_WIDTH }
            ?.maxByOrNull { it.width * it.height }
            ?: Size(1280, 720)

        val thread = HandlerThread("carnage-camera").apply { start() }
        val handler = Handler(thread.looper)
        val reader = ImageReader.newInstance(size.width, size.height, ImageFormat.JPEG, 2)
        val texture = SurfaceTexture(0).apply { setDefaultBufferSize(640, 480) }
        val preview = Surface(texture)
        val jpeg = AtomicReference<ByteArray?>(null)
        val done = CountDownLatch(1)
        var device: CameraDevice? = null

        reader.setOnImageAvailableListener({ r ->
            r.acquireLatestImage()?.use { image ->
                val buffer = image.planes[0].buffer
                jpeg.set(ByteArray(buffer.remaining()).also { buffer.get(it) })
            }
            done.countDown()
        }, handler)

        try {
            manager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    device = camera
                    @Suppress("DEPRECATION")
                    camera.createCaptureSession(
                        listOf(preview, reader.surface),
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(session: CameraCaptureSession) {
                                settleThenShoot(camera, session, preview, reader, traits, handler)
                            }

                            override fun onConfigureFailed(session: CameraCaptureSession) {
                                done.countDown()
                            }
                        },
                        handler,
                    )
                }

                override fun onDisconnected(camera: CameraDevice) = done.countDown()
                override fun onError(camera: CameraDevice, error: Int) {
                    Log.w(TAG, "camera error $error")
                    done.countDown()
                }
            }, handler)
            done.await(TIMEOUT_S, TimeUnit.SECONDS)
        } catch (e: Exception) {
            Log.w(TAG, "photo failed", e)
        } finally {
            runCatching { device?.close() }
            runCatching { reader.close() }
            runCatching { preview.release(); texture.release() }
            thread.quitSafely()
        }
        return jpeg.get()?.let { Base64.encodeToString(it, Base64.NO_WRAP) }
    }

    private fun settleThenShoot(
        camera: CameraDevice,
        session: CameraCaptureSession,
        preview: Surface,
        reader: ImageReader,
        traits: CameraCharacteristics,
        handler: Handler,
    ) {
        val warm = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
            addTarget(preview)
            set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
            set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
        }.build()
        session.setRepeatingRequest(warm, null, handler)
        handler.postDelayed({
            runCatching {
                val still = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                    addTarget(reader.surface)
                    set(CaptureRequest.CONTROL_AF_MODE,
                        CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                    set(CaptureRequest.JPEG_ORIENTATION,
                        traits.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90)
                    set(CaptureRequest.JPEG_QUALITY, 85.toByte())
                }.build()
                session.stopRepeating()
                session.capture(still, null, handler)
            }.onFailure { Log.w(TAG, "still capture failed", it) }
        }, SETTLE_MS)
    }
}
