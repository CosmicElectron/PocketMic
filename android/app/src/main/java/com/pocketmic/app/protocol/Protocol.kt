package com.pocketmic.app.protocol

import java.nio.BufferUnderflowException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * PocketMic wire protocol v1 (see PROTOCOL.md in the repository root).
 *
 * u32 fields are carried as [Long] values in `0..0xFFFFFFFF`, u16/u8 fields as [Int].
 * Decoders return null for anything malformed instead of throwing.
 */
object Protocol {
    const val VERSION = 1
    const val DEFAULT_PORT = 50505

    const val HEADER_SIZE = 8
    const val MAX_STRING_BYTES = 64

    const val SAMPLE_RATE = 48_000L
    const val CHANNELS = 1
    const val FRAME_SAMPLES = 480
    const val AUDIO_BODY_HEADER_SIZE = 10
    const val AUDIO_PACKET_SIZE = HEADER_SIZE + AUDIO_BODY_HEADER_SIZE + FRAME_SAMPLES * CHANNELS * 2

    /** Largest UDP payload that fits an Ethernet frame without IP fragmentation. */
    const val MAX_DATAGRAM_SIZE = 1472

    const val FLAG_MUTED = 0x0001

    const val U32_MAX = 0xFFFF_FFFFL

    internal val MAGIC = byteArrayOf(0x50, 0x4b, 0x4d, 0x43) // "PKMC"

    object Type {
        const val DISCOVER = 0x01
        const val ANNOUNCE = 0x02
        const val CONNECT = 0x03
        const val ACCEPT = 0x04
        const val REJECT = 0x05
        const val AUDIO = 0x10
        const val KEEPALIVE = 0x11
        const val BYE = 0x12
    }

    object PhoneState {
        const val READY = 0
        const val STREAMING = 1
        const val BUSY = 2
    }

    object RejectReason {
        const val BUSY = 1
        const val BAD_VERSION = 2
        const val MIC_UNAVAILABLE = 3
    }

    data class Header(val version: Int, val type: Int, val flags: Int)

    /** Parses the 8-byte header; null if the packet is too short or the magic is wrong. */
    fun decodeHeader(data: ByteArray, offset: Int = 0, length: Int = data.size): Header? {
        if (!inBounds(data, offset, length) || length < HEADER_SIZE) return null
        for (i in MAGIC.indices) {
            if (data[offset + i] != MAGIC[i]) return null
        }
        val flags = (data[offset + 6].toInt() and 0xFF) or ((data[offset + 7].toInt() and 0xFF) shl 8)
        return Header(data[offset + 4].toInt() and 0xFF, data[offset + 5].toInt() and 0xFF, flags)
    }

    /**
     * Reads the u32 that directly follows the header (the session_id of a CONNECT) whatever the
     * packet's version, or 0 if the packet is too short. Lets the phone answer a CONNECT from an
     * incompatible PC with REJECT BAD_VERSION for the right session.
     */
    fun sessionIdAfterHeader(data: ByteArray, offset: Int = 0, length: Int = data.size): Long {
        if (!inBounds(data, offset, length) || length < HEADER_SIZE + 4) return 0
        return ByteBuffer.wrap(data, offset + HEADER_SIZE, 4).order(ByteOrder.LITTLE_ENDIAN).u32()
    }

    /**
     * Decodes a complete version-1 datagram. Returns null for a bad magic, another version, an
     * unknown type, a wrong length (too short or trailing bytes) or an over-long string.
     */
    fun decode(data: ByteArray, offset: Int = 0, length: Int = data.size): Message? {
        val header = decodeHeader(data, offset, length) ?: return null
        if (header.version != VERSION) return null
        val buf = ByteBuffer.wrap(data, offset + HEADER_SIZE, length - HEADER_SIZE)
            .order(ByteOrder.LITTLE_ENDIAN)
        return try {
            val message = when (header.type) {
                Type.DISCOVER -> Discover(buf.u32(), buf.str() ?: return null)
                Type.ANNOUNCE -> Announce(buf.u8(), buf.u32(), buf.u8(), buf.u32(), buf.str() ?: return null)
                Type.CONNECT -> Connect(buf.u32(), buf.u32(), buf.str() ?: return null)
                Type.ACCEPT -> Accept(buf.u32(), buf.u32(), buf.u8(), buf.u16())
                Type.REJECT -> Reject(buf.u32(), buf.u8())
                Type.AUDIO -> decodeAudioBody(buf, header.flags) ?: return null
                Type.KEEPALIVE -> Keepalive(buf.u32(), buf.u32(), buf.u16(), buf.u16())
                Type.BYE -> Bye(buf.u32())
                else -> return null
            }
            if (buf.hasRemaining()) null else message
        } catch (_: BufferUnderflowException) {
            null
        }
    }

