package com.pocketmic.app.interop

import com.pocketmic.app.engine.AudioSink
import com.pocketmic.app.engine.AudioSource
import com.pocketmic.app.engine.EngineConfig
import com.pocketmic.app.engine.EngineSnapshot
import com.pocketmic.app.engine.EngineState
import com.pocketmic.app.engine.MicState
import com.pocketmic.app.engine.StreamEngine
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestName
import java.io.File
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.random.Random

/**
 * Proves the phone and the PC halves interoperate: the real [StreamEngine] runs on this JVM with a
 * synthetic 1 kHz microphone, and the real PC receiver (`python -m pocketmic --nogui --device null`)
 * runs as a child process, over UDP on an ephemeral port. Nothing is played into any audio device.
 *
 * Skipped unless POCKETMIC_INTEROP=1. The broadcast-discovery test binds 0.0.0.0, which can make
 * Windows Firewall ask about this JVM, so it also needs POCKETMIC_INTEROP_BROADCAST=1. Measurements
 * are printed as "[interop]" lines. See android/README.md for how to run it.
 */
class ReceiverInteropTest {

    @get:Rule val testName = TestName()

    private val loopback: InetAddress = InetAddress.getLoopbackAddress()
    private val python = System.getenv("POCKETMIC_PYTHON") ?: "python"
    private val pcDir = File(System.getenv("POCKETMIC_PC_DIR") ?: "../../pc").canonicalFile
    private lateinit var workDir: File
    private val engines = mutableListOf<StreamEngine>()
    private val receivers = mutableListOf<PcReceiver>()

    @Before
    fun setUp() {
        assumeTrue("set POCKETMIC_INTEROP=1 to run the PC receiver interop tests", System.getenv("POCKETMIC_INTEROP") == "1")
        assertTrue("PC receiver not found in $pcDir (set POCKETMIC_PC_DIR)", File(pcDir, "pocketmic/__main__.py").isFile)
        // Kept after the run (under build/, so `gradlew clean` removes it) for the receivers' logs.
        workDir = File("build/interop/${testName.methodName}").absoluteFile
        workDir.deleteRecursively()
        assertTrue(workDir.mkdirs())
    }

    @After
    fun tearDown() {
        receivers.forEach { it.destroy() }
        engines.forEach { it.stop() }
    }

    @Test
    fun streamsWithoutLossReportsStatsHonoursMuteAndSaysByeOnExit() {
        val phone = startPhone()
        val first = receiver("first", "settings-a")
        first.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "28")
        val connected = phone.events.await(15_000, "the first receiver to connect") { it.state == EngineState.STREAMING }
        report("connect: CONNECT -> ACCEPT -> streaming %d ms after the receiver process started",
            ms(connected.nanos - first.startedNanos))
        val address = connected.snapshot.pcAddress
        assertTrue("pc_name ${connected.snapshot.pcName} from CONNECT",
            first.settingsFile.readText().contains("\"pc_name\": \"${connected.snapshot.pcName}\""))

        // Prefill done: from here on there must be no underruns.
        val start = first.awaitStats(5_000, "playback to start") { it.flag("playing") }

