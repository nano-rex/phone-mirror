package io.github.nanorex.phonemirror

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.os.Build
import android.os.Handler
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import android.view.Surface
import android.view.WindowManager
import java.nio.ByteBuffer
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Captures the screen through MediaProjection into a hardware H.264 encoder and hands the
 * encoded packets to [sink]. Restarts the encoder at the new size when the screen rotates.
 */
class ScreenEncoder(
    private val context: Context,
    private val projection: MediaProjection,
    private val handler: Handler,
    private val sink: Sink,
) {
    interface Sink {
        fun onSession(width: Int, height: Int)
        fun onPacket(flags: Int, ptsUs: Long, data: ByteArray)
        fun onError(e: Exception)
    }

    @Volatile private var running = false
    @Volatile private var restart = false
    @Volatile private var screenDims = Pair(0, 0)
    private var virtualDisplay: VirtualDisplay? = null
    private var thread: Thread? = null
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) {}
        override fun onDisplayRemoved(displayId: Int) {}
        override fun onDisplayChanged(displayId: Int) {
            // rotation or a resolution change: restart the encoder at the new size
            if (displayId != Display.DEFAULT_DISPLAY) return
            val (w, h, _) = screenSize()
            if (Pair(w, h) != screenDims) restart = true
        }
    }

    fun start() {
        running = true
        displayManager.registerDisplayListener(displayListener, handler)
        thread = Thread({
            try {
                loop()
            } catch (e: Exception) {
                if (running) sink.onError(e)
            }
        }, "phonemirror-encoder").also { it.start() }
    }

    fun stop() {
        running = false
        displayManager.unregisterDisplayListener(displayListener)
        thread?.join(2000)
        virtualDisplay?.release()
        virtualDisplay = null
    }

    @Suppress("DEPRECATION")
    private fun screenSize(): Triple<Int, Int, Int> {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        wm.defaultDisplay.getRealMetrics(metrics)
        return Triple(metrics.widthPixels, metrics.heightPixels, metrics.densityDpi)
    }

    /** Scale so the longer side is at most [maxSide], keeping both sides multiples of 8. */
    private fun videoSize(width: Int, height: Int, maxSide: Int): Pair<Int, Int> {
        val scale = minOf(1.0, maxSide.toDouble() / max(width, height))
        fun align(v: Int) = max(8, (v * scale / 8).roundToInt() * 8)
        return Pair(align(width), align(height))
    }

    private fun loop() {
        var maxSide = MAX_SIDES.first()
        while (running) {
            restart = false
            val (sw, sh, dpi) = screenSize()
            screenDims = Pair(sw, sh)
            val (w, h) = videoSize(sw, sh, maxSide)
            val codec = try {
                createCodec(w, h)
            } catch (e: Exception) {
                // some encoders refuse large sizes: try the next smaller one
                Log.w(TAG, "encoder rejected ${w}x$h", e)
                val next = MAX_SIDES.indexOf(maxSide) + 1
                if (next >= MAX_SIDES.size) throw e
                maxSide = MAX_SIDES[next]
                continue
            }
            val surface = codec.createInputSurface()
            codec.start()
            val vd = virtualDisplay
            if (vd == null) {
                virtualDisplay = projection.createVirtualDisplay(
                    "phonemirror", w, h, dpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    surface, null, handler,
                )
            } else {
                vd.resize(w, h, dpi)
                vd.surface = surface
            }
            sink.onSession(w, h)
            try {
                drain(codec)
            } finally {
                virtualDisplay?.surface = null
                try {
                    codec.stop()
                } catch (_: Exception) {
                }
                codec.release()
                surface.release()
            }
        }
    }

    private fun createCodec(w: Int, h: Int): MediaCodec {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, 8_000_000)
            setInteger(MediaFormat.KEY_FRAME_RATE, 60)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 10)
            // keep sending frames while the screen is still, so the computer is never stale
            setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 100_000L)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                setFloat(MediaFormat.KEY_MAX_FPS_TO_ENCODER, 60f)
            }
        }
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (e: Exception) {
            codec.release()
            throw e
        }
        return codec
    }

    private fun drain(codec: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (running && !restart) {
            val index = codec.dequeueOutputBuffer(info, 100_000)
            if (index < 0) continue
            val buffer: ByteBuffer? = codec.getOutputBuffer(index)
            if (buffer != null && info.size > 0) {
                buffer.position(info.offset)
                buffer.limit(info.offset + info.size)
                val data = ByteArray(info.size)
                buffer.get(data)
                var flags = 0
                if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) flags = flags or Protocol.FLAG_CONFIG
                if (info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0) flags = flags or Protocol.FLAG_KEY
                sink.onPacket(flags, info.presentationTimeUs, data)
            }
            codec.releaseOutputBuffer(index, false)
            if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
        }
    }

    companion object {
        private const val TAG = "phonemirror"
        private val MAX_SIDES = listOf(1600, 1280, 960)
    }
}