    /**
     * Writes an AUDIO datagram for the first [count] samples of [samples] into [dest] and returns
     * its length. A muted packet always carries silence, whatever [samples] holds. Allocation-free
     * so the capture thread can call it 100 times a second.
     */
    fun encodeAudio(
        dest: ByteArray,
        sessionId: Long,
        seq: Long,
        muted: Boolean,
        samples: ShortArray,
        count: Int = samples.size,
    ): Int {
        require(count in 0..samples.size) { "count $count outside 0..${samples.size}" }
        val size = HEADER_SIZE + AUDIO_BODY_HEADER_SIZE + count * CHANNELS * 2
        require(dest.size >= size) { "buffer of ${dest.size} bytes cannot hold $size" }
        val buf = ByteBuffer.wrap(dest).order(ByteOrder.LITTLE_ENDIAN)
        buf.putHeader(Type.AUDIO, if (muted) FLAG_MUTED else 0)
        buf.putU32(sessionId)
        buf.putU32(seq)
        buf.putU16(count)
        if (muted) {
            dest.fill(0, buf.position(), size)
        } else {
            for (i in 0 until count) buf.putShort(samples[i])
        }
        return size
    }

    /** UTF-8 bytes of [text], truncated to [MAX_STRING_BYTES] without splitting a character. */
    fun truncatedUtf8(text: String): ByteArray {
        val bytes = text.toByteArray(Charsets.UTF_8)
        if (bytes.size <= MAX_STRING_BYTES) return bytes
        var cut = MAX_STRING_BYTES
        while (cut > 0 && (bytes[cut].toInt() and 0xC0) == 0x80) cut--
        return bytes.copyOf(cut)
    }

    private fun decodeAudioBody(buf: ByteBuffer, flags: Int): Audio? {
        val sessionId = buf.u32()
        val seq = buf.u32()
        val count = buf.u16()
        if (buf.remaining() != count * CHANNELS * 2) return null
        val samples = ShortArray(count * CHANNELS) { buf.short }
        return Audio(sessionId, seq, flags and FLAG_MUTED != 0, samples)
    }

    private fun inBounds(data: ByteArray, offset: Int, length: Int): Boolean =
        offset >= 0 && length >= 0 && offset <= data.size - length
}

/**
 * AUDIO sequence numbers, which wrap at 2^32. The phone only produces them; comparing them across
 * the wrap is the receiver's (the PC's) job.
 */
object Seq {
    fun next(seq: Long): Long = (seq + 1) and Protocol.U32_MAX
}

sealed interface Message {
    fun encode(): ByteArray
}

data class Discover(val pcId: Long, val pcName: String) : Message {
    override fun encode(): ByteArray {
        val name = Protocol.truncatedUtf8(pcName)
        return packet(Protocol.Type.DISCOVER, 4 + 1 + name.size) {
            putU32(pcId)
            putStr(name)
        }
    }
}

data class Announce(
    val state: Int,
    val sampleRate: Long,
    val channels: Int,
    val phoneId: Long,
    val phoneName: String,
) : Message {
    override fun encode(): ByteArray {
        val name = Protocol.truncatedUtf8(phoneName)
        return packet(Protocol.Type.ANNOUNCE, 1 + 4 + 1 + 4 + 1 + name.size) {
            putU8(state)
            putU32(sampleRate)
            putU8(channels)
            putU32(phoneId)
            putStr(name)
        }
    }
}

