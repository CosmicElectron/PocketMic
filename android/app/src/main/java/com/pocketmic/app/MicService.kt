package com.pocketmic.app

import android.Manifest
import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.pocketmic.app.engine.EngineConfig
import com.pocketmic.app.engine.EngineSnapshot
import com.pocketmic.app.engine.EngineState
import com.pocketmic.app.engine.MicState
import com.pocketmic.app.engine.StreamEngine

/**
 * Foreground service (type microphone) that owns the microphone, the network engine, the
 * wake/Wi-Fi locks and the Wi-Fi binding for as long as the user keeps PocketMic on. MainActivity
 * starts it while visible, as Android 14+ requires for microphone access from a foreground service.
 */
class MicService : Service() {
    private data class NotificationContent(val text: String, val muted: Boolean)

    private val mainHandler = Handler(Looper.getMainLooper())
    private var engine: StreamEngine? = null
    private var locks: StreamLocks? = null
    private var wifi: WifiBinding? = null

    /** Content last posted by [showNotification], which skips identical updates. */
    private var shownNotification: NotificationContent? = null

    /** Refreshes the level meter and stats; discrete changes are also pushed by the engine. */
    private val refreshTick = object : Runnable {
        override fun run() {
            publish()
            mainHandler.postDelayed(this, UI_REFRESH_MS)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannelCompat.Builder(CHANNEL_ID, NotificationManagerCompat.IMPORTANCE_LOW)
            .setName(getString(R.string.channel_name))
            .setDescription(getString(R.string.channel_description))
            .setShowBadge(false)
            .build()
        NotificationManagerCompat.from(this).createNotificationChannel(channel)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start()
            ACTION_MUTE -> engine?.setMuted(true)
            ACTION_UNMUTE -> engine?.setMuted(false)
            ACTION_STOP -> stopSelf()
        }
        if (engine == null) stopSelf()
        // A restart by the system would happen in the background, where Android refuses
        // microphone access, so let the user start again instead.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        mainHandler.removeCallbacksAndMessages(null)
        engine?.let {
            it.listener = null
            it.stop()
        }
        engine = null
        wifi?.stop()
        wifi = null
        locks?.release()
        locks = null
        if (AppState.status.running) AppState.publish(MicStatus.OFF)
        super.onDestroy()
    }

    private fun start() {
        val current = engine
        val content = current?.let { contentFor(it.snapshot()) }
            ?: NotificationContent(getString(R.string.notification_waiting), muted = false)
        // Every startForegroundService() call must be answered with startForeground(), even when
        // already running, so do it first and unconditionally. Android 10 and older have no
        // microphone service type and do not need one.
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        } else {
            0
        }
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(content), type)
        } catch (e: IllegalStateException) {
            failStart(e)
            return
        } catch (e: SecurityException) {
            failStart(e)
            return
        }
        if (current != null) return

        val settings = AppSettings(this)
        val config = EngineConfig(settings.phoneId, settings.phoneName, settings.port)
        val newEngine = StreamEngine(config, MicCapture(this, settings.micMode))
        newEngine.listener = { mainHandler.post(::publish) }
        engine = newEngine
        locks = StreamLocks(this).also { it.acquire() }
        wifi = WifiBinding(this, onChange = newEngine::rebind).also { it.start() }
        newEngine.start()
        mainHandler.post(refreshTick)
    }

    private fun failStart(e: RuntimeException) {
        Log.e(TAG, "Android refused to start the microphone service", e)
        // A refused repeat start leaves the already running stream as it was.
        if (engine != null) return
        val reason = e.message ?: e.javaClass.simpleName
        AppState.publish(MicStatus(running = false, startError = getString(R.string.error_service_start, reason)))
    }

    private fun publish() {
        val snapshot = engine?.snapshot() ?: return
        AppState.publish(MicStatus(running = true, engine = snapshot))
        showNotification(contentFor(snapshot))
    }

    private fun contentFor(snapshot: EngineSnapshot): NotificationContent {
        val text = when {
            snapshot.mic == MicState.UNAVAILABLE -> getString(R.string.notification_mic_unavailable)
            snapshot.mic == MicState.SILENCED -> getString(R.string.notification_mic_silenced)
            snapshot.state == EngineState.STREAMING ->
                getString(R.string.notification_streaming, snapshot.pcName)
            snapshot.networkError != null -> getString(R.string.notification_network_problem)
            else -> getString(R.string.notification_waiting)
        }
        return NotificationContent(text, snapshot.muted)
    }

    private fun showNotification(content: NotificationContent) {
        if (content == shownNotification) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            // Not remembered as shown, so the next refresh posts it once the user allows notifications.
            return
        }
        NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, buildNotification(content))
        shownNotification = content
    }

    private fun buildNotification(content: NotificationContent): Notification {
        val openApp = PendingIntent.getActivity(
            this,
            REQUEST_OPEN,
            Intent(this, MainActivity::class.java)
                .setAction(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val muteAction = if (content.muted) {
            NotificationCompat.Action(
                R.drawable.ic_mic,
                getString(R.string.unmute),
                actionIntent(ACTION_UNMUTE, REQUEST_UNMUTE),
            )
        } else {
            NotificationCompat.Action(
                R.drawable.ic_mic_off,
                getString(R.string.mute),
                actionIntent(ACTION_MUTE, REQUEST_MUTE),
            )
        }
        val stopAction = NotificationCompat.Action(
            R.drawable.ic_stop,
            getString(R.string.stop),
            actionIntent(ACTION_STOP, REQUEST_STOP),
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(getString(if (content.muted) R.string.notification_title_muted else R.string.app_name))
            .setContentText(content.text)
            .setContentIntent(openApp)
            .addAction(muteAction)
            .addAction(stopAction)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun actionIntent(action: String, requestCode: Int): PendingIntent =
        PendingIntent.getService(
            this,
            requestCode,
            intent(this, action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    companion object {
        const val ACTION_START = "com.pocketmic.app.action.START"
        const val ACTION_MUTE = "com.pocketmic.app.action.MUTE"
        const val ACTION_UNMUTE = "com.pocketmic.app.action.UNMUTE"
        const val ACTION_STOP = "com.pocketmic.app.action.STOP"

        fun intent(context: Context, action: String): Intent =
            Intent(context, MicService::class.java).setAction(action)

        private const val TAG = "PocketMic"
        private const val CHANNEL_ID = "mic_status"
        private const val NOTIFICATION_ID = 1
        private const val UI_REFRESH_MS = 100L
        private const val REQUEST_OPEN = 0
        private const val REQUEST_MUTE = 1
        private const val REQUEST_UNMUTE = 2
        private const val REQUEST_STOP = 3
    }
}
