package io.github.nanorex.phonemirror

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.nio.ByteBuffer

/**
 * Foreground service that runs one mirroring session: connect to the paired computer,
 * authenticate, stream the screen, and pass the computer's input to [ControlService].
 * Everything stops when the user taps Stop, the computer disconnects, or Android revokes
 * the screen capture.
 */
class MirrorService : Service() {

    enum class State { IDLE, CONNECTING, MIRRORING }

    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var encoder: ScreenEncoder? = null
    @Volatile private var link: SecureLink? = null
    @Volatile private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown(null)
            return START_NOT_STICKY
        }
        if (state != State.IDLE) return START_NOT_STICKY
        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, 0) ?: 0
        @Suppress("DEPRECATION")
        val data: Intent? = intent?.getParcelableExtra(EXTRA_DATA)
        val pairing = PairingStore(this).load()
        if (data == null || pairing == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        // Android 14+ requires the media projection foreground service to be running
        // before the projection is created.
        startInForeground(getString(R.string.notif_connecting, pairing.pcName))
        setState(State.CONNECTING, getString(R.string.status_connecting, pairing.pcName))
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val mp = try {
            mpm.getMediaProjection(resultCode, data)
        } catch (e: Exception) {
            null
        }
        if (mp == null) {
            shutdown(getString(R.string.status_capture_denied))
            return START_NOT_STICKY
        }
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                shutdown(null) // the user stopped sharing from the system UI
            }
        }, main)
        Thread({ session(pairing, mp) }, "phonemirror-session").start()
        return START_NOT_STICKY
    }

    private fun session(pairing: Pairing, mp: MediaProjection) {
        try {
            val socket = Connector(this, PairingStore(this)).connect(pairing)
            socket.soTimeout = 0
            val l = SecureLink.connect(socket, pairing.pairId, pairing.key)
            link = l
            val control = ControlService.instance
            val info = JSONObject()
                .put("name", deviceName())
                .put("model", Build.MODEL)
                .put("android", Build.VERSION.RELEASE)
                .put("control", control != null)
            l.send(byteArrayOf(Protocol.MSG_INFO) + info.toString().toByteArray())
            control?.beginSession { message -> sendStatus(message) }

            main.post {
                if (stopping) return@post
                val enc = ScreenEncoder(this, mp, main, object : ScreenEncoder.Sink {
                    override fun onSession(width: Int, height: Int) {
                        send(ByteBuffer.allocate(9).put(Protocol.MSG_VIDEO_SESSION).putInt(width).putInt(height).array())
                    }

                    override fun onPacket(flags: Int, ptsUs: Long, data: ByteArray) {
                        val header = ByteBuffer.allocate(10).put(Protocol.MSG_VIDEO_PACKET)
                            .put(flags.toByte()).putLong(ptsUs).array()
                        send(header + data)
                    }

                    override fun onError(e: Exception) {
                        Log.e(TAG, "encoder failed", e)
                        shutdown(getString(R.string.status_encoder_failed))
                    }
                })
                encoder = enc
                enc.start()
                updateNotification(getString(R.string.notif_mirroring, pairing.pcName))
                setState(State.MIRRORING, getString(R.string.status_mirroring, pairing.pcName))
            }

            while (!stopping) {
                val message = l.recv()
                if (message.isNotEmpty() && message[0] >= Protocol.MSG_TOUCH) {
                    val svc = ControlService.instance
                    if (svc != null) svc.handle(message) else sendStatusOnce()
                }
            }
        } catch (e: Exception) {
            if (!stopping) {
                Log.i(TAG, "session ended", e)
                shutdown(e.message ?: getString(R.string.status_disconnected))
            }
        }
    }

    private var controlHintSent = false

    private fun sendStatusOnce() {
        if (controlHintSent) return
        controlHintSent = true
        sendStatus(getString(R.string.status_control_off))
    }

    private fun sendStatus(message: String) {
        val json = JSONObject().put("control", ControlService.instance != null).put("message", message)
        send(byteArrayOf(Protocol.MSG_STATUS) + json.toString().toByteArray())
    }

    private fun send(message: ByteArray) {
        val l = link ?: return
        try {
            l.send(message)
        } catch (e: Exception) {
            if (!stopping) shutdown(getString(R.string.status_disconnected))
        }
    }

    private fun deviceName(): String {
        val name = try {
            android.provider.Settings.Global.getString(contentResolver, "device_name")
        } catch (e: Exception) {
            null
        }
        return (name ?: "${Build.MANUFACTURER} ${Build.MODEL}").take(64)
    }

    /** Stops everything; safe to call from any thread, more than once. */
    private fun shutdown(message: String?) {
        main.post {
            if (stopping) return@post
            stopping = true
            ControlService.instance?.endSession()
            val enc = encoder
            encoder = null
            Thread {
                link?.close()
                enc?.stop() // joins the encoder thread, so keep it off the main thread
                main.post {
                    try {
                        projection?.stop()
                    } catch (_: Exception) {
                    }
                    projection = null
                    setState(State.IDLE, message ?: "")
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf()
                }
            }.start()
        }
    }

    // --- notification ------------------------------------------------------------------

    private fun notification(text: String): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW)
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, MirrorService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            this, 2, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, getString(R.string.stop), stop).build())
            .build()
    }

    private fun startInForeground(text: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification(text), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(NOTIFICATION_ID, notification(text))
        }
    }

    private fun updateNotification(text: String) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIFICATION_ID, notification(text))
    }

    companion object {
        private const val TAG = "phonemirror"
        private const val CHANNEL = "mirroring"
        private const val NOTIFICATION_ID = 1
        const val ACTION_START = "io.github.nanorex.phonemirror.START"
        const val ACTION_STOP = "io.github.nanorex.phonemirror.STOP"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_DATA = "data"

        @Volatile var state = State.IDLE
            private set
        @Volatile var statusMessage = ""
            private set
        /** Called on the main thread whenever [state] or [statusMessage] changes. */
        var listener: (() -> Unit)? = null

        private fun setState(s: State, message: String) {
            state = s
            statusMessage = message
            Handler(Looper.getMainLooper()).post { listener?.invoke() }
        }
    }
}