        // While the first receiver streams, a PC with another pc_id must be turned away.
        val second = receiver("second", "settings-b")
        second.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "3")
        second.awaitExit(15_000)
        val rejected = checkNotNull(second.stats())
        assertEquals("second receiver's notice", "busy", rejected.text("notice"))
        assertEquals("searching", rejected.text("link"))
        assertTrue("second receiver lists the phone as BUSY", rejected.json.contains("\"state\": \"BUSY\""))
        report("second PC (other pc_id) while streaming: REJECT BUSY (notice=%s, phone listed BUSY)", rejected.text("notice"))

        val end = first.awaitStats(20_000, "12 s of streaming") { it.time >= start.time + 12 }
        val seconds = end.time - start.time
        val received = end.number("received") - start.number("received")
        val underruns = end.number("underruns") - start.number("underruns")
        val concealed = end.number("concealed") - start.number("concealed")
        report("streaming over %.1f s: %.0f packets (%.1f/s), loss %.0f permille, underruns +%.0f, " +
            "concealed +%.0f, late %.0f, duplicates %.0f, resyncs +%.0f, buffer %.1f ms (target %.0f), drift %+.0f ppm",
            seconds, received, received / seconds, end.number("loss_permille"), underruns, concealed,
            end.number("late"), end.number("duplicates"), end.number("resyncs") - start.number("resyncs"),
            end.number("buffer_ms"), end.number("target_ms"), end.number("drift_ppm"))
        assertTrue("streamed at least 10 s", seconds >= 10)
        assertEquals("packets per second", 100.0, received / seconds, 2.0)
        assertTrue("loss ~0", end.number("loss_permille") <= 1)
        assertEquals("underruns after prefill", 0.0, underruns, 0.0)
        assertEquals("concealed (lost) frames", 0.0, concealed, 1.0)
        assertEquals("output level of the -20 dBFS tone", TONE_PEAK, end.number("output_peak"), 0.005)

        // KEEPALIVE carries the receiver's loss and buffer to the phone.
        val engineView = phone.engine.snapshot()
        val pcView = checkNotNull(first.stats())
        report("KEEPALIVE stats on the engine: loss %s permille, buffer %s ms (receiver: loss %.0f, buffer %.1f ms); packets sent %d",
            engineView.lossPermille, engineView.bufferMs, pcView.number("loss_permille"), pcView.number("buffer_ms"),
            engineView.packetsSent)
        assertNotNull(engineView.lossPermille)
        assertNotNull(engineView.bufferMs)
        assertEquals(pcView.number("loss_permille"), engineView.lossPermille!!.toDouble(), 1.0)
        assertEquals(pcView.number("buffer_ms"), engineView.bufferMs!!.toDouble(), 30.0)

        // Muting on the phone flags every AUDIO packet; the receiver plays silence.
        val mutedAt = System.nanoTime()
        phone.engine.setMuted(true)
        val flagged = first.awaitStats(3_000, "the MUTED flag") { it.flag("phone_muted") }
        val flaggedMs = ms(System.nanoTime() - mutedAt)
        val silent = first.awaitStats(5_000, "a whole second of muted output") { it.time >= flagged.time + MUTE_SETTLE_S }
        report("mute: MUTED flag in the receiver's stats %d ms after setMuted(true) (stats are written every second); " +
            "output peak while muted %.5f", flaggedMs, silent.number("output_peak"))
        assertTrue(silent.flag("phone_muted"))
        assertEquals("muted audio keeps the session alive", "connected", silent.text("link"))
        assertEquals("muted output", 0.0, silent.number("output_peak"), 1e-4)

        phone.engine.setMuted(false)
        val unflagged = first.awaitStats(3_000, "the MUTED flag to clear") { !it.flag("phone_muted") }
        val loud = first.awaitStats(5_000, "a whole second of unmuted output") { it.time >= unflagged.time + MUTE_SETTLE_S }
        report("unmute: output peak %.4f (expected %.4f)", loud.number("output_peak"), TONE_PEAK)
        assertEquals(TONE_PEAK, loud.number("output_peak"), 0.005)
        assertEquals("underruns over the whole run", 0.0, loud.number("underruns"), 0.0)

        // Quitting sends BYE: the phone is READY again at once, not after its 3 s timeout.
        first.awaitExit(20_000)
        val ready = phone.events.await(5_000, "READY after the receiver quit", after = connected.nanos) { it.state == EngineState.WAITING }
        val stopped = checkNotNull(first.stats())
        assertTrue(stopped.flag("stopped"))
        report("receiver quit: engine READY %+d ms relative to the receiver process exiting (BYE, not timeout)",
            ms(ready.nanos - first.exitedNanos))
        assertTrue("READY came from BYE, before any timeout could fire", ready.nanos - first.exitedNanos < 500_000_000)
        assertTrue("one session from connect to BYE, through the BUSY reject and the mute",
            phone.events.since(connected.nanos).takeWhile { it !== ready }
                .all { it.snapshot.state == EngineState.STREAMING && it.pcAddress == address })

        // Started again, it finds the phone it used last (unicast DISCOVER to the remembered IP).
        val again = receiver("first-again", "settings-a")
        again.start(phone.port, "--duration", "4")
        val reconnected = phone.events.await(15_000, "the restarted receiver to reconnect", after = ready.nanos) {
            it.state == EngineState.STREAMING
        }
        report("restart without --phone-ip: reconnected %d ms after the process started (remembered phone)",
            ms(reconnected.nanos - again.startedNanos))
        again.awaitStats(5_000, "audio after the restart") { it.flag("playing") }
        again.awaitExit(20_000)
        phone.events.await(5_000, "READY after the restarted receiver quit", after = reconnected.nanos) { it.state == EngineState.WAITING }
    }

    @Test
    fun restartedReceiverTakesOverItsSessionFromANewPort() {
        // Longer than the phone's 3 s, so a slow Python start cannot turn the takeover into an
        // ordinary connect to a READY phone.
        val phone = startPhone(sessionTimeoutMs = 10_000)
        val crashed = receiver("crashed", "settings-a")
        crashed.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "60")
        val first = phone.events.await(15_000, "the receiver to connect") { it.state == EngineState.STREAMING }
        crashed.awaitStats(5_000, "playback to start") { it.flag("playing") }
        val killedAt = crashed.kill()

        val restarted = receiver("restarted", "settings-a")
        restarted.start(phone.port, "--duration", "5")
        val takeover = phone.events.await(15_000, "the same pc_id to take over", after = killedAt) {
            it.state == EngineState.STREAMING && it.pcAddress != first.snapshot.pcAddress
        }
        assertTrue("the session never ended in between",
            phone.events.since(killedAt).takeWhile { it !== takeover }.none { it.snapshot.state != EngineState.STREAMING })
        val stats = restarted.awaitStats(5_000, "audio on the new session") { it.flag("playing") }
        report("takeover: same pc_id from port %s replaced the session to %s %d ms after the kill, without a timeout",
            takeover.pcAddress, first.snapshot.pcAddress, ms(takeover.nanos - killedAt))
        assertEquals("127.0.0.1:${stats.number("pc_port").toInt()}", takeover.pcAddress)
        assertEquals("connected", stats.text("link"))
        restarted.awaitExit(20_000)
        phone.events.await(5_000, "READY after the receiver quit", after = takeover.nanos) { it.state == EngineState.WAITING }
    }

    @Test
    fun killedReceiverSessionEndsAfterTheThreeSecondTimeout() {
        val phone = startPhone()
        val receiver = receiver("killed", "settings-a")
        receiver.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "60")
        val connected = phone.events.await(15_000, "the receiver to connect") { it.state == EngineState.STREAMING }
        receiver.awaitStats(5_000, "playback to start") { it.flag("playing") }
        val killedAt = receiver.kill()

        val ready = phone.events.await(6_000, "the session timeout", after = killedAt) { it.state == EngineState.WAITING }
        val delayMs = ms(ready.nanos - killedAt)
        report("receiver killed (no BYE): engine READY %d ms later (timeout %d ms after the last KEEPALIVE)",
            delayMs, StreamEngine.SESSION_TIMEOUT_MS)
        // The last KEEPALIVE left up to 500 ms before the kill; the engine checks every 100 ms.
        assertTrue("READY after $delayMs ms", delayMs in 2_400..3_600)
        assertTrue(phone.events.since(connected.nanos).takeWhile { it !== ready }.all { it.snapshot.state == EngineState.STREAMING })
        assertNull("the socket survived sending to a dead port", phone.engine.snapshot().networkError)
        assertEquals(phone.port, phone.engine.snapshot().port)
    }

    @Test
    fun phoneSwitchedOffSaysByeAndTheReceiverReconnectsWhenItIsBackOn() {
        val phone = startPhone()
        val receiver = receiver("waiting", "settings-a")
        receiver.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "8")
        phone.events.await(15_000, "the receiver to connect") { it.state == EngineState.STREAMING }
        val before = receiver.awaitStats(5_000, "playback to start") { it.flag("playing") }

        phone.engine.stop()
        val ended = receiver.awaitStats(3_000, "the phone's BYE") { it.text("link") == "searching" }
        assertEquals("phone_ended", ended.text("notice"))

        // Switched on again, the app listens on the same port with the same phone_id.
        val switchedOnAt = System.nanoTime()
        val back = startPhone(port = phone.port, phoneId = phone.phoneId)
        val reconnected = back.events.await(10_000, "the receiver to reconnect") { it.state == EngineState.STREAMING }
        val stats = receiver.awaitStats(5_000, "audio from the phone again") {
            it.flag("playing") && it.number("session_id") != before.number("session_id")
        }
        report("phone off: receiver notice %s; phone on again: streaming %d ms later " +
            "(the receiver sends DISCOVER every 1 s while searching, so 0-1 s depending on phase)",
            ended.text("notice"), ms(reconnected.nanos - switchedOnAt))
        assertEquals("connected", stats.text("link"))
        receiver.awaitExit(20_000)
        back.events.await(5_000, "READY after the receiver quit", after = reconnected.nanos) { it.state == EngineState.WAITING }
    }

    @Test
    fun phoneWithoutAMicrophoneRejectsWithMicUnavailable() {
        val phone = startPhone(source = UnavailableMic())
        val receiver = receiver("no-mic", "settings-a")
        receiver.start(phone.port, "--phone-ip", "127.0.0.1", "--duration", "2")
        receiver.awaitExit(15_000)
        val stats = checkNotNull(receiver.stats())
        report("phone without a microphone: receiver notice %s, link %s", stats.text("notice"), stats.text("link"))
        assertEquals("mic_unavailable", stats.text("notice"))
        assertEquals("searching", stats.text("link"))
        assertTrue(phone.events.all().none { it.snapshot.state == EngineState.STREAMING })
    }

    @Test
    fun receiverFindsThePhoneByUnicastDiscoveryToItsPhoneIp() {
        // 6 + 30 x 3 bytes of UTF-8: the phone must cut it to 64 bytes without splitting a character.
        val name = "Pixel " + "\u2603".repeat(30)
        val phone = startPhone(name = name)
        val receiver = receiver("unicast", "settings-a")
        // A fresh install whose user typed the phone's IP once: no --phone-ip, no remembered phone.
        receiver.settingsFile.writeText("{\"manual_ip\": \"127.0.0.1\"}")
        receiver.start(phone.port, "--duration", "4")
        val connected = phone.events.await(15_000, "discovery and auto-connect") { it.state == EngineState.STREAMING }
        val stats = receiver.awaitStats(5_000, "playback to start") { it.flag("playing") }
        report("unicast discovery (DISCOVER to the typed IP, ANNOUNCE READY, auto-connect): streaming %d ms after start",
            ms(connected.nanos - receiver.startedNanos))
        assertTrue("listed with its phone_id", stats.json.contains("\"phone_id\": ${phone.phoneId}"))
        val shown = "Pixel " + "\u2603".repeat(19)
        assertTrue("name cut on a character boundary: ${stats.json}", stats.json.contains("\"name\": \"${pythonJson(shown)}\""))
        report("phone name of %d UTF-8 bytes listed on the PC as its first %d bytes, whole characters only",
            name.toByteArray().size, shown.toByteArray().size)
        receiver.awaitExit(20_000)
    }

    @Test
    fun receiverFindsThePhoneByBroadcastDiscovery() {
        assumeTrue("set POCKETMIC_INTEROP_BROADCAST=1 to bind 0.0.0.0 for the broadcast test",
            System.getenv("POCKETMIC_INTEROP_BROADCAST") == "1")
        // Bound to all interfaces like the app; Windows loops a host's own broadcasts back to such
        // a socket (never to one bound to 127.0.0.1).
        val phone = startPhone(bindAddress = null)
        val receiver = receiver("broadcast", "settings-a")
        receiver.start(phone.port, "--duration", "4")
        val connected = phone.events.await(15_000, "broadcast discovery and auto-connect") { it.state == EngineState.STREAMING }
        receiver.awaitStats(5_000, "playback to start") { it.flag("playing") }
        val pcHost = connected.pcAddress!!.substringBefore(':')
        report("broadcast discovery (no phone IP known): streaming %d ms after start, PC seen as %s (JVM %s)",
            ms(connected.nanos - receiver.startedNanos), connected.pcAddress, System.getProperty("java.home"))
        assertFalse("found through a broadcast, not loopback", InetAddress.getByName(pcHost).isLoopbackAddress)
        receiver.awaitExit(20_000)
    }

    // --- Harness ----------------------------------------------------------------------------

    private class Phone(val engine: StreamEngine, val events: EngineEvents, val phoneId: Long) {
        val port: Int get() = engine.snapshot().port
    }

    private fun startPhone(
        bindAddress: InetAddress? = loopback,
        port: Int = 0,
        phoneId: Long = Random.nextLong(1, 1L shl 32),
        name: String = "Interop Phone",
        sessionTimeoutMs: Long = StreamEngine.SESSION_TIMEOUT_MS,
        source: AudioSource = ToneSource(TONE_PEAK),
    ): Phone {
        val config = EngineConfig(phoneId, name, port, bindAddress, sessionTimeoutMs)
        val engine = StreamEngine(config, source)
        val events = EngineEvents(engine)
        engines += engine
        engine.start()
        events.await(5_000, "the engine to bind") { it.state == EngineState.WAITING }
        report("engine (phone_id %08x) listening on %s:%d", phoneId, bindAddress?.hostAddress ?: "0.0.0.0", engine.snapshot().port)
        return Phone(engine, events, phoneId)
    }

    /** A receiver whose identity (pc_id, remembered phone) lives in [settingsDir] under this test's work directory. */
    private fun receiver(name: String, settingsDir: String): PcReceiver {
        val dir = File(workDir, settingsDir).apply { mkdirs() }
        return PcReceiver(python, pcDir, File(dir, "settings.json"), workDir, name).also { receivers += it }
    }

    private fun report(format: String, vararg args: Any?) {
        println("[interop] ${testName.methodName}: ${String.format(format, *args)}")
    }

    private fun ms(nanos: Long): Long = nanos / 1_000_000

    /** [text] as Python's json.dump writes it inside quotes: ASCII, with \uXXXX escapes. */
    private fun pythonJson(text: String): String =
        text.map { if (it.code < 0x80) it.toString() else "\\u%04x".format(it.code) }.joinToString("")

    /** A phone whose microphone cannot be opened. */
    private class UnavailableMic : AudioSource {
        override fun start(sink: AudioSink) = sink.onMicStateChanged(MicState.UNAVAILABLE)

        override fun stop() = Unit
    }

    private companion object {
        /** -20 dBFS: an ordinary speaking level. */
        const val TONE_PEAK = 0.1

        /** The stats report the peak of the last whole second, plus up to 0.2 s of buffered audio. */
        const val MUTE_SETTLE_S = 2.5
    }
}

/** Every snapshot the engine's listener reports, timestamped, so tests can time state changes. */
class EngineEvents(engine: StreamEngine) {
    class Event(val nanos: Long, val snapshot: EngineSnapshot) {
        val pcAddress: String? get() = snapshot.pcAddress
    }

    private val events = CopyOnWriteArrayList<Event>()

    init {
        engine.listener = { events += Event(System.nanoTime(), it) }
    }

    fun all(): List<Event> = events.toList()

    fun since(nanos: Long): List<Event> = events.filter { it.nanos >= nanos }

    /** The first event after [after] (System.nanoTime()) matching [condition], waiting up to [timeoutMs]. */
    fun await(timeoutMs: Long, what: String, after: Long = Long.MIN_VALUE, condition: (EngineSnapshot) -> Boolean): Event {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (true) {
            events.firstOrNull { it.nanos >= after && condition(it.snapshot) }?.let { return it }
            if (System.nanoTime() > deadline) {
                throw AssertionError("timed out waiting for $what; engine events: " +
                    events.joinToString { "${it.snapshot.state}@${it.pcAddress}" })
            }
            Thread.sleep(5)
        }
    }
}
