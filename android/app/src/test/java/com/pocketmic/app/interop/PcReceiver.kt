package com.pocketmic.app.interop

import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * The real PC receiver, `python -m pocketmic --nogui --device null`, as a child process. Its
 * settings file is [settingsFile] (via POCKETMIC_SETTINGS), which gives it its own pc_id and
 * single-instance lock and keeps it away from the user's real settings; its log lands next to it.
 */
class PcReceiver(
    private val python: String,
    private val pcDir: File,
    val settingsFile: File,
    private val workDir: File,
    val name: String,
) {
    val statsFile = File(workDir, "$name-stats.json")
    private val consoleFile = File(workDir, "$name-console.txt")
    private var process: Process? = null

    /** System.nanoTime() when the process was started. */
    var startedNanos = 0L
        private set

    /** System.nanoTime() when the process was seen to exit, or 0 while it runs. */
    @Volatile var exitedNanos = 0L
        private set

    val isAlive: Boolean get() = process?.isAlive == true

    /** Starts the receiver for the phone on UDP [port], with any extra command-line [args]. */
    fun start(port: Int, vararg args: String): PcReceiver {
        check(process == null) { "$name was already started" }
        statsFile.delete()
        val command = listOf(
            python, "-m", "pocketmic", "--nogui", "--device", "null",
            "--port", port.toString(), "--stats-file", statsFile.absolutePath, *args,
        )
        val builder = ProcessBuilder(command)
            .directory(pcDir)
            .redirectErrorStream(true)
            .redirectOutput(consoleFile)
        builder.environment()["POCKETMIC_SETTINGS"] = settingsFile.absolutePath
        startedNanos = System.nanoTime()
        val started = builder.start()
        process = started
        // Process.onExit() is not in android.jar, which this code compiles against.
        Thread({
            started.waitFor()
            exitedNanos = System.nanoTime()
        }, "$name-exit-watch").apply {
            isDaemon = true
            start()
        }
        return this
    }

    /**
     * Ends the process the way a crash or Task Manager would (TerminateProcess), so it sends no
     * BYE. Returns System.nanoTime() just before the kill.
     */
    fun kill(): Long {
        val running = checkNotNull(process) { "$name was never started" }
        val killedAt = System.nanoTime()
        running.destroyForcibly()
        check(running.waitFor(5, TimeUnit.SECONDS)) { "$name did not die" }
        return killedAt
    }

    /** Waits for a run started with --duration to end by itself, and checks it exited cleanly. */
    fun awaitExit(timeoutMs: Long) {
        val running = checkNotNull(process) { "$name was never started" }
        if (!running.waitFor(timeoutMs, TimeUnit.MILLISECONDS)) fail("did not exit within $timeoutMs ms")
        while (exitedNanos == 0L) Thread.sleep(1)
        if (running.exitValue() != 0) fail("exited with code ${running.exitValue()}")
    }

    /** The latest stats file contents, or null before the first write. */
    fun stats(): ReceiverStats? {
        val text = try {
            statsFile.readText()
        } catch (_: IOException) {
            return null // not written yet, or being replaced right now
        }
        return ReceiverStats(text).takeIf { it.isComplete }
    }

    /** Polls the stats file (rewritten every second) until [condition] holds. */
    fun awaitStats(timeoutMs: Long, what: String, condition: (ReceiverStats) -> Boolean): ReceiverStats {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (true) {
            val exited = !isAlive
            stats()?.takeIf(condition)?.let { return it }
            // The final snapshot is written on the way out, so it was read above if it matched.
            if (exited) fail("exited before $what; last stats: ${stats()?.json}")
            if (System.nanoTime() > deadline) fail("timed out waiting for $what; last stats: ${stats()?.json}")
            Thread.sleep(50)
        }
    }

    fun destroy() {
        process?.destroyForcibly()?.waitFor(5, TimeUnit.SECONDS)
    }

    /** The end of the receiver's console output and log file, for failure messages. */
    fun diagnostics(): String {
        fun tail(file: File) =
            if (file.exists()) file.readLines().takeLast(DIAGNOSTIC_LINES).joinToString("\n") else "(none)"
        return "--- $name console ---\n${tail(consoleFile)}\n" +
            "--- $name pocketmic.log ---\n${tail(File(settingsFile.parentFile, "pocketmic.log"))}"
    }

    private fun fail(problem: String): Nothing = throw AssertionError("Receiver $name $problem\n${diagnostics()}")

    private companion object {
        const val DIAGNOSTIC_LINES = 25
    }
}

/**
 * One snapshot from the receiver's --stats-file (Receiver.snapshot() in pc/pocketmic/receiver.py).
 * Only top-level scalar fields are read, which a regular expression handles: the unit-test
 * classpath has no JSON parser (android.jar's org.json is a stub there).
 */
class ReceiverStats(val json: String) {
    /** A complete write ends with the closing brace; anything else was read mid-replace. */
    val isComplete: Boolean get() = json.trimEnd().endsWith("}") && raw("time") != null

    fun text(key: String): String? = raw(key)?.removeSurrounding("\"")

    fun number(key: String): Double =
        raw(key)?.toDoubleOrNull() ?: throw AssertionError("stats field \"$key\" is not a number in $json")

    fun flag(key: String): Boolean = raw(key) == "true"

    /** Seconds on the receiver's wall clock when this snapshot was taken. */
    val time: Double get() = number("time")

    private fun raw(key: String): String? =
        Regex("\"${Regex.escape(key)}\": (\"[^\"]*\"|[^,\\s}]+)").find(json)?.groupValues?.get(1)
}
