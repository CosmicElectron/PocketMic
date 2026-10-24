package com.pocketmic.app.interop

import com.pocketmic.app.engine.AudioSink
import com.pocketmic.app.engine.AudioSource
import com.pocketmic.app.protocol.Protocol
import kotlin.math.PI
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * A synthetic microphone: a sine of [peak] (0..1 of full scale) in 480-sample frames, delivered in
 * real time like AudioRecord. Frame n is due n x 10 ms after start, so a late timer wake-up delays
 * a frame (and the next ones follow at once) but never changes the average rate.
 */
class ToneSource(private val peak: Double, private val frequencyHz: Double = 1000.0) : AudioSource {
    @Volatile private var running = false
    private var thread: Thread? = null

    override fun start(sink: AudioSink) {
        running = true
        thread = Thread({ deliver(sink) }, "interop-tone").apply {
            isDaemon = true
            start()
        }
    }

    override fun stop() {
        running = false
        thread?.join()
    }

    private fun deliver(sink: AudioSink) {
        val frame = ShortArray(Protocol.FRAME_SAMPLES)
        val start = System.nanoTime()
        var produced = 0L
        while (running) {
            val wait = start + produced * FRAME_NANOS - System.nanoTime()
            if (wait > 0) {
                Thread.sleep((wait / 1_000_000).coerceAtLeast(1))
                continue
            }
            val first = produced * Protocol.FRAME_SAMPLES
            for (i in frame.indices) {
                val phase = 2 * PI * frequencyHz * (first + i) / Protocol.SAMPLE_RATE
                frame[i] = (peak * Short.MAX_VALUE * sin(phase)).roundToInt().toShort()
            }
            sink.onFrame(frame)
            produced++
        }
    }

    private companion object {
        const val FRAME_NANOS = 1_000_000_000L * Protocol.FRAME_SAMPLES / Protocol.SAMPLE_RATE
    }
}
