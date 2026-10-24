package com.pocketmic.app.engine

import com.pocketmic.app.protocol.Accept
import com.pocketmic.app.protocol.Announce
import com.pocketmic.app.protocol.Bye
import com.pocketmic.app.protocol.Connect
import com.pocketmic.app.protocol.Discover
import com.pocketmic.app.protocol.Keepalive
import com.pocketmic.app.protocol.Message
import com.pocketmic.app.protocol.Protocol
import com.pocketmic.app.protocol.Reject
import com.pocketmic.app.protocol.Seq
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.max

data class EngineConfig(
    val phoneId: Long,
    val phoneName: String,
    /** UDP port to listen on; 0 picks a free port once and keeps it across rebinds. */
    val port: Int = Protocol.DEFAULT_PORT,
    /** Local address to bind; null binds all interfaces, which is needed to hear broadcasts. */
    val bindAddress: InetAddress? = null,
    val sessionTimeoutMs: Long = StreamEngine.SESSION_TIMEOUT_MS,
)

enum class EngineState {
    /** The socket is not open yet, or is being reopened after an error. */
    STARTING,
    /** Listening and announcing READY; no PC connected. */
    WAITING,
    STREAMING,
    STOPPED,
}

data class EngineSnapshot(
    val state: EngineState,
    /** Port the socket is bound to, or 0 before the first successful bind. */
    val port: Int,
    val pcName: String?,
    val pcAddress: String?,
    val mic: MicState,
    /** Why the socket is not usable right now, or null when it is. */
    val networkError: String?,
    val muted: Boolean,
    /** AUDIO packets sent in the current session. */
    val packetsSent: Long,
    /** PC-reported stats from the latest KEEPALIVE; null until one arrives. */
    val lossPermille: Int?,
    val bufferMs: Int?,
    /** Smoothed peak level of what is being sent, 0..1 (0 while muted). */
    val level: Float,
)

/**
 * The phone side of PROTOCOL.md "Behaviour > Phone": answers DISCOVER, manages the single
 * CONNECT session and streams AUDIO from [source] to the connected PC. Every reply and every
 * AUDIO packet leaves from the listening socket towards the exact address the PC sent from.
 *
 * One network thread owns receiving; the source's thread sends AUDIO through [onFrame]. All
 * session and socket state is guarded by [lock], so ACCEPT always precedes a session's first AUDIO
 * and BYE is the last packet a stopped session sees. Sends never block (see [Transport]), so the
 * lock is only held briefly even while the Wi-Fi link stalls, and [snapshot] is safe to call from
 * the main thread. [listener] is called from engine threads on discrete changes (bind, session
 * start/end, mic state, mute, stop); fast-moving stats are read with [snapshot].
 */
