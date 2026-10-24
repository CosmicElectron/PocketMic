package com.pocketmic.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.ArrayAdapter
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.core.view.updatePadding
import androidx.core.widget.doAfterTextChanged
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import com.pocketmic.app.databinding.ActivityMainBinding
import com.pocketmic.app.engine.EngineSnapshot
import com.pocketmic.app.engine.EngineState
import com.pocketmic.app.engine.MicState
import java.util.Locale
import kotlin.math.log10
import kotlin.math.roundToInt

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: AppSettings
    private lateinit var wifiMonitor: WifiAddressMonitor
    private var wifiAddress: String? = null
    private var dialog: AlertDialog? = null
    private val statusListener: (MicStatus) -> Unit = { render(it) }

    /** shouldShowRequestPermissionRationale(RECORD_AUDIO) when the pending request was launched. */
    private var micRationaleBeforeRequest = false

    private val permissionRequest =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions(), ::onPermissionResult)
    private val notificationRequest =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) showNotificationsDenied()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        micRationaleBeforeRequest = savedInstanceState?.getBoolean(KEY_MIC_RATIONALE_BEFORE_REQUEST) == true
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or
                    WindowInsetsCompat.Type.displayCutout() or
                    WindowInsetsCompat.Type.ime(),
            )
            view.updatePadding(left = bars.left, top = bars.top, right = bars.right, bottom = bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        settings = AppSettings(this)
        wifiMonitor = WifiAddressMonitor(this) { address ->
            wifiAddress = address
            render(AppState.status)
        }

        binding.micModeSpinner.adapter = ArrayAdapter.createFromResource(
            this,
            R.array.mic_modes,
            android.R.layout.simple_spinner_item,
        ).apply { setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }
        // On recreation (e.g. rotation) the views restore what the user had typed.
        if (savedInstanceState == null) {
            binding.phoneNameInput.setText(settings.phoneName)
            // ASCII digits whatever the locale, so the value parses back with toIntOrNull().
            binding.portInput.setText(String.format(Locale.ROOT, "%d", settings.port))
            binding.micModeSpinner.setSelection(settings.micMode.ordinal)
        }

        binding.startButton.setOnClickListener { onStartStopClicked() }
        binding.muteButton.setOnClickListener { toggleMute() }
        binding.portInput.doAfterTextChanged {
            binding.portLayout.error = null
            render(AppState.status)
        }
    }

    override fun onStart() {
        super.onStart()
        AppState.addListener(statusListener)
        wifiMonitor.start()
    }

    override fun onPause() {
        super.onPause()
        if (!AppState.status.running) saveSettings(reportErrors = false)
    }

    // A pending permission result is delivered to the recreated activity, which needs this too.
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean(KEY_MIC_RATIONALE_BEFORE_REQUEST, micRationaleBeforeRequest)
    }

    override fun onStop() {
        AppState.removeListener(statusListener)
        wifiMonitor.stop()
        super.onStop()
    }

    override fun onDestroy() {
        dialog?.dismiss()
        dialog = null
        super.onDestroy()
    }

    private fun render(status: MicStatus) {
        val running = status.running
        val engine = status.engine.takeIf { running }

        binding.statusText.text = when {
            !running -> getString(R.string.status_off)
            engine == null || engine.state == EngineState.STARTING -> getString(R.string.status_starting)
            engine.state == EngineState.STREAMING -> getString(R.string.status_streaming, engine.pcName)
            else -> getString(R.string.status_waiting)
        }

        val error = when {
            status.startError != null -> status.startError
            engine == null -> null
            engine.mic == MicState.UNAVAILABLE -> getString(R.string.error_mic_unavailable)
            engine.mic == MicState.SILENCED -> getString(R.string.error_mic_silenced)
            engine.networkError != null -> getString(R.string.error_network, engine.networkError)
            else -> null
        }
        binding.errorText.text = error
        binding.errorText.isVisible = error != null

        val port = engine?.port?.takeIf { it != 0 }
            ?: binding.portInput.text?.toString()?.toIntOrNull()
            ?: settings.port
        binding.addressText.text = wifiAddress
            ?.let { getString(R.string.address_format, it, port) }
            ?: getString(R.string.address_unknown)
        binding.wifiWarning.isVisible = wifiAddress == null

        binding.startButton.setText(if (running) R.string.stop else R.string.start)
        binding.muteButton.isEnabled = running
        binding.muteButton.setText(if (engine?.muted == true) R.string.unmute else R.string.mute)
        binding.levelMeter.progress = meterPercent(engine?.level ?: 0f)
        binding.statsText.text = statsText(engine)

        binding.micModeSpinner.isEnabled = !running
        binding.phoneNameLayout.isEnabled = !running
        binding.portLayout.isEnabled = !running
    }

    private fun statsText(engine: EngineSnapshot?): String {
        if (engine == null || engine.state != EngineState.STREAMING) return getString(R.string.stats_idle)
        val unknown = getString(R.string.stats_unknown)
        val loss = engine.lossPermille?.let { getString(R.string.stats_loss, it / 10f) } ?: unknown
        val buffer = engine.bufferMs?.let { getString(R.string.stats_buffer, it) } ?: unknown
        return getString(R.string.stats_format, engine.packetsSent, loss, buffer)
    }

    /** Maps a 0..1 peak level onto 0..100 over a -60..0 dBFS scale. */
    private fun meterPercent(level: Float): Int {
        if (level <= 0f) return 0
        val db = 20 * log10(level)
        return ((db - METER_FLOOR_DB) / -METER_FLOOR_DB * 100).roundToInt().coerceIn(0, 100)
    }

    private fun onStartStopClicked() {
        if (AppState.status.running) {
            stopService(Intent(this, MicService::class.java))
            return
        }
        if (!saveSettings(reportErrors = true)) return
        val mic = Manifest.permission.RECORD_AUDIO
        when (MicPermission.onStart(isGranted(mic), shouldShowRequestPermissionRationale(mic))) {
            MicPermission.OnStart.STREAM -> {
                startMic()
                missingNotificationPermission()?.let(notificationRequest::launch)
            }
            MicPermission.OnStart.EXPLAIN_THEN_ASK -> dialog = MaterialAlertDialogBuilder(this)
                .setTitle(R.string.permission_title)
                .setMessage(R.string.permission_rationale)
                .setPositiveButton(R.string.permission_continue) { _, _ -> askForPermissions() }
                .setNegativeButton(android.R.string.cancel, null)
                .show()
            MicPermission.OnStart.ASK -> askForPermissions()
        }
    }

    /** Asks for the microphone, and for notifications too if they are still missing. */
    private fun askForPermissions() {
        val mic = Manifest.permission.RECORD_AUDIO
        micRationaleBeforeRequest = shouldShowRequestPermissionRationale(mic)
        permissionRequest.launch(listOfNotNull(mic, missingNotificationPermission()).toTypedArray())
    }

    private fun onPermissionResult(results: Map<String, Boolean>) {
        // Empty when Android cancelled the request without asking anything.
        if (results.isEmpty()) return
        val mic = Manifest.permission.RECORD_AUDIO
        val outcome = MicPermission.outcome(
            granted = isGranted(mic),
            rationaleBefore = micRationaleBeforeRequest,
            rationaleAfter = shouldShowRequestPermissionRationale(mic),
        )
        when (outcome) {
            MicPermission.Outcome.STREAM -> {
                if (missingNotificationPermission() != null) showNotificationsDenied()
                startMic()
            }
            MicPermission.Outcome.BLOCKED -> showOpenSettingsDialog()
            MicPermission.Outcome.DENIED ->
                Snackbar.make(binding.root, R.string.mic_permission_denied, Snackbar.LENGTH_LONG)
                    .setAction(R.string.open_settings) { openAppSettings() }
                    .show()
        }
    }

    private fun showNotificationsDenied() =
        Snackbar.make(binding.root, R.string.notifications_denied, Snackbar.LENGTH_LONG).show()

    private fun startMic() {
        // Must happen while this activity is visible: Android 14+ only lets a foreground service
        // use the microphone if it was started from the foreground.
        try {
            ContextCompat.startForegroundService(this, MicService.intent(this, MicService.ACTION_START))
        } catch (e: IllegalStateException) {
            val reason = e.message ?: e.javaClass.simpleName
            AppState.publish(MicStatus(running = false, startError = getString(R.string.error_service_start, reason)))
        }
    }

    private fun toggleMute() {
        val muted = AppState.status.engine?.muted == true
        startService(MicService.intent(this, if (muted) MicService.ACTION_UNMUTE else MicService.ACTION_MUTE))
    }

    /** Saves the settings; an invalid port is kept out and, if [reportErrors], flagged. */
    private fun saveSettings(reportErrors: Boolean): Boolean {
        settings.phoneName = binding.phoneNameInput.text?.toString().orEmpty()
        settings.micMode = MicMode.entries[binding.micModeSpinner.selectedItemPosition]
        val port = binding.portInput.text?.toString()?.trim()?.toIntOrNull()
        if (port == null || !AppSettings.isValidPort(port)) {
            if (reportErrors) {
                binding.portLayout.error = getString(R.string.error_port, AppSettings.MIN_PORT, AppSettings.MAX_PORT)
            }
            return false
        }
        settings.port = port
        return true
    }

    private fun showOpenSettingsDialog() {
        dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.permission_settings_title)
            .setMessage(R.string.permission_settings_message)
            .setPositiveButton(R.string.open_settings) { _, _ -> openAppSettings() }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun openAppSettings() {
        startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
    }

    /** POST_NOTIFICATIONS if this Android version has it and it is not granted; optional for streaming. */
    private fun missingNotificationPermission(): String? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            !isGranted(Manifest.permission.POST_NOTIFICATIONS)
        ) {
            Manifest.permission.POST_NOTIFICATIONS
        } else {
            null
        }

    private fun isGranted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private companion object {
        const val METER_FLOOR_DB = -60f
        const val KEY_MIC_RATIONALE_BEFORE_REQUEST = "mic_rationale_before_request"
    }
}