data class Connect(val sessionId: Long, val pcId: Long, val pcName: String) : Message {
    override fun encode(): ByteArray {
        val name = Protocol.truncatedUtf8(pcName)
        return packet(Protocol.Type.CONNECT, 4 + 4 + 1 + name.size) {
            putU32(sessionId)
            putU32(pcId)
            putStr(name)
        }
    }
}

data class Accept(
    val sessionId: Long,
    val sampleRate: Long,
    val channels: Int,
    val frameSamples: Int,
) : Message {
    override fun encode(): ByteArray = packet(Protocol.Type.ACCEPT, 4 + 4 + 1 + 2) {
        putU32(sessionId)
        putU32(sampleRate)
        putU8(channels)
        putU16(frameSamples)
    }
}

data class Reject(val sessionId: Long, val reason: Int) : Message {
    override fun encode(): ByteArray = packet(Protocol.Type.REJECT, 4 + 1) {
        putU32(sessionId)
        putU8(reason)
    }
}

/** An AUDIO packet; [samples] holds sample_count x channels values (mono in v1). */
class Audio(val sessionId: Long, val seq: Long, val muted: Boolean, val samples: ShortArray) : Message {
    override fun encode(): ByteArray {
        val bytes = ByteArray(Protocol.HEADER_SIZE + Protocol.AUDIO_BODY_HEADER_SIZE + samples.size * 2)
        Protocol.encodeAudio(bytes, sessionId, seq, muted, samples)
        return bytes
    }

    override fun equals(other: Any?): Boolean =
        other is Audio && sessionId == other.sessionId && seq == other.seq &&
            muted == other.muted && samples.contentEquals(other.samples)

    override fun hashCode(): Int =
        ((sessionId.hashCode() * 31 + seq.hashCode()) * 31 + muted.hashCode()) * 31 +
            samples.contentHashCode()

    override fun toString(): String =
        "Audio(sessionId=$sessionId, seq=$seq, muted=$muted, samples=${samples.size})"
}

data class Keepalive(
    val sessionId: Long,
    val highestSeq: Long,
    val lossPermille: Int,
    val bufferMs: Int,
) : Message {
    override fun encode(): ByteArray = packet(Protocol.Type.KEEPALIVE, 4 + 4 + 2 + 2) {
        putU32(sessionId)
        putU32(highestSeq)
        putU16(lossPermille)
        putU16(bufferMs)
    }
}

data class Bye(val sessionId: Long) : Message {
    override fun encode(): ByteArray = packet(Protocol.Type.BYE, 4) {
        putU32(sessionId)
    }
}

private inline fun packet(type: Int, bodySize: Int, body: ByteBuffer.() -> Unit): ByteArray {
    val bytes = ByteArray(Protocol.HEADER_SIZE + bodySize)
    val buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
    buf.putHeader(type, 0)
    buf.body()
    check(!buf.hasRemaining()) { "type $type body filled ${buf.position()} of ${bytes.size} bytes" }
    return bytes
}

private fun ByteBuffer.putHeader(type: Int, flags: Int) {
    put(Protocol.MAGIC)
    put(Protocol.VERSION.toByte())
    put(type.toByte())
    putU16(flags)
}

private fun ByteBuffer.putU8(value: Int) {
    require(value in 0..0xFF) { "u8 out of range: $value" }
    put(value.toByte())
}

private fun ByteBuffer.putU16(value: Int) {
    require(value in 0..0xFFFF) { "u16 out of range: $value" }
    putShort(value.toShort())
}

private fun ByteBuffer.putU32(value: Long) {
    require(value in 0..Protocol.U32_MAX) { "u32 out of range: $value" }
    putInt(value.toInt())
}

private fun ByteBuffer.putStr(utf8: ByteArray) {
    putU8(utf8.size)
    put(utf8)
}

private fun ByteBuffer.u8(): Int = get().toInt() and 0xFF
private fun ByteBuffer.u16(): Int = short.toInt() and 0xFFFF
private fun ByteBuffer.u32(): Long = int.toLong() and Protocol.U32_MAX

private fun ByteBuffer.str(): String? {
    val length = u8()
    if (length > Protocol.MAX_STRING_BYTES) return null
    val bytes = ByteArray(length)
    get(bytes)
    return String(bytes, Charsets.UTF_8)
}
