import random
import struct

import pytest

from pocketmic import protocol as p

SAMPLES = struct.pack("<5h", 0, 1, -1, 32767, -32768)

VECTORS = [
    (p.Discover(0x11223344, "Desk"), "504b4d430101000044332211044465736b"),
    (p.Announce(p.PhoneState.READY, 48000, 1, 0xA1B2C3D4, "Pixel 9"),
     "504b4d43010200000080bb000001d4c3b2a107506978656c2039"),
    (p.Connect(0xDEADBEEF, 0x11223344, "Desk"), "504b4d4301030000efbeadde44332211044465736b"),
    (p.Accept(0xDEADBEEF, 48000, 1, 480), "504b4d4301040000efbeadde80bb000001e001"),
    (p.Reject(0xDEADBEEF, p.RejectReason.BUSY), "504b4d4301050000efbeadde01"),
    (p.Audio(0xDEADBEEF, 7, SAMPLES, muted=False),
     "504b4d4301100000efbeadde07000000050000000100ffffff7f0080"),
    (p.Audio(0xDEADBEEF, 8, bytes(4), muted=True), "504b4d4301100100efbeadde08000000020000000000"),
    (p.Keepalive(0xDEADBEEF, 1000, 5, 60), "504b4d4301110000efbeaddee803000005003c00"),
    (p.Bye(0xDEADBEEF), "504b4d4301120000efbeadde"),
]


@pytest.mark.parametrize("msg,hexstr", VECTORS, ids=[type(m).__name__ + str(i) for i, (m, _) in enumerate(VECTORS)])
def test_vector_encodes_exactly(msg, hexstr):
    assert p.encode(msg).hex() == hexstr


@pytest.mark.parametrize("msg,hexstr", VECTORS, ids=[type(m).__name__ + str(i) for i, (m, _) in enumerate(VECTORS)])
def test_vector_decodes_to_same_fields(msg, hexstr):
    assert p.parse(bytes.fromhex(hexstr)) == msg


def test_audio_vector_fields():
    msg = p.parse(bytes.fromhex("504b4d4301100000efbeadde07000000050000000100ffffff7f0080"))
    assert (msg.session_id, msg.seq, msg.sample_count, msg.muted) == (0xDEADBEEF, 7, 5, False)
    assert list(struct.unpack("<5h", msg.pcm)) == [0, 1, -1, 32767, -32768]
    muted = p.parse(bytes.fromhex("504b4d4301100100efbeadde08000000020000000000"))
    assert (muted.seq, muted.sample_count, muted.muted, muted.pcm) == (8, 2, True, bytes(4))


def test_full_size_audio_datagram_is_978_bytes():
    packet = p.encode(p.Audio(1, 0, bytes(2 * p.FRAME_SAMPLES)))
    assert len(packet) == 978
    assert p.parse(packet).sample_count == 480


def test_stereo_audio_payload_uses_channel_count():
    pcm = struct.pack("<4h", 1, 2, 3, 4)
    packet = p.encode(p.Audio(9, 1, pcm, channels=2))
    assert p.parse(packet, channels=2).sample_count == 2
    assert p.decode(packet, channels=1) is None  # 2 samples x 1 ch would leave 4 bytes over


MALFORMED = {
    "empty": b"",
    "short header": bytes.fromhex("504b4d4301"),
    "bad magic": bytes.fromhex("504b4d440101000044332211044465736b"),
    "bad version": bytes.fromhex("504b4d430201000044332211044465736b"),
    "unknown type": bytes.fromhex("504b4d4301ff000044332211044465736b"),
    "truncated discover": bytes.fromhex("504b4d4301010000443322"),
    "string past end": bytes.fromhex("504b4d430101000044332211094465736b"),
    "string over 64": bytes.fromhex("504b4d430101000044332211") + bytes([65]) + b"a" * 65,
    "trailing bytes": bytes.fromhex("504b4d4301120000efbeadde00"),
    "truncated bye": bytes.fromhex("504b4d4301120000efbead"),
    "audio short payload": bytes.fromhex("504b4d4301100000efbeadde07000000050000000100ffffff7f00"),
    "audio long payload": bytes.fromhex("504b4d4301100000efbeadde07000000050000000100ffffff7f008000"),
    "unknown announce state": bytes.fromhex("504b4d43010200000380bb000001d4c3b2a107506978656c2039"),
    "unknown reject reason": bytes.fromhex("504b4d4301050000efbeadde09"),
    "truncated keepalive": bytes.fromhex("504b4d4301110000efbeaddee803000005003c"),
}


@pytest.mark.parametrize("name", MALFORMED)
def test_malformed_packets_are_ignored(name):
    data = MALFORMED[name]
    assert p.decode(data) is None
    with pytest.raises(p.ProtocolError):
        p.parse(data)


def test_fuzzed_packets_never_raise_anything_else():
    rng = random.Random(1234)
    valid = [bytes.fromhex(h) for _, h in VECTORS]
    for _ in range(5000):
        base = bytearray(rng.choice(valid))
        mode = rng.randrange(3)
        if mode == 0:
            base = base[:rng.randrange(len(base) + 1)]
        elif mode == 1:
            for _ in range(rng.randint(1, 4)):
                base[rng.randrange(len(base))] = rng.randrange(256)
        else:
            base = bytearray(rng.randbytes(rng.randrange(40)))
        p.decode(bytes(base))  # must not raise


def test_peek_header_sees_other_versions():
    header = p.peek_header(bytes.fromhex("504b4d4302030000efbeadde"))
    assert (header.version, header.type) == (2, p.MsgType.CONNECT)
    assert p.peek_header(b"XXXX\x01\x01\x00\x00") is None


def test_strings_truncate_on_character_boundary():
    two_byte = "é" * 40                       # 80 bytes of UTF-8
    encoded = p.encode_str(two_byte)
    assert encoded[0] == 64 and encoded[1:].decode("utf-8") == "é" * 32
    mixed = "a" * 63 + "€"                    # the euro sign (3 bytes) would straddle byte 64
    encoded = p.encode_str(mixed)
    assert encoded[0] == 63 and encoded[1:].decode("utf-8") == "a" * 63
    long_name = p.encode(p.Discover(1, "x" * 100))
    assert p.parse(long_name).pc_name == "x" * 64


def test_invalid_utf8_name_is_replaced_not_rejected():
    data = bytes.fromhex("504b4d4301010000443322110241ff")
    assert p.parse(data).pc_name == "A�"


def test_encode_rejects_out_of_range_fields():
    with pytest.raises(ValueError):
        p.encode(p.Bye(1 << 32))
    with pytest.raises(ValueError):
        p.encode(p.Audio(1, 0, b"\x00"))       # half a sample


def test_seq_wraparound_helpers():
    top = 0xFFFFFFFF
    assert p.seq_add(top, 1) == 0
    assert p.seq_add(top, 3) == 2
    assert p.seq_diff(0, top) == 1
    assert p.seq_diff(top, 0) == -1
    assert p.seq_diff(5, 0xFFFFFFF0) == 21
    assert p.seq_newer(2, top) and not p.seq_newer(top, 2)
    assert not p.seq_newer(7, 7)
    assert p.seq_diff(0x80000000, 0) == -0x80000000
