package com.pocketmic.app

import android.os.Looper
import androidx.annotation.MainThread
import com.pocketmic.app.engine.EngineSnapshot

/** Everything the UI shows about the microphone service. */
data class MicStatus(
    val running: Boolean,
    val engine: EngineSnapshot? = null,
    /** Why the last attempt to start failed; cleared by the next start. */
    val startError: String? = null,
) {
    companion object {
        val OFF = MicStatus(running = false)
    }
}

/**
 * Process-wide link between [MicService] (which publishes) and the UI (which listens).
 * Main thread only, so listeners never race with each other or with publishing.
 */
object AppState {
    private val listeners = mutableListOf<(MicStatus) -> Unit>()

    var status: MicStatus = MicStatus.OFF
        private set

    @MainThread
    fun publish(status: MicStatus) {
        checkMainThread()
        if (status == this.status) return
        this.status = status
        listeners.toList().forEach { it(status) }
    }

    /** Adds [listener] and immediately calls it with the current status. */
    @MainThread
    fun addListener(listener: (MicStatus) -> Unit) {
        checkMainThread()
        listeners += listener
        listener(status)
    }

    @MainThread
    fun removeListener(listener: (MicStatus) -> Unit) {
        checkMainThread()
        listeners -= listener
    }

    private fun checkMainThread() =
        check(Looper.myLooper() == Looper.getMainLooper()) { "AppState is main-thread only" }
}
