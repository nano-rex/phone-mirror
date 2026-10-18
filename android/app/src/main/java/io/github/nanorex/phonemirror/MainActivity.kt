package io.github.nanorex.phonemirror

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.google.zxing.integration.android.IntentIntegrator

/**
 * Setup checklist and start/stop button. Each step says what it is for, opens exactly the
 * system screen needed, and re-checks itself when the user comes back.
 */
class MainActivity : Activity() {

    private lateinit var store: PairingStore
    private lateinit var pairStatus: TextView
    private lateinit var pairButton: Button
    private lateinit var forgetButton: Button
    private lateinit var controlStatus: TextView
    private lateinit var controlButton: Button
    private lateinit var restrictedHelp: LinearLayout
    private lateinit var notifyCard: LinearLayout
    private lateinit var notifyStatus: TextView
    private lateinit var notifyButton: Button
    private lateinit var mirrorButton: Button
    private lateinit var mirrorStatus: TextView
    private var askedForControl = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = PairingStore(this)
        askedForControl = savedInstanceState?.getBoolean("askedForControl") ?: false
        buildUi()
        handlePairingIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handlePairingIntent(intent)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("askedForControl", askedForControl)
    }

    override fun onResume() {
        super.onResume()
        MirrorService.listener = { refresh() }
        refresh()
    }

    override fun onPause() {
        MirrorService.listener = null
        super.onPause()
    }

    // --- UI ------------------------------------------------------------------------------

    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    private fun text(size: Float, bold: Boolean = false) = TextView(this).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        if (bold) setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(4), 0, dp(4))
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun card(parent: LinearLayout, title: String, body: String): LinearLayout {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(12))
            background = GradientDrawable().apply {
                cornerRadius = dp(12).toFloat()
                setStroke(dp(1), 0x33888888)
            }
        }
        card.addView(text(18f, bold = true).apply { this.text = title })
        card.addView(text(14f).apply { this.text = body })
        parent.addView(card, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
        return card
    }

    private fun buildUi() {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }
        column.addView(text(14f).apply {
            text = "Show this phone on your computer and control it with the mouse and keyboard. " +
                "Open phonemirror on the computer first."
        })

        val pair = card(column, "1. Pair with your computer",
            "Scan the code that phonemirror shows on the computer. Only pair with a computer you trust: " +
                "while you are mirroring it can see your screen and, if you allow it, control the phone.")
        pairStatus = text(14f, bold = true).also { pair.addView(it) }
        pairButton = button("Scan the code") { scan() }.also { pair.addView(it) }
        forgetButton = button("Forget this computer") { forget() }.also { pair.addView(it) }

        val control = card(column, "2. Allow control (optional)",
            "Lets the computer tap, swipe and type on this phone. Without it you can still watch the " +
                "screen. phonemirror only acts while you are mirroring to your paired computer, and never " +
                "types into password fields.")
        controlStatus = text(14f, bold = true).also { control.addView(it) }
        controlButton = button("Turn on control") { askControl() }.also { control.addView(it) }
        restrictedHelp = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        restrictedHelp.addView(text(14f).apply {
            text = "Is the switch greyed out (\"Restricted setting\")? Android locks it for apps that " +
                "were not installed from a store. Open App info, tap ⋮ at the top right, choose " +
                "\"Allow restricted settings\", confirm, then try again."
        })
        restrictedHelp.addView(button("Open App info") { openAppInfo() })
        control.addView(restrictedHelp)

        notifyCard = card(column, "3. Show the Stop button",
            "Lets phonemirror show a notification with a Stop button while mirroring.")
        notifyStatus = text(14f, bold = true).also { notifyCard.addView(it) }
        notifyButton = button("Allow notifications") { askNotifications() }.also { notifyCard.addView(it) }

        mirrorButton = button("Start mirroring") { toggleMirroring() }.apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
            minHeight = dp(56)
        }
        column.addView(mirrorButton, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4) })
        mirrorStatus = text(14f).apply { gravity = Gravity.CENTER_HORIZONTAL }
        column.addView(mirrorStatus)

        setContentView(ScrollView(this).apply { addView(column) })
    }

    private fun refresh() {
        val pairing = store.load()
        pairStatus.text = if (pairing != null) "✓ Paired with ${pairing.pcName}" else "Not paired yet"
        pairButton.text = if (pairing != null) "Pair a different computer" else "Scan the code"
        forgetButton.visibility = if (pairing != null) View.VISIBLE else View.GONE

        val controlOn = ControlService.isEnabled(this)
        controlStatus.text = if (controlOn) "✓ Control is on" else "Off: mirroring only"
        controlButton.text = if (controlOn) "Turn off in Settings" else "Turn on control"
        restrictedHelp.visibility =
            if (!controlOn && askedForControl && Build.VERSION.SDK_INT >= 33) View.VISIBLE else View.GONE

        if (Build.VERSION.SDK_INT >= 33) {
            val granted = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            notifyStatus.text = if (granted) "✓ Allowed" else "Not allowed: you can still stop from this screen"
            notifyButton.visibility = if (granted) View.GONE else View.VISIBLE
        } else {
            notifyCard.visibility = View.GONE
        }

        val state = MirrorService.state
        mirrorButton.isEnabled = pairing != null && state != MirrorService.State.CONNECTING
        mirrorButton.text = when (state) {
            MirrorService.State.IDLE -> "Start mirroring"
            MirrorService.State.CONNECTING -> "Connecting…"
            MirrorService.State.MIRRORING -> "Stop mirroring"
        }
        mirrorStatus.text = when {
            pairing == null -> "Pair with your computer first."
            MirrorService.statusMessage.isNotEmpty() -> MirrorService.statusMessage
            else -> ""
        }
    }

    // --- pairing -------------------------------------------------------------------------

    private fun scan() {
        IntentIntegrator(this)
            .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
            .setPrompt("Scan the code shown by phonemirror on your computer")
            .setBeepEnabled(false)
            .setOrientationLocked(false)
            .initiateScan()
    }

    /** Pairing links come from the QR scanner, a phone camera app, or `phonemirror setup android`. */
    private fun handlePairingIntent(intent: Intent?) {
        val uri = when {
            intent == null -> null
            intent.action == Intent.ACTION_VIEW -> intent.dataString
            intent.action == ACTION_PAIR -> intent.getStringExtra("uri")
            else -> null
        } ?: return
        intent?.action = null // don't ask again on rotation
        confirmPairing(uri)
    }

    private fun confirmPairing(uri: String) {
        val pairing = try {
            Pairing.parse(uri)
        } catch (e: IllegalArgumentException) {
            AlertDialog.Builder(this).setMessage(e.message).setPositiveButton("OK", null).show()
            return
        }
        val existing = store.load()
        val replace = if (existing != null && existing.pcId != pairing.pcId) {
            "\n\nThis replaces the pairing with ${existing.pcName}."
        } else ""
        AlertDialog.Builder(this)
            .setTitle("Pair with “${pairing.pcName}”?")
            .setMessage("Only continue if this is your own computer and it is showing this code right now.$replace")
            .setPositiveButton("Pair") { _, _ ->
                store.save(pairing)
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun forget() {
        AlertDialog.Builder(this)
            .setMessage("Forget this computer? You will need to scan its code again to mirror.")
            .setPositiveButton("Forget") { _, _ ->
                store.clear()
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // --- control -------------------------------------------------------------------------

    private fun askControl() {
        if (ControlService.isEnabled(this)) {
            openAccessibilitySettings()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Turn on control")
            .setMessage("Next, Android opens its Accessibility settings.\n\n" +
                "1. Tap “phonemirror control” (it may be under “Downloaded apps” or “Installed apps”).\n" +
                "2. Turn the switch on and tap Allow.\n" +
                "3. Press Back to return here.\n\n" +
                "Android shows a strong warning because this permission can control the phone. " +
                "phonemirror uses it only while you are mirroring to your paired computer.")
            .setPositiveButton("Open settings") { _, _ ->
                askedForControl = true
                openAccessibilitySettings()
            }
            .setNegativeButton("Not now", null)
            .show()
    }

    private fun openAccessibilitySettings() {
        val component = ComponentName(this, ControlService::class.java).flattenToString()
        val direct = Intent("android.settings.ACCESSIBILITY_DETAILS_SETTINGS")
            .putExtra(Intent.EXTRA_COMPONENT_NAME, component)
        val list = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            // highlights our entry on Settings apps that support it
            .putExtra(":settings:fragment_args_key", component)
            .putExtra(":settings:show_fragment_args", Bundle().apply { putString(":settings:fragment_args_key", component) })
        for (i in listOfNotNull(if (Build.VERSION.SDK_INT >= 31) direct else null, list)) {
            try {
                startActivity(i)
                return
            } catch (_: ActivityNotFoundException) {
            } catch (_: SecurityException) {
            }
        }
    }

    private fun openAppInfo() {
        startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
    }

    private fun askNotifications() {
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFY)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refresh()
    }

    // --- mirroring -----------------------------------------------------------------------

    private fun toggleMirroring() {
        if (MirrorService.state == MirrorService.State.MIRRORING) {
            startService(Intent(this, MirrorService::class.java).setAction(MirrorService.ACTION_STOP))
            return
        }
        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        // On Android 14+ ask for the whole screen directly instead of offering "a single app"
        val request = if (Build.VERSION.SDK_INT >= 34) {
            mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
        } else {
            mpm.createScreenCaptureIntent()
        }
        startActivityForResult(request, REQ_CAPTURE)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                val service = Intent(this, MirrorService::class.java)
                    .setAction(MirrorService.ACTION_START)
                    .putExtra(MirrorService.EXTRA_RESULT_CODE, resultCode)
                    .putExtra(MirrorService.EXTRA_DATA, data)
                startForegroundService(service)
            } else {
                mirrorStatus.text = "Screen sharing was cancelled."
            }
            return
        }
        val scan = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (scan?.contents != null) confirmPairing(scan.contents)
    }

    companion object {
        const val ACTION_PAIR = "io.github.nanorex.phonemirror.PAIR"
        private const val REQ_CAPTURE = 1
        private const val REQ_NOTIFY = 2
    }
}
