package io.github.nanorex.phonemirror

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.graphics.Path
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.nio.ByteBuffer
import kotlin.math.max
import kotlin.math.min

/**
 * Turns input from the computer into gestures and actions on this phone.
 *
 * It does nothing unless a mirroring session that passed the pairing handshake is running
 * ([beginSession]/[endSession] are called only by [MirrorService]). It reads no screen
 * content except the focused text field when the computer types, and refuses to edit
 * password fields.
 */
class ControlService : AccessibilityService() {

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var sessionActive = false
    private var report: ((String) -> Unit)? = null

    // live touch stroke state (main thread only)
    private var stroke: GestureDescription.StrokeDescription? = null
    private var lastX = 0f
    private var lastY = 0f
    private var lastTime = 0L
    private var inFlight = false
    private var pendingMove: Pair<Float, Float>? = null
    private var pendingUp: Pair<Float, Float>? = null
    private var generation = 0 // bumps on each new touch so stale callbacks are ignored

    override fun onServiceConnected() {
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    fun beginSession(reporter: (String) -> Unit) {
        main.post {
            report = reporter
            sessionActive = true
        }
    }

    fun endSession() {
        main.post {
            sessionActive = false
            report = null
            releaseTouch()
        }
    }

    /** Called from the network thread with one decrypted computer-to-phone message. */
    fun handle(message: ByteArray) {
        main.post {
            if (!sessionActive || message.isEmpty()) return@post
            try {
                dispatch(message)
            } catch (e: RuntimeException) {
                report?.invoke("Couldn't do that on the phone: ${e.message}")
            }
        }
    }

    private fun dispatch(message: ByteArray) {
        val buf = ByteBuffer.wrap(message, 1, message.size - 1)
        val payload = String(message, 1, message.size - 1, Charsets.UTF_8)
        when (message[0]) {
            Protocol.MSG_TOUCH -> {
                val action = buf.get().toInt()
                val (x, y) = toScreen(buf.float, buf.float)
                when (action) {
                    Protocol.TOUCH_DOWN -> touchDown(x, y)
                    Protocol.TOUCH_MOVE -> { pendingMove = Pair(x, y); pump() }
                    Protocol.TOUCH_UP -> { pendingUp = Pair(x, y); pump() }
                }
            }
            Protocol.MSG_SCROLL -> {
                val (x, y) = toScreen(buf.float, buf.float)
                scroll(x, y, buf.float, buf.float)
            }
            Protocol.MSG_KEY -> key(payload)
            Protocol.MSG_TEXT -> editFocused { text, start, end -> Triple(text.replaceRange(start, end, payload), start + payload.length, start + payload.length) }
            Protocol.MSG_CLIPBOARD -> {
                val text = String(message, 2, message.size - 2, Charsets.UTF_8)
                val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("phonemirror", text))
                if (message[1].toInt() == 1) focusedField()?.performAction(AccessibilityNodeInfo.ACTION_PASTE)
            }
        }
    }

    // --- coordinates ---------------------------------------------------------------------

