package com.pocketmic.app.engine

/** Produces microphone audio for [StreamEngine] as 10 ms frames. */
interface AudioSource {
    /**
     * Starts delivering frames to [sink] from a thread owned by the source. If the microphone
     * cannot be opened, the source reports [MicState.UNAVAILABLE] through
     * [AudioSink.onMicStateChanged] (possibly before this returns) and may keep retrying.
     */
    fun start(sink: AudioSink)

    /** Stops delivery. No [AudioSink] call is made after this returns. */
    fun stop()
}

interface AudioSink {
    /**
     * Receives exactly [com.pocketmic.app.protocol.Protocol.FRAME_SAMPLES] mono 48 kHz samples.
     * The array is only valid for the duration of the call.
     */
    fun onFrame(frame: ShortArray)

    fun onMicStateChanged(state: MicState)
}

/** Whether the frames a source delivers carry real microphone audio. Sources start [LIVE]. */
enum class MicState {
    LIVE,

    /**
     * Recording, but Android hands over silence: a call or an app in the foreground has taken the
     * microphone, or microphone access is switched off. Audio returns by itself, so PCs can still
     * connect.
     */
    SILENCED,

    /** The microphone could not be opened; CONNECTs are rejected with MIC_UNAVAILABLE. */
    UNAVAILABLE,
}
