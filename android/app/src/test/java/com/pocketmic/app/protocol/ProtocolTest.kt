package com.pocketmic.app.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class ProtocolTest {

    /** The test vectors from PROTOCOL.md, each checked in both directions. */
    private val vectors: List<Pair<Message, String>> = listOf(
        Discover(0x11223344, "Desk") to "504b4d430101000044332211044465736b",
        Announce(0, 48000, 1, 0xA1B2C3D4, "Pixel 9") to
            "504b4d43010200000080bb000001d4c3b2a107506978656c2039",
        Connect(0xDEADBEEF, 0x11223344, "Desk") to "504b4d4301030000efbeadde44332211044465736b",
        Accept(0xDEADBEEF, 48000, 1, 480) to "504b4d4301040000efbeadde80bb000001e001",
        Reject(0xDEADBEEF, 1) to "504b4d4301050000efbeadde01",
        Audio(0xDEADBEEF, 7, false, shortArrayOf(0, 1, -1, 32767, -32768)) to
            "504b4d4301100000efbeadde07000000050000000100ffffff7f0080",
        Audio(0xDEADBEEF, 8, true, shortArrayOf(0, 0)) to "504b4d4301100100efbeadde08000000020000000000",
        Keepalive(0xDEADBEEF, 1000, 5, 60) to "504b4d4301110000efbeaddee803000005003c00",
        Bye(0xDEADBEEF) to "504b4d4301120000efbeadde",
    )

    @Test
    fun encodesEveryTestVectorExactly() {
        for ((message, hex) in vectors) {
            assertEquals(message.toString(), hex, message.encode().toHex())
        }
    }

    @Test
    fun decodesEveryTestVectorToTheSameFields() {
        for ((message, hex) in vectors) {
            assertEquals(hex, message, Protocol.decode(hex.fromHex()))
        }
    }

    @Test
    fun decodesFromAnOffsetInsideALargerBuffer() {
        val packet = "504b4d4301120000efbeadde".fromHex()
        val buffer = ByteArray(40) { 0x55 }
        packet.copyInto(buffer, destinationOffset = 10)
        assertEquals(Bye(0xDEADBEEF), Protocol.decode(buffer, 10, packet.size))
    }

    @Test
    fun encodeAudioIntoReusableBufferMatchesVector() {
        val dest = ByteArray(Protocol.AUDIO_PACKET_SIZE)
        val length = Protocol.encodeAudio(dest, 0xDEADBEEF, 7, false, shortArrayOf(0, 1, -1, 32767, -32768))
        assertEquals(
            "504b4d4301100000efbeadde07000000050000000100ffffff7f0080",
            dest.copyOf(length).toHex(),
        )
    }

    @Test
    fun mutedAudioAlwaysCarriesSilence() {
        val dest = ByteArray(Protocol.AUDIO_PACKET_SIZE) { 0x7f }
        val length = Protocol.encodeAudio(dest, 0xDEADBEEF, 8, true, shortArrayOf(123, -456))
        assertEquals("504b4d4301100100efbeadde08000000020000000000", dest.copyOf(length).toHex())
    }

    @Test
    fun fullFrameAudioPacketIs978Bytes() {
        val dest = ByteArray(Protocol.AUDIO_PACKET_SIZE)
        val frame = ShortArray(Protocol.FRAME_SAMPLES) { it.toShort() }
        assertEquals(978, Protocol.encodeAudio(dest, 1, 0, false, frame))
        assertEquals(978, Protocol.AUDIO_PACKET_SIZE)
        val decoded = Protocol.decode(dest) as Audio
        assertArrayEquals(frame, decoded.samples)
    }

    @Test
    fun maxU32ValuesRoundTrip() {
        val message = Keepalive(Protocol.U32_MAX, Protocol.U32_MAX, 0xFFFF, 0xFFFF)
        assertEquals(message, Protocol.decode(message.encode()))
    }

    @Test
    fun rejectsOutOfRangeFieldsWhenEncoding() {
        assertThrows(IllegalArgumentException::class.java) { Bye(-1).encode() }
        assertThrows(IllegalArgumentException::class.java) { Bye(Protocol.U32_MAX + 1).encode() }
        assertThrows(IllegalArgumentException::class.java) { Keepalive(1, 1, 0x10000, 0).encode() }
        assertThrows(IllegalArgumentException::class.java) { Reject(1, 256).encode() }
    }

    @Test
    fun stringsAreTruncatedTo64BytesOnACharacterBoundary() {
        val ascii = "a".repeat(100)
        val discover = Discover(1, ascii).encode()
        assertEquals(64, discover[12].toInt())
        assertEquals("a".repeat(64), (Protocol.decode(discover) as Discover).pcName)

        // 21 three-byte characters = 63 bytes; the 22nd would end at byte 66, so it is dropped.
        val wide = "€".repeat(30)
        val truncated = Protocol.truncatedUtf8(wide)
        assertEquals(63, truncated.size)
        assertEquals("€".repeat(21), String(truncated, Charsets.UTF_8))

        // A 4-byte emoji straddling the limit is dropped whole, never split into a bad surrogate.
        val emoji = "a".repeat(62) + "🎤"
        assertEquals("a".repeat(62), String(Protocol.truncatedUtf8(emoji), Charsets.UTF_8))

        assertEquals("Pixel 9", String(Protocol.truncatedUtf8("Pixel 9"), Charsets.UTF_8))
        assertEquals(0, Protocol.truncatedUtf8("").size)
    }

    @Test
    fun nonAsciiNamesRoundTrip() {
        val message = Announce(Protocol.PhoneState.BUSY, 48000, 1, 42, "Téléphone de Zoë")
        assertEquals(message, Protocol.decode(message.encode()))
    }

    @Test
    fun ignoresPacketsShorterThanTheHeader() {
        for (length in 0 until Protocol.HEADER_SIZE) {
            val bytes = "504b4d4301120000".fromHex().copyOf(length)
            assertNull(Protocol.decodeHeader(bytes))
            assertNull(Protocol.decode(bytes))
        }
    }

    @Test
    fun ignoresBadMagic() {
        assertNull(Protocol.decode("504b4d4401120000efbeadde".fromHex()))
        assertNull(Protocol.decode("00000000000000000000000000".fromHex()))
    }

    @Test
    fun ignoresUnknownType() {
        assertNull(Protocol.decode("504b4d4301130000efbeadde".fromHex()))
        assertNull(Protocol.decode("504b4d4301000000".fromHex()))
    }

    @Test
    fun ignoresOtherVersionsButStillExposesTheHeader() {
        val v2Connect = "504b4d4302030000efbeadde44332211044465736b".fromHex()
        assertNull(Protocol.decode(v2Connect))
        assertEquals(Protocol.Header(2, Protocol.Type.CONNECT, 0), Protocol.decodeHeader(v2Connect))
        assertEquals(0xDEADBEEF, Protocol.sessionIdAfterHeader(v2Connect))
        assertEquals(0L, Protocol.sessionIdAfterHeader("504b4d43020300".fromHex()))
    }

    @Test
    fun rejectsTruncatedBodies() {
        for ((_, hex) in vectors) {
            val bytes = hex.fromHex()
            for (length in Protocol.HEADER_SIZE until bytes.size) {
                assertNull("$hex cut to $length", Protocol.decode(bytes, 0, length))
            }
        }
    }

    @Test
    fun rejectsTrailingBytes() {
        for ((_, hex) in vectors) {
            assertNull(hex, Protocol.decode((hex + "00").fromHex()))
        }
    }

    @Test
    fun rejectsStringLongerThan64Bytes() {
        val name = ByteArray(65) { 'x'.code.toByte() }
        val bytes = "504b4d430101000044332211".fromHex() + byteArrayOf(65) + name
        assertNull(Protocol.decode(bytes))
    }

    @Test
    fun rejectsAudioWhosePayloadDisagreesWithSampleCount() {
        // sample_count says 5 but only 4 samples follow.
        assertNull(Protocol.decode("504b4d4301100000efbeadde07000000050000000100ffffff7f".fromHex()))
        // sample_count says 1 but 2 samples follow.
        assertNull(Protocol.decode("504b4d4301100000efbeadde07000000010000000100".fromHex()))
    }

    @Test
    fun invalidUtf8DoesNotThrow() {
        val bytes = "504b4d430101000044332211".fromHex() + byteArrayOf(2, 0xC3.toByte(), 0x28)
        val decoded = Protocol.decode(bytes) as Discover
        assertEquals(0x11223344L, decoded.pcId)
    }

    @Test
    fun outOfBoundsOffsetsReturnNull() {
        val bytes = "504b4d4301120000efbeadde".fromHex()
        assertNull(Protocol.decode(bytes, 4, bytes.size))
        assertNull(Protocol.decode(bytes, -1, 4))
        assertNull(Protocol.decodeHeader(bytes, 0, -1))
        assertEquals(0L, Protocol.sessionIdAfterHeader(bytes, 8, 12))
    }

    @Test
    fun randomGarbageNeverThrows() {
        val random = java.util.Random(1234)
        repeat(20_000) {
            val bytes = ByteArray(random.nextInt(64))
            random.nextBytes(bytes)
            if (bytes.size >= 6 && random.nextBoolean()) {
                Protocol.MAGIC.copyInto(bytes)
                bytes[4] = 1
                bytes[5] = listOf(0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x12)[random.nextInt(8)].toByte()
            }
            Protocol.decode(bytes)
        }
    }

    @Test
    fun seqNextWrapsAroundAt2To32() {
        assertEquals(1L, Seq.next(0))
        assertEquals(0x8000_0000L, Seq.next(0x7FFF_FFFFL))
        assertEquals(0L, Seq.next(Protocol.U32_MAX))
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xFF) }

    private fun String.fromHex(): ByteArray = chunked(2).map { it.toInt(16).toByte() }.toByteArray()
}
