package com.pocketmic.app.engine

import com.pocketmic.app.protocol.Accept
import com.pocketmic.app.protocol.Announce
import com.pocketmic.app.protocol.Audio
import com.pocketmic.app.protocol.Bye
import com.pocketmic.app.protocol.Connect
import com.pocketmic.app.protocol.Discover
import com.pocketmic.app.protocol.Keepalive
import com.pocketmic.app.protocol.Message
import com.pocketmic.app.protocol.Protocol
import com.pocketmic.app.protocol.Reject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.BindException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketAddress
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Drives [StreamEngine] over real UDP on 127.0.0.1 with ephemeral ports. Binding only to
 * loopback keeps the tests from tripping a firewall prompt and from touching port 50505.
 */
class StreamEngineTest {

    private val loopback: InetAddress = InetAddress.getLoopbackAddress()
    private val engines = mutableListOf<StreamEngine>()
    private val sockets = mutableListOf<DatagramSocket>()

    @After
    fun tearDown() {
        engines.forEach { it.stop() }
        sockets.forEach { it.close() }
    }

    // --- Discovery --------------------------------------------------------------------------

    @Test
    fun discoverIsAnsweredWithReadyAnnounceFromTheListeningPort() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 0x11223344)
        pc.send(Discover(0x11223344, "Desk"))
        assertEquals(
            Announce(Protocol.PhoneState.READY, 48000, 1, PHONE_ID, PHONE_NAME),
            pc.expect<Announce>(),
        )
    }

    @Test
    fun announceReportsStreamingToTheConnectedPcAndBusyToOthers() {
        val engine = startEngine()
        val pcA = FakePc(engine, pcId = 1)
        val pcB = FakePc(engine, pcId = 2)
        pcA.connect(sessionId = 100)

        pcA.send(Discover(1, "A"))
        assertEquals(Protocol.PhoneState.STREAMING, pcA.expect<Announce>().state)
        pcB.send(Discover(2, "B"))
        assertEquals(Protocol.PhoneState.BUSY, pcB.expect<Announce>().state)
    }

    // --- CONNECT ----------------------------------------------------------------------------

    @Test
    fun connectIsAcceptedAndAudioFlowsToTheSenderWithSeqFromZero() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 7)

        assertEquals(Accept(0xDEADBEEF, 48000, 1, 480), pc.connect(0xDEADBEEF))
        val frames = (0 until 3).map { n -> ShortArray(Protocol.FRAME_SAMPLES) { (it * (n + 1)).toShort() } }
        frames.forEach(source::push)

        frames.forEachIndexed { index, frame ->
            val audio = pc.expect<Audio>()
            assertEquals(0xDEADBEEF, audio.sessionId)
            assertEquals(index.toLong(), audio.seq)
            assertFalse(audio.muted)
            assertArrayEquals(frame, audio.samples)
        }
        val snapshot = engine.snapshot()
        assertEquals(EngineState.STREAMING, snapshot.state)
        assertEquals("PC-7", snapshot.pcName)
        assertEquals("${loopback.hostAddress}:${pc.port}", snapshot.pcAddress)
        assertEquals(3L, snapshot.packetsSent)
    }

    @Test
    fun noAudioIsSentWithoutASessionButTheLevelMeterStillWorks() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 1)
        pc.send(Discover(1, "A"))
        pc.expect<Announce>()

        repeat(5) { source.push(ShortArray(Protocol.FRAME_SAMPLES) { 16384 }) }
        pc.expectSilence()
        assertEquals(0.5f, engine.snapshot().level, 0.01f)
        assertEquals(0L, engine.snapshot().packetsSent)
    }

    @Test
    fun duplicateConnectResendsAcceptWithoutRestartingTheSession() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 9)
        pc.connect(sessionId = 55)
        source.push()
        source.push()
        assertEquals(0L, pc.expect<Audio>().seq)
        assertEquals(1L, pc.expect<Audio>().seq)

        assertEquals(55L, pc.connect(sessionId = 55).sessionId)
        source.push()
        val audio = pc.expect<Audio>()
        assertEquals(55L, audio.sessionId)
        assertEquals(2L, audio.seq)
        assertEquals(3L, engine.snapshot().packetsSent)
    }

    @Test
    fun samePcReconnectingFromANewPortTakesOverTheSession() {
        val source = ManualSource()
        val engine = startEngine(source)
        val oldSocket = FakePc(engine, pcId = 42)
        oldSocket.connect(sessionId = 1)
        source.push()
        assertEquals(0L, oldSocket.expect<Audio>().seq)

        val newSocket = FakePc(engine, pcId = 42)
        assertEquals(2L, newSocket.connect(sessionId = 2).sessionId)
        source.push()
        source.push()
        val first = newSocket.expect<Audio>()
        assertEquals(2L, first.sessionId)
        assertEquals(0L, first.seq)
        assertEquals(1L, newSocket.expect<Audio>().seq)
        oldSocket.expectSilence()
        assertEquals("${loopback.hostAddress}:${newSocket.port}", engine.snapshot().pcAddress)
    }

    @Test
    fun anotherPcIsRejectedBusyWhileTheSessionIsAlive() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pcA = FakePc(engine, pcId = 1)
        val pcB = FakePc(engine, pcId = 2)
        pcA.connect(sessionId = 10)

        pcB.send(Connect(20, 2, "B"))
        assertEquals(Reject(20, Protocol.RejectReason.BUSY), pcB.expect<Reject>())

        source.push()
        assertEquals(10L, pcA.expect<Audio>().sessionId)
        pcB.expectSilence()
        assertEquals("PC-1", engine.snapshot().pcName)
    }

    @Test
    fun anotherPcIsAcceptedOnceTheOldSessionHasTimedOut() {
        val source = ManualSource()
        val engine = startEngine(source, sessionTimeoutMs = 300)
        val pcA = FakePc(engine, pcId = 1)
        val pcB = FakePc(engine, pcId = 2)
        pcA.connect(sessionId = 10)

        Thread.sleep(600)
        assertEquals(20L, pcB.connect(sessionId = 20).sessionId)
        source.push()
        assertEquals(20L, pcB.expect<Audio>().sessionId)
        pcA.expectSilence()
    }

    @Test
    fun connectIsRejectedWhileTheMicIsUnavailableAndAcceptedOnceItRecovers() {
        val source = ManualSource(initialMicState = MicState.UNAVAILABLE)
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 3)
        assertEquals(MicState.UNAVAILABLE, engine.snapshot().mic)

        pc.send(Connect(77, 3, "PC"))
        assertEquals(Reject(77, Protocol.RejectReason.MIC_UNAVAILABLE), pc.expect<Reject>())
        pc.send(Discover(3, "PC"))
        assertEquals(Protocol.PhoneState.READY, pc.expect<Announce>().state)
        assertEquals(EngineState.WAITING, engine.snapshot().state)

        source.sink.onMicStateChanged(MicState.LIVE)
        assertEquals(78L, pc.connect(sessionId = 78).sessionId)
    }

    @Test
    fun aSilencedMicIsReportedButStillAcceptsConnects() {
        val source = ManualSource()
        val engine = startEngine(source)
        val states = CopyOnWriteArrayList<MicState>()
        engine.listener = { states += it.mic }
        val pc = FakePc(engine, pcId = 3)

        source.sink.onMicStateChanged(MicState.SILENCED)
        assertEquals(MicState.SILENCED, engine.snapshot().mic)
        assertEquals(listOf(MicState.SILENCED), states.toList())
        assertEquals(79L, pc.connect(sessionId = 79).sessionId)
        source.push(ShortArray(Protocol.FRAME_SAMPLES))
        assertEquals(0L, pc.expect<Audio>().seq)

        source.sink.onMicStateChanged(MicState.LIVE)
        assertEquals(MicState.LIVE, engine.snapshot().mic)
    }

    @Test
    fun connectFromAnotherProtocolVersionIsRejectedBadVersion() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 0x11223344)
        val v2Connect = Connect(0xDEADBEEF, 0x11223344, "Desk").encode().also { it[4] = 2 }
        pc.sendRaw(v2Connect)
        assertEquals(Reject(0xDEADBEEF, Protocol.RejectReason.BAD_VERSION), pc.expect<Reject>())
        assertEquals(EngineState.WAITING, engine.snapshot().state)

        val v2Discover = Discover(0x11223344, "Desk").encode().also { it[4] = 2 }
        pc.sendRaw(v2Discover)
        pc.expectSilence()
    }

    @Test
    fun connectWithSessionIdZeroIsIgnored() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 1)
        pc.send(Connect(0, 1, "PC"))
        pc.expectSilence()
        assertEquals(EngineState.WAITING, engine.snapshot().state)
    }

    // --- KEEPALIVE, BYE and timeout -----------------------------------------------------------

    @Test
    fun keepalivesKeepTheSessionAliveAndUpdateStats() {
        val source = ManualSource()
        val engine = startEngine(source, sessionTimeoutMs = 500)
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 900)
        assertNull(engine.snapshot().lossPermille)

        repeat(15) {
            pc.send(Keepalive(900, 0, 5, 60))
            Thread.sleep(100)
        }
        val snapshot = engine.snapshot()
        assertEquals(EngineState.STREAMING, snapshot.state)
        assertEquals(5, snapshot.lossPermille)
        assertEquals(60, snapshot.bufferMs)
        pc.expectSilence(100)
        source.push()
        assertEquals(900L, pc.expect<Audio>().sessionId)
    }

    @Test
    fun keepaliveForAnUnknownSessionIsAnsweredWithBye() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 5)
        pc.send(Keepalive(1234, 0, 0, 0))
        assertEquals(Bye(1234), pc.expect<Bye>())

        pc.connect(sessionId = 1)
        val stalePc = FakePc(engine, pcId = 6)
        stalePc.send(Keepalive(999, 0, 0, 0))
        assertEquals(Bye(999), stalePc.expect<Bye>())
        assertEquals(EngineState.STREAMING, engine.snapshot().state)
        pc.expectSilence(100)
    }

    @Test
    fun byeForTheCurrentSessionEndsIt() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 31)

        pc.send(Bye(30))
        pc.expectSilence(100)
        assertEquals(EngineState.STREAMING, engine.snapshot().state)

        pc.send(Bye(31))
        awaitTrue("session to end") { engine.snapshot().state == EngineState.WAITING }
        source.push()
        pc.expectSilence(100)
        pc.send(Discover(5, "PC"))
        assertEquals(Protocol.PhoneState.READY, pc.expect<Announce>().state)
    }

    @Test
    fun sessionEndsWhenKeepalivesStop() {
        val engine = startEngine(sessionTimeoutMs = 300)
        val states = CopyOnWriteArrayList<EngineState>()
        engine.listener = { states += it.state }
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 31)

        awaitTrue("session to time out") { engine.snapshot().state == EngineState.WAITING }
        assertEquals(listOf(EngineState.STREAMING, EngineState.WAITING), states.toList())
        pc.send(Keepalive(31, 0, 0, 0))
        assertEquals(Bye(31), pc.expect<Bye>())
    }

    @Test
    fun defaultSessionTimeoutIsThreeSeconds() {
        assertEquals(3_000L, EngineConfig(1, "x").sessionTimeoutMs)
        val engine = startEngine(sessionTimeoutMs = EngineConfig(1, "x").sessionTimeoutMs)
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 31)
        val connectedAt = System.nanoTime()

        Thread.sleep(2_500)
        assertEquals(EngineState.STREAMING, engine.snapshot().state)
        awaitTrue("default timeout", timeoutMs = 2_000) { engine.snapshot().state == EngineState.WAITING }
        val elapsedMs = (System.nanoTime() - connectedAt) / 1_000_000
        assertTrue("ended after $elapsedMs ms", elapsedMs in 2_900..3_800)
    }

    // --- Mute, stop, robustness ---------------------------------------------------------------

    @Test
    fun mutedAudioIsFlaggedSilentAndKeepsItsRate() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 8)

        engine.setMuted(true)
        source.push(ShortArray(Protocol.FRAME_SAMPLES) { 12345 })
        source.push(ShortArray(Protocol.FRAME_SAMPLES) { -12345 })
        repeat(2) { index ->
            val audio = pc.expect<Audio>()
            assertTrue(audio.muted)
            assertEquals(index.toLong(), audio.seq)
            assertArrayEquals(ShortArray(Protocol.FRAME_SAMPLES), audio.samples)
        }
        assertTrue(engine.snapshot().muted)
        assertEquals(0f, engine.snapshot().level, 0f)

        engine.setMuted(false)
        source.push(ShortArray(Protocol.FRAME_SAMPLES) { 7 })
        val audio = pc.expect<Audio>()
        assertFalse(audio.muted)
        assertEquals(7.toShort(), audio.samples[0])
    }

    @Test
    fun stopSaysByeToTheConnectedPcThenClosesTheSocket() {
        val source = ManualSource()
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 0xCAFE)
        val port = engine.snapshot().port

        engine.stop()
        assertEquals(Bye(0xCAFE), pc.expect<Bye>())
        assertTrue(source.stopped)
        assertEquals(EngineState.STOPPED, engine.snapshot().state)

        pc.sendRaw(Discover(5, "PC").encode(), port)
        pc.expectSilence()
        engine.stop()
    }

    @Test
    fun stopWithoutASessionSendsNothing() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 5)
        pc.send(Discover(5, "PC"))
        pc.expect<Announce>()
        engine.stop()
        pc.expectSilence()
        assertEquals(EngineState.STOPPED, engine.snapshot().state)
    }

    @Test
    fun malformedAndPhoneToPcPacketsAreIgnored() {
        val engine = startEngine()
        val pc = FakePc(engine, pcId = 5)
        val junk = listOf(
            ByteArray(0),
            byteArrayOf(0x50, 0x4b),
            "PKMX".toByteArray() + ByteArray(8),
            Protocol.MAGIC + byteArrayOf(1, 0x7f, 0, 0),
            Discover(5, "PC").encode().copyOf(10),
            Connect(1, 5, "PC").encode() + byteArrayOf(0),
            Announce(0, 48000, 1, 1, "Other phone").encode(),
            Accept(1, 48000, 1, 480).encode(),
            Reject(1, 1).encode(),
            Audio(1, 0, false, ShortArray(4)).encode(),
        )
        junk.forEach { pc.sendRaw(it) }
        pc.expectSilence()

        pc.send(Discover(5, "PC"))
        assertEquals(Protocol.PhoneState.READY, pc.expect<Announce>().state)
    }

    @Test
    fun bindFailuresAreRetriedWithBackoff() {
        val opener = FlakyOpener(bindFailures = 2)
        val errors = CopyOnWriteArrayList<String>()
        val engine = createEngine(ManualSource(), opener = opener)
        engine.listener = { snapshot -> snapshot.networkError?.let(errors::add) }
        engine.start()

        awaitTrue("bind to succeed") { engine.snapshot().state == EngineState.WAITING }
        assertNull(engine.snapshot().networkError)
        assertEquals(3, opener.attempts)
        assertTrue(errors.toString(), errors.any { "simulated bind failure" in it })

        val pc = FakePc(engine, pcId = 5)
        pc.send(Discover(5, "PC"))
        pc.expect<Announce>()
    }

    @Test
    fun receiveErrorsRebindTheSamePortAndKeepTheSession() {
        val source = ManualSource()
        val opener = FlakyOpener(bindFailures = 0)
        val errors = CopyOnWriteArrayList<String>()
        val engine = createEngine(source, opener = opener)
        engine.listener = { snapshot -> snapshot.networkError?.let(errors::add) }
        engine.start()
        awaitTrue("first bind") { engine.snapshot().state == EngineState.WAITING }
        val port = engine.snapshot().port
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 64)

        opener.transports.single().failNextReceive = true
        awaitTrue("rebind") { opener.transports.size == 2 && engine.snapshot().networkError == null }
        assertTrue(errors.toString(), errors.any { "simulated receive failure" in it })
        assertEquals(port, engine.snapshot().port)
        assertTrue(opener.transports.first().closed)

        pc.send(Keepalive(64, 0, 0, 0))
        pc.expectSilence(200)
        source.push()
        val audio = pc.expect<Audio>()
        assertEquals(64L, audio.sessionId)
        assertEquals(0L, audio.seq)
    }

    @Test
    fun requestedRebindReopensTheSamePortQuietlyAndKeepsTheSession() {
        val source = ManualSource()
        val opener = FlakyOpener(bindFailures = 0)
        val errors = CopyOnWriteArrayList<String>()
        val engine = createEngine(source, opener = opener)
        engine.listener = { snapshot -> snapshot.networkError?.let(errors::add) }
        engine.start()
        awaitTrue("first bind") { engine.snapshot().state == EngineState.WAITING }
        val port = engine.snapshot().port
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 64)
        source.push()
        assertEquals(0L, pc.expect<Audio>().seq)

        engine.rebind()
        awaitTrue("rebind") { opener.transports.size == 2 }
        assertTrue(opener.transports.first().closed)
        assertEquals(port, engine.snapshot().port)
        // The new socket hears the PC, and the session and its sequence carry on.
        pc.send(Discover(5, "PC"))
        assertEquals(Protocol.PhoneState.STREAMING, pc.expect<Announce>().state)
        source.push()
        val audio = pc.expect<Audio>()
        assertEquals(64L, audio.sessionId)
        assertEquals(1L, audio.seq)
        assertTrue(errors.toString(), errors.isEmpty())
    }

    @Test
    fun audioTheSendBufferCannotTakeIsDroppedButStillCountsInSeq() {
        val source = ManualSource()
        val opener = FlakyOpener(bindFailures = 0)
        val engine = createEngine(source, opener = opener)
        engine.start()
        awaitTrue("bind") { engine.snapshot().state == EngineState.WAITING }
        val pc = FakePc(engine, pcId = 5)
        pc.connect(sessionId = 12)

        opener.transports.single().sendBufferFull = true
        repeat(3) { source.push() }
        pc.expectSilence(100)
        assertEquals(EngineState.STREAMING, engine.snapshot().state)
        assertEquals(0L, engine.snapshot().packetsSent)

        opener.transports.single().sendBufferFull = false
        source.push()
        assertEquals(3L, pc.expect<Audio>().seq)
        assertEquals(1L, engine.snapshot().packetsSent)
    }

    @Test
    fun concurrentAudioAndSessionChangesKeepPerSessionSequencesIntact() {
        val source = ClockedSource(periodMs = 2)
        val engine = startEngine(source)
        val pc = FakePc(engine, pcId = 5, receiveBufferBytes = 1 shl 20)

        for (sessionId in 1L..5L) {
            pc.send(Connect(sessionId, 5, "PC"))
            var accepted = false
            var expectedSeq = 0L
            val until = System.nanoTime() + 150_000_000L
            while (System.nanoTime() < until || !accepted) {
                when (val message = pc.receive() ?: throw AssertionError("stream stalled")) {
                    is Accept -> {
                        assertEquals(sessionId, message.sessionId)
                        accepted = true
                    }
                    is Audio -> if (accepted) {
                        assertEquals("session switched back", sessionId, message.sessionId)
                        assertEquals(expectedSeq, message.seq)
                        expectedSeq++
                    } else {
                        assertEquals(sessionId - 1, message.sessionId)
                    }
                    else -> throw AssertionError("unexpected $message")
                }
            }
            assertTrue("session $sessionId streamed only $expectedSeq packets", expectedSeq > 10)
        }

        engine.stop()
        var last: Message? = null
        while (true) last = pc.receive(300) ?: break
        assertEquals(Bye(5), last)
    }

    // --- Helpers ------------------------------------------------------------------------------

    private fun createEngine(
        source: AudioSource,
        sessionTimeoutMs: Long = 3_000,
        opener: TransportOpener = TransportOpener { UdpTransport.open(it) },
    ): StreamEngine {
        val config = EngineConfig(PHONE_ID, PHONE_NAME, port = 0, bindAddress = loopback, sessionTimeoutMs = sessionTimeoutMs)
        return StreamEngine(config, source, opener).also { engines += it }
    }

    private fun startEngine(source: AudioSource = ManualSource(), sessionTimeoutMs: Long = 3_000): StreamEngine {
        val engine = createEngine(source, sessionTimeoutMs)
        engine.start()
        awaitTrue("engine to bind") { engine.snapshot().state == EngineState.WAITING }
        return engine
    }

    private fun awaitTrue(what: String, timeoutMs: Long = 3_000, condition: () -> Boolean) {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (!condition()) {
            if (System.nanoTime() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(5)
        }
    }

    /** A PC: one UDP socket on loopback that talks to the engine's listening port. */
    private inner class FakePc(
        private val engine: StreamEngine,
        private val pcId: Long,
        receiveBufferBytes: Int = 0,
    ) {
        private val socket = DatagramSocket(InetSocketAddress(loopback, 0)).also {
            if (receiveBufferBytes > 0) it.receiveBufferSize = receiveBufferBytes
            sockets += it
        }
        private val buffer = ByteArray(Protocol.MAX_DATAGRAM_SIZE)
        val port: Int get() = socket.localPort

        fun send(message: Message) = sendRaw(message.encode())

        fun sendRaw(bytes: ByteArray, port: Int = engine.snapshot().port) {
            socket.send(DatagramPacket(bytes, bytes.size, InetSocketAddress(loopback, port)))
        }

        /** Next decoded packet, checking it came from the engine's listening socket. */
        fun receive(timeoutMs: Int = 2_000): Message? {
            socket.soTimeout = timeoutMs
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                socket.receive(packet)
            } catch (_: SocketTimeoutException) {
                return null
            }
            assertEquals("sent from the listening port", engine.snapshot().port, packet.port)
            return Protocol.decode(buffer, 0, packet.length)
                ?: throw AssertionError("engine sent an undecodable packet")
        }

        inline fun <reified T : Message> expect(): T {
            val message = receive()
            assertTrue("expected ${T::class.simpleName}, got $message", message is T)
            return message as T
        }

        fun expectSilence(ms: Int = 300) {
            val message = receive(ms)
            assertNull("unexpected $message", message)
        }

        fun connect(sessionId: Long): Accept {
            send(Connect(sessionId, pcId, "PC-$pcId"))
            return expect()
        }
    }

    private class ManualSource(private val initialMicState: MicState = MicState.LIVE) : AudioSource {
        @Volatile private var started: AudioSink? = null
        @Volatile var stopped = false
        val sink: AudioSink get() = started ?: throw AssertionError("source not started")

        override fun start(sink: AudioSink) {
            started = sink
            if (initialMicState != MicState.LIVE) sink.onMicStateChanged(initialMicState)
        }

        override fun stop() {
            stopped = true
        }

        fun push(frame: ShortArray = ShortArray(Protocol.FRAME_SAMPLES) { 1000 }) = sink.onFrame(frame)
    }

    /** Pushes a frame every [periodMs] from its own thread, like a real capture thread. */
    private class ClockedSource(private val periodMs: Long) : AudioSource {
        @Volatile private var running = false
        private var thread: Thread? = null

        override fun start(sink: AudioSink) {
            running = true
            thread = Thread {
                val frame = ShortArray(Protocol.FRAME_SAMPLES)
                var n = 0
                while (running) {
                    frame.fill((n++ % 1000).toShort())
                    sink.onFrame(frame)
                    Thread.sleep(periodMs)
                }
            }.apply { start() }
        }

        override fun stop() {
            running = false
            thread?.join()
        }
    }

    /** A real socket that tests can make fail a receive, or act as if its send buffer were full. */
    private class FlakyTransport(private val real: Transport) : Transport by real {
        @Volatile var failNextReceive = false
        @Volatile var sendBufferFull = false
        @Volatile var closed = false

        override fun send(data: ByteArray, length: Int, to: SocketAddress): Boolean =
            !sendBufferFull && real.send(data, length, to)

        override fun receive(into: ByteArray, timeoutMs: Long): Datagram? {
            if (failNextReceive) {
                failNextReceive = false
                throw SocketException("simulated receive failure")
            }
            return real.receive(into, timeoutMs)
        }

        override fun close() {
            closed = true
            real.close()
        }
    }

    private class FlakyOpener(private var bindFailures: Int) : TransportOpener {
        val transports = CopyOnWriteArrayList<FlakyTransport>()
        @Volatile var attempts = 0

        override fun open(address: InetSocketAddress): Transport {
            attempts++
            if (bindFailures > 0) {
                bindFailures--
                throw BindException("simulated bind failure")
            }
            return FlakyTransport(UdpTransport.open(address)).also { transports += it }
        }
    }

    private companion object {
        const val PHONE_ID = 0xA1B2C3D4L
        const val PHONE_NAME = "Test Phone"
    }
}