    @Suppress("DEPRECATION")
    private fun screen(): Pair<Int, Int> {
        val metrics = DisplayMetrics()
        (getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(metrics)
        return Pair(metrics.widthPixels, metrics.heightPixels)
    }

    private fun toScreen(nx: Float, ny: Float): Pair<Float, Float> {
        val (w, h) = screen()
        val x = (nx.coerceIn(0f, 1f) * w).coerceIn(0f, w - 1f)
        val y = (ny.coerceIn(0f, 1f) * h).coerceIn(0f, h - 1f)
        return Pair(x, y)
    }

    // --- touch -----------------------------------------------------------------------------

    private fun touchDown(x: Float, y: Float) {
        // a new gesture cancels any unfinished one, which lifts its finger
        generation++
        inFlight = false
        pendingMove = null
        pendingUp = null
        val path = Path().apply { moveTo(x, y) }
        val first = GestureDescription.StrokeDescription(path, 0, 1, true)
        stroke = first
        lastX = x
        lastY = y
        lastTime = System.currentTimeMillis()
        send(first, final = false)
    }

    /** Sends the next stroke segment once the previous one finished (the API requires it). */
    private fun pump() {
        val current = stroke ?: return
        if (inFlight) return
        val up = pendingUp
        val target = up ?: pendingMove ?: Pair(lastX, lastY) // nothing new: keep holding
        val now = System.currentTimeMillis()
        val duration = (now - lastTime).coerceIn(1, 40)
        val path = Path().apply {
            moveTo(lastX, lastY)
            // a stroke path must not be empty; a 0.1 px nudge is invisible
            if (target.first == lastX && target.second == lastY) lineTo(lastX + 0.1f, lastY)
            else lineTo(target.first, target.second)
        }
        val next = current.continueStroke(path, 0, if (up == null && pendingMove == null) 50 else duration, up == null)
        pendingMove = null
        pendingUp = null
        stroke = next
        lastX = target.first
        lastY = target.second
        lastTime = now
        send(next, final = up != null)
    }

    private fun send(s: GestureDescription.StrokeDescription, final: Boolean) {
        inFlight = true
        val gen = generation
        val gesture = GestureDescription.Builder().addStroke(s).build()
        val ok = dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                if (gen != generation) return
                inFlight = false
                if (final) stroke = null else pump()
            }

            override fun onCancelled(g: GestureDescription?) {
                if (gen != generation) return
                inFlight = false
                stroke = null
                pendingMove = null
                pendingUp = null
            }
        }, main)
        if (!ok) {
            inFlight = false
            stroke = null
        }
    }

    /** Lift any finger still down, e.g. when the computer disconnects mid-drag. */
    private fun releaseTouch() {
        if (stroke != null) {
            pendingMove = null
            pendingUp = Pair(lastX, lastY)
            if (!inFlight) pump()
        }
    }

    private fun scroll(x: Float, y: Float, dx: Float, dy: Float) {
        if (stroke != null) return
        val (w, h) = screen()
        val step = min(w, h) * 0.12f
        // wheel away from the user (dy > 0) shows content above: the finger moves down
        val tx = (x - dx * step).coerceIn(1f, w - 2f)
        val ty = (y + dy * step).coerceIn(1f, h - 2f)
        if (tx == x && ty == y) return
        val path = Path().apply { moveTo(x, y); lineTo(tx, ty) }
        dispatchGesture(
            GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, 120)).build(),
            null, null,
        )
    }

    // --- keys and text -------------------------------------------------------------------

    private fun key(name: String) {
        when (name) {
            "back", "escape" -> performGlobalAction(GLOBAL_ACTION_BACK)
            "home" -> performGlobalAction(GLOBAL_ACTION_HOME)
            "recents" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
            "notifications" -> performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)
            "settings" -> performGlobalAction(GLOBAL_ACTION_QUICK_SETTINGS)
            "collapse" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                performGlobalAction(GLOBAL_ACTION_DISMISS_NOTIFICATION_SHADE)
            } else {
                performGlobalAction(GLOBAL_ACTION_BACK)
            }
            "power" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)
            } else {
                report?.invoke("Locking the screen needs Android 9 or newer.")
            }
            "volume_up", "volume_down" -> {
                val audio = getSystemService(Context.AUDIO_SERVICE) as AudioManager
                audio.adjustStreamVolume(
                    AudioManager.STREAM_MUSIC,
                    if (name == "volume_up") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER,
                    AudioManager.FLAG_SHOW_UI,
                )
            }
            "enter" -> {
                val field = focusedField() ?: return
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
                    field.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
                ) return
                if (field.isMultiLine) editFocused { t, s, e -> Triple(t.replaceRange(s, e, "\n"), s + 1, s + 1) }
            }
            "backspace" -> editFocused { t, s, e ->
                if (s != e) Triple(t.removeRange(s, e), s, s)
                else if (s > 0) Triple(t.removeRange(s - 1, s), s - 1, s - 1)
                else Triple(t, s, s)
            }
            "delete" -> editFocused { t, s, e ->
                if (s != e) Triple(t.removeRange(s, e), s, s)
                else if (s < t.length) Triple(t.removeRange(s, s + 1), s, s)
                else Triple(t, s, s)
            }
            "left" -> editFocused { t, s, e -> val p = if (s != e) min(s, e) else max(s - 1, 0); Triple(t, p, p) }
            "right" -> editFocused { t, s, e -> val p = if (s != e) max(s, e) else min(e + 1, t.length); Triple(t, p, p) }
            "move_home" -> editFocused { t, _, _ -> Triple(t, 0, 0) }
            "move_end" -> editFocused { t, _, _ -> Triple(t, t.length, t.length) }
        }
    }

    private fun focusedField(): AccessibilityNodeInfo? =
        rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }

    /**
     * Edits the focused field through accessibility actions. [edit] gets the current text and
     * selection and returns (new text, selection start, selection end).
     */
    private fun editFocused(edit: (String, Int, Int) -> Triple<String, Int, Int>) {
        val field = focusedField()
        if (field == null) {
            report?.invoke("Tap a text field on the phone first.")
            return
        }
        if (field.isPassword) {
            // the visible text is masked, so editing it could corrupt the password
            report?.invoke("For your security, type passwords on the phone itself.")
            return
        }
        val showingHint = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && field.isShowingHintText
        val text = if (showingHint) "" else field.text?.toString() ?: ""
        var start = field.textSelectionStart
        var end = field.textSelectionEnd
        if (start < 0 || end < 0 || start > text.length || end > text.length) {
            start = text.length
            end = text.length
        }
        val (newText, selStart, selEnd) = edit(text, min(start, end), max(start, end))
        if (newText != text) {
            val args = Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, newText)
            }
            if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                report?.invoke("This app doesn't accept typed text from the computer.")
                return
            }
        }
        val sel = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, selStart)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, selEnd)
        }
        field.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
    }

    companion object {
        @Volatile var instance: ControlService? = null
            private set

        fun isEnabled(context: Context): Boolean {
            val enabled = Settings.Secure.getString(
                context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            ) ?: return false
            val me = ComponentName(context, ControlService::class.java)
            return enabled.split(':').any {
                ComponentName.unflattenFromString(it) == me
            }
        }
    }
}