class StreamEngine(
    private val config: EngineConfig,
    private val source: AudioSource,
    private val opener: TransportOpener = TransportOpener { UdpTransport.open(it) },
) : AudioSink {

    private class Session(
        val sessionId: Long,
        val pcId: Long,
        val pcName: String,
        val address: SocketAddress,
        var lastKeepaliveNanos: Long,
    ) {
        var nextSeq = 0L
        var packetsSent = 0L
        var lossPermille: Int? = null
        var bufferMs: Int? = null
    }

    private val lock = Any()
    private var transport: Transport? = null
    private var boundPort = 0
    private var session: Session? = null
    private var micState = MicState.LIVE
    private var networkError: String? = null
    private var rebindRequested = false
    private var stopped = false
    private var stateChanged = false
    private val audioBuffer = ByteArray(Protocol.AUDIO_PACKET_SIZE)

    @Volatile private var muted = false
    @Volatile private var level = 0f

    @Volatile var listener: ((EngineSnapshot) -> Unit)? = null

    private val timeoutNanos = TimeUnit.MILLISECONDS.toNanos(config.sessionTimeoutMs)
    private val stopSignal = CountDownLatch(1)
    private var thread: Thread? = null

    private val isStopping: Boolean get() = stopSignal.count == 0L

    /** Starts the audio source and the network thread. An engine can be started once. */
    fun start() {
        check(thread == null && !isStopping) { "StreamEngine can only be started once" }
        source.start(this)
        thread = Thread(::runNetwork, "PocketMic-network").apply { start() }
    }

    /** Stops audio, says BYE to the connected PC (if any) and closes the socket. Idempotent. */
    fun stop() {
        if (isStopping) return
        source.stop()
        stopSignal.countDown()
        val networkThread = thread
        if (networkThread != null) {
            networkThread.join(STOP_JOIN_MS)
        } else {
            synchronized(lock) {
                stopped = true
                stateChanged = true
            }
            dispatchChanges()
        }
    }

    /**
     * Reopens the socket on the same port within one receive poll, keeping the session. A socket
     * stays on the network it was opened on, so this is needed after the phone's network changes.
     */
    fun rebind() {
        synchronized(lock) { rebindRequested = true }
    }

    fun setMuted(muted: Boolean) {
        if (this.muted == muted) return
        this.muted = muted
        listener?.invoke(snapshot())
    }

    fun snapshot(): EngineSnapshot = synchronized(lock) {
        val current = session
        EngineSnapshot(
            state = when {
                stopped -> EngineState.STOPPED
                current != null -> EngineState.STREAMING
                transport != null -> EngineState.WAITING
                else -> EngineState.STARTING
            },
            port = boundPort,
            pcName = current?.pcName,
            pcAddress = (current?.address as? InetSocketAddress)
                ?.let { "${it.address.hostAddress}:${it.port}" },
            mic = micState,
            networkError = networkError,
            muted = muted,
            packetsSent = current?.packetsSent ?: 0,
            lossPermille = current?.lossPermille,
            bufferMs = current?.bufferMs,
            level = level,
        )
    }

    override fun onFrame(frame: ShortArray) {
        require(frame.size == Protocol.FRAME_SAMPLES) { "frame has ${frame.size} samples" }
        val muted = muted
        level = max(if (muted) 0f else peakOf(frame), level * LEVEL_DECAY_PER_FRAME)
        synchronized(lock) {
            val current = session ?: return
            val out = transport ?: return
            val length = Protocol.encodeAudio(audioBuffer, current.sessionId, current.nextSeq, muted, frame)
            // seq advances even if the packet is dropped: it counts 10 ms of time, so the PC sees a gap.
            current.nextSeq = Seq.next(current.nextSeq)
            try {
                // A full send buffer means the link is stalled; this audio would only arrive late.
                if (out.send(audioBuffer, length, current.address)) current.packetsSent++
            } catch (_: IOException) {
                // Typically a brief Wi-Fi outage; a broken socket also fails receive(), which rebinds.
            }
        }
    }

    override fun onMicStateChanged(state: MicState) {
        synchronized(lock) {
            if (micState == state) return
            micState = state
            stateChanged = true
        }
        if (state != MicState.LIVE) level = 0f
        dispatchChanges()
    }

    private fun runNetwork() {
        val buffer = ByteArray(Protocol.MAX_DATAGRAM_SIZE)
        var backoffMs = MIN_BACKOFF_MS
        while (!isStopping) {
            val current = synchronized(lock) { transport.takeUnless { rebindRequested } } ?: bind()
            if (current == null) {
                if (awaitStop(backoffMs)) break
                backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
                continue
            }
            try {
                val datagram = current.receive(buffer, RECEIVE_POLL_MS)
                backoffMs = MIN_BACKOFF_MS
                if (datagram != null) handle(buffer, datagram.length, datagram.from)
            } catch (e: IOException) {
                if (isStopping) break
                synchronized(lock) {
                    current.close()
                    transport = null
                    networkError = describe(e)
                    stateChanged = true
                }
                dispatchChanges()
                if (awaitStop(backoffMs)) break
                backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
            }
            synchronized(lock) { liveSession(System.nanoTime()) }
            dispatchChanges()
        }
        shutdown()
    }

    /**
     * Opens the socket on the configured port (or the one picked by the first bind), first closing
     * the one a [rebind] replaces, since it holds that port.
     */
    private fun bind(): Transport? {
        val replaced = synchronized(lock) {
            rebindRequested = false
            transport
        }
        replaced?.close()
        val port = if (config.port != 0) config.port else boundPort
        val opened = try {
            opener.open(InetSocketAddress(config.bindAddress, port))
        } catch (e: IOException) {
            synchronized(lock) {
                val error = "UDP port $port: ${describe(e)}"
                if (transport != null || networkError != error) stateChanged = true
                transport = null
                networkError = error
            }
            dispatchChanges()
            return null
        }
        synchronized(lock) {
            transport = opened
            boundPort = opened.localPort
            networkError = null
            stateChanged = true
        }
        dispatchChanges()
        return opened
    }

    private fun handle(data: ByteArray, length: Int, from: SocketAddress) {
        val header = Protocol.decodeHeader(data, 0, length) ?: return
        synchronized(lock) {
            if (header.version != Protocol.VERSION) {
                if (header.type == Protocol.Type.CONNECT) {
                    val sessionId = Protocol.sessionIdAfterHeader(data, 0, length)
                    send(Reject(sessionId, Protocol.RejectReason.BAD_VERSION), from)
                }
                return
            }
            when (val message = Protocol.decode(data, 0, length)) {
                is Discover -> onDiscover(message, from)
                is Connect -> onConnect(message, from)
                is Keepalive -> onKeepalive(message, from)
                is Bye -> onBye(message)
                else -> Unit // malformed, or a phone-to-PC message type
            }
        }
    }

    // The on* handlers below run on the network thread with [lock] held.

    private fun onDiscover(message: Discover, from: SocketAddress) {
        val current = liveSession(System.nanoTime())
        val state = when {
            current == null -> Protocol.PhoneState.READY
            current.pcId == message.pcId -> Protocol.PhoneState.STREAMING
            else -> Protocol.PhoneState.BUSY
        }
        send(Announce(state, Protocol.SAMPLE_RATE, Protocol.CHANNELS, config.phoneId, config.phoneName), from)
    }

    private fun onConnect(message: Connect, from: SocketAddress) {
        if (message.sessionId == 0L) return
        if (micState == MicState.UNAVAILABLE) {
            send(Reject(message.sessionId, Protocol.RejectReason.MIC_UNAVAILABLE), from)
            return
        }
        val now = System.nanoTime()
        val current = liveSession(now)
        when {
            current != null && current.pcId != message.pcId ->
                send(Reject(message.sessionId, Protocol.RejectReason.BUSY), from)
            current != null && current.sessionId == message.sessionId ->
                send(acceptFor(message.sessionId), from)
            else -> {
                // READY, or the same PC reconnecting (perhaps from a new port): (re)start the session.
                send(acceptFor(message.sessionId), from)
                session = Session(message.sessionId, message.pcId, message.pcName, from, now)
                stateChanged = true
            }
        }
    }

    private fun onKeepalive(message: Keepalive, from: SocketAddress) {
        val now = System.nanoTime()
        val current = liveSession(now)
        if (current != null && current.sessionId == message.sessionId) {
            current.lastKeepaliveNanos = now
            current.lossPermille = message.lossPermille
            current.bufferMs = message.bufferMs
        } else {
            send(Bye(message.sessionId), from)
        }
    }

    private fun onBye(message: Bye) {
        if (session?.sessionId == message.sessionId) {
            session = null
            stateChanged = true
        }
    }

    /** The current session, ending it first if its PC has gone quiet for too long. */
    private fun liveSession(now: Long): Session? {
        val current = session ?: return null
        if (now - current.lastKeepaliveNanos <= timeoutNanos) return current
        session = null
        stateChanged = true
        return null
    }

    private fun acceptFor(sessionId: Long) =
        Accept(sessionId, Protocol.SAMPLE_RATE, Protocol.CHANNELS, Protocol.FRAME_SAMPLES)

    private fun send(message: Message, to: SocketAddress) {
        val bytes = message.encode()
        try {
            transport?.send(bytes, bytes.size, to)
        } catch (_: IOException) {
            // Replies are best effort; the PC retries CONNECT and DISCOVER on its own.
        }
    }

    private fun shutdown() {
        synchronized(lock) {
            session?.let { send(Bye(it.sessionId), it.address) }
            session = null
            transport?.close()
            transport = null
            stopped = true
            stateChanged = true
        }
        dispatchChanges()
    }

    private fun dispatchChanges() {
        val changed = synchronized(lock) { stateChanged.also { stateChanged = false } }
        if (changed) listener?.invoke(snapshot())
    }

    /** Waits up to [ms]; true if [stop] was called meanwhile. */
    private fun awaitStop(ms: Long): Boolean = stopSignal.await(ms, TimeUnit.MILLISECONDS)

    private fun describe(e: IOException): String = e.message ?: e.javaClass.simpleName

    private fun peakOf(frame: ShortArray): Float {
        var peak = 0
        for (sample in frame) peak = max(peak, abs(sample.toInt()))
        return peak / 32768f
    }

    companion object {
        const val SESSION_TIMEOUT_MS = 3_000L
        private const val RECEIVE_POLL_MS = 100L
        private const val MIN_BACKOFF_MS = 250L
        private const val MAX_BACKOFF_MS = 5_000L
        private const val STOP_JOIN_MS = 2_000L

        /** Per 10 ms frame: the meter falls about 9 dB per 100 ms after a peak. */
        private const val LEVEL_DECAY_PER_FRAME = 0.9f
    }
}
