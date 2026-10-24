package com.pocketmic.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Process
import android.util.Log
import androidx.core.content.ContextCompat
import com.pocketmic.app.engine.AudioSink
import com.pocketmic.app.engine.AudioSource
import com.pocketmic.app.engine.MicState
import com.pocketmic.app.protocol.Protocol
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Records 48 kHz mono 16-bit audio with [AudioRecord] on an urgent-audio-priority thread and hands
 * it to the engine in 480-sample frames. Blocking reads pace the loop, so there are no sleeps.
 * If the microphone cannot be opened, or the audio server dies under it (ERROR_DEAD_OBJECT), the
 * record is recreated and the sink is told when the mic goes away and comes back. The sink is
 * also told when Android silences the record (see [isSilenced]).
 */
class MicCapture(context: Context, private val mode: MicMode) : AudioSource {
    private val appContext = context.applicationContext
    private val stopSignal = CountDownLatch(1)
    private var thread: Thread? = null

    private val isStopping: Boolean get() = stopSignal.count == 0L

    override fun start(sink: AudioSink) {
        check(thread == null) { "MicCapture can only be started once" }
        // Opened here rather than on the capture thread so a failure is reported before the
        // engine starts answering CONNECTs.
        val record = openRecord()?.let(::startOrRelease)
        if (record == null) sink.onMicStateChanged(MicState.UNAVAILABLE)
        thread = Thread({ capture(sink, record) }, "PocketMic-capture").apply { start() }
    }

    override fun stop() {
        stopSignal.countDown()
        thread?.join(STOP_JOIN_MS)
    }

    private fun capture(sink: AudioSink, initial: AudioRecord?) {
        try {
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        } catch (e: SecurityException) {
            // Some builds restrict this priority; capturing at normal priority still works.
            Log.w(TAG, "Could not raise the capture thread priority", e)
        }
        val frame = ShortArray(Protocol.FRAME_SAMPLES)
        var record = initial
        var reported = if (initial == null) MicState.UNAVAILABLE else MicState.LIVE
        fun report(state: MicState) {
            if (state == reported) return
            reported = state
            sink.onMicStateChanged(state)
        }
        var framesSinceSilenceCheck = 0
        try {
            while (!isStopping) {
                if (record == null) {
                    if (stopSignal.await(REOPEN_DELAY_MS, TimeUnit.MILLISECONDS)) break
                    record = openRecord()?.let(::startOrRelease) ?: continue
                    report(MicState.LIVE)
                }
                if (!readFrame(record, frame)) {
                    if (isStopping) break
                    Log.w(TAG, "AudioRecord read failed; reopening the microphone")
                    record.release()
                    record = openRecord()?.let(::startOrRelease)
                    report(if (record == null) MicState.UNAVAILABLE else MicState.LIVE)
                    continue
                }
                if (++framesSinceSilenceCheck >= SILENCE_CHECK_FRAMES) {
                    framesSinceSilenceCheck = 0
                    report(if (isSilenced(record)) MicState.SILENCED else MicState.LIVE)
                }
                sink.onFrame(frame)
            }
        } finally {
            record?.release()
        }
    }

    /**
     * Android 10+ does not fail a record whose microphone is taken by a call or by an app in the
     * foreground (or blocked by the microphone privacy switch): reads go on and return zeros.
     * Polled rather than taken from AudioRecordingCallback, which only fires while audio flows.
     */
    private fun isSilenced(record: AudioRecord): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            record.activeRecordingConfiguration?.isClientSilenced == true

    /** Fills [frame] completely; false on any read error (e.g. ERROR_DEAD_OBJECT). */
    private fun readFrame(record: AudioRecord, frame: ShortArray): Boolean {
        var filled = 0
        while (filled < frame.size) {
            val read = record.read(frame, filled, frame.size - filled, AudioRecord.READ_BLOCKING)
            if (read <= 0) return false
            filled += read
        }
        return true
    }

    private fun openRecord(): AudioRecord? {
        if (ContextCompat.checkSelfPermission(appContext, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return null
        }
        val minBytes = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_MASK, ENCODING)
        if (minBytes <= 0) return null
        val record = try {
            AudioRecord(audioSource(), SAMPLE_RATE, CHANNEL_MASK, ENCODING, maxOf(minBytes, BUFFER_BYTES))
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "AudioRecord rejected its parameters", e)
            return null
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "AudioRecord failed to initialise")
            record.release()
            return null
        }
        return record
    }

    private fun startOrRelease(record: AudioRecord): AudioRecord? {
        try {
            record.startRecording()
        } catch (e: IllegalStateException) {
            Log.w(TAG, "AudioRecord failed to start", e)
        }
        if (record.recordingState == AudioRecord.RECORDSTATE_RECORDING) return record
        record.release()
        return null
    }

    private fun audioSource(): Int = when (mode) {
        MicMode.STANDARD -> MediaRecorder.AudioSource.MIC
        MicMode.NOISE_SUPPRESSION -> MediaRecorder.AudioSource.VOICE_COMMUNICATION
        MicMode.RAW -> {
            val audioManager = appContext.getSystemService(AudioManager::class.java)
            val unprocessed = audioManager?.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
            if (unprocessed == "true") {
                MediaRecorder.AudioSource.UNPROCESSED
            } else {
                MediaRecorder.AudioSource.VOICE_RECOGNITION
            }
        }
    }

    private companion object {
        const val TAG = "PocketMic"
        const val SAMPLE_RATE = Protocol.SAMPLE_RATE.toInt()
        const val CHANNEL_MASK = AudioFormat.CHANNEL_IN_MONO
        const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        /** 100 ms of slack against scheduling hiccups; reads still return every 10 ms. */
        const val BUFFER_BYTES = SAMPLE_RATE / 10 * 2
        const val REOPEN_DELAY_MS = 1_000L
        const val STOP_JOIN_MS = 1_000L

        /** About once a second: the check is a call into the audio service. */
        const val SILENCE_CHECK_FRAMES = 100
    }
}
