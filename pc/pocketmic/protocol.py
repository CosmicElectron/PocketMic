"""PocketMic wire protocol v1 (see PROTOCOL.md): message types, encoding and decoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Union

MAGIC = b"PKMC"
VERSION = 1
DEFAULT_PORT = 50505

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SAMPLES = 480
MAX_STR_BYTES = 64

FLAG_MUTED = 0x0001

_HEADER = struct.Struct("<4sBBH")
HEADER_SIZE = _HEADER.size
_U8 = struct.Struct("<B")
_U32 = struct.Struct("<I")
_ANNOUNCE_FIXED = struct.Struct("<BIBI")
_ACCEPT = struct.Struct("<IIBH")
_REJECT = struct.Struct("<IB")
_AUDIO_FIXED = struct.Struct("<IIH")
_KEEPALIVE = struct.Struct("<IIHH")

SEQ_MOD = 1 << 32
_SEQ_HALF = 1 << 31


class MsgType(IntEnum):
    DISCOVER = 0x01
    ANNOUNCE = 0x02
    CONNECT = 0x03
    ACCEPT = 0x04
    REJECT = 0x05
    AUDIO = 0x10
    KEEPALIVE = 0x11
    BYE = 0x12


class PhoneState(IntEnum):
    READY = 0
    STREAMING = 1  # streaming to the PC whose DISCOVER carried the same pc_id
    BUSY = 2       # streaming to a different PC


class RejectReason(IntEnum):
    BUSY = 1
    BAD_VERSION = 2
    MIC_UNAVAILABLE = 3


class ProtocolError(ValueError):
    """Raised for any datagram that does not follow the protocol."""


@dataclass(frozen=True)
class Header:
    version: int
    type: int
    flags: int


@dataclass(frozen=True)
class Discover:
    pc_id: int
    pc_name: str


@dataclass(frozen=True)
class Announce:
    state: PhoneState
    sample_rate: int
    channels: int
    phone_id: int
    phone_name: str


@dataclass(frozen=True)
class Connect:
    session_id: int
    pc_id: int
    pc_name: str


@dataclass(frozen=True)
class Accept:
    session_id: int
    sample_rate: int
    channels: int
    frame_samples: int


@dataclass(frozen=True)
class Reject:
    session_id: int
    reason: RejectReason


@dataclass(frozen=True)
class Audio:
    """One AUDIO datagram. `pcm` is the raw little-endian int16 payload (interleaved if channels > 1)."""

    session_id: int
    seq: int
    pcm: bytes
    muted: bool = False
    channels: int = 1

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // (2 * self.channels)


@dataclass(frozen=True)
class Keepalive:
    session_id: int
    highest_seq: int
    loss_permille: int
    buffer_ms: int


@dataclass(frozen=True)
class Bye:
    session_id: int


Message = Union[Discover, Announce, Connect, Accept, Reject, Audio, Keepalive, Bye]


# ---------------------------------------------------------------------------
# Sequence numbers (u32, wrapping)
# ---------------------------------------------------------------------------

def seq_diff(a: int, b: int) -> int:
    """Signed distance a - b in the wrapping u32 sequence space, in [-2^31, 2^31)."""
    return ((a - b + _SEQ_HALF) % SEQ_MOD) - _SEQ_HALF


def seq_add(seq: int, n: int) -> int:
    return (seq + n) % SEQ_MOD


def seq_newer(a: int, b: int) -> bool:
    """True if `a` comes after `b`, taking wraparound into account."""
    return seq_diff(a, b) > 0


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _check_u(value: int, bits: int, field: str) -> int:
    if not 0 <= value < (1 << bits):
        raise ValueError(f"{field}={value} does not fit in u{bits}")
    return value


def encode_str(text: str) -> bytes:
    """u8 length + UTF-8, truncated to MAX_STR_BYTES on a character boundary."""
    raw = text.encode("utf-8")
    if len(raw) > MAX_STR_BYTES:
        # The input was valid UTF-8, so the only undecodable part is a character split by the cut.
        raw = raw[:MAX_STR_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
    return _U8.pack(len(raw)) + raw


def _header(msg_type: MsgType, flags: int = 0) -> bytes:
    return _HEADER.pack(MAGIC, VERSION, msg_type, flags)


def encode(msg: Message) -> bytes:
    if isinstance(msg, Discover):
        return _header(MsgType.DISCOVER) + _U32.pack(_check_u(msg.pc_id, 32, "pc_id")) + encode_str(msg.pc_name)
    if isinstance(msg, Announce):
        return (_header(MsgType.ANNOUNCE)
                + _ANNOUNCE_FIXED.pack(_check_u(int(msg.state), 8, "state"),
                                       _check_u(msg.sample_rate, 32, "sample_rate"),
                                       _check_u(msg.channels, 8, "channels"),
                                       _check_u(msg.phone_id, 32, "phone_id"))
                + encode_str(msg.phone_name))
    if isinstance(msg, Connect):
        return (_header(MsgType.CONNECT)
                + _U32.pack(_check_u(msg.session_id, 32, "session_id"))
                + _U32.pack(_check_u(msg.pc_id, 32, "pc_id"))
                + encode_str(msg.pc_name))
    if isinstance(msg, Accept):
        return _header(MsgType.ACCEPT) + _ACCEPT.pack(_check_u(msg.session_id, 32, "session_id"),
                                                      _check_u(msg.sample_rate, 32, "sample_rate"),
                                                      _check_u(msg.channels, 8, "channels"),
                                                      _check_u(msg.frame_samples, 16, "frame_samples"))
    if isinstance(msg, Reject):
        return _header(MsgType.REJECT) + _REJECT.pack(_check_u(msg.session_id, 32, "session_id"),
                                                      _check_u(int(msg.reason), 8, "reason"))
    if isinstance(msg, Audio):
        if msg.channels < 1 or len(msg.pcm) % (2 * msg.channels):
            raise ValueError("AUDIO payload is not a whole number of int16 frames")
        flags = FLAG_MUTED if msg.muted else 0
        return (_header(MsgType.AUDIO, flags)
                + _AUDIO_FIXED.pack(_check_u(msg.session_id, 32, "session_id"),
                                    _check_u(msg.seq, 32, "seq"),
                                    _check_u(msg.sample_count, 16, "sample_count"))
                + msg.pcm)
    if isinstance(msg, Keepalive):
        return _header(MsgType.KEEPALIVE) + _KEEPALIVE.pack(_check_u(msg.session_id, 32, "session_id"),
                                                            _check_u(msg.highest_seq, 32, "highest_seq"),
                                                            _check_u(msg.loss_permille, 16, "loss_permille"),
                                                            _check_u(msg.buffer_ms, 16, "buffer_ms"))
    if isinstance(msg, Bye):
        return _header(MsgType.BYE) + _U32.pack(_check_u(msg.session_id, 32, "session_id"))
    raise TypeError(f"not a protocol message: {msg!r}")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def peek_header(data: bytes) -> Optional[Header]:
    """Header of a datagram with the right magic, whatever its version; None otherwise."""
    if len(data) < HEADER_SIZE:
        return None
    magic, version, msg_type, flags = _HEADER.unpack_from(data)
    if magic != MAGIC:
        return None
    return Header(version, msg_type, flags)


class _Reader:
    def __init__(self, data: bytes, offset: int):
        self._data = data
        self._pos = offset

    def take(self, fmt: struct.Struct) -> tuple:
        if self._pos + fmt.size > len(self._data):
            raise ProtocolError("datagram too short")
        values = fmt.unpack_from(self._data, self._pos)
        self._pos += fmt.size
        return values

    def bytes(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ProtocolError("datagram too short")
        raw = self._data[self._pos:self._pos + n]
        self._pos += n
        return raw

    def string(self) -> str:
        (length,) = self.take(_U8)
        if length > MAX_STR_BYTES:
            raise ProtocolError("string longer than 64 bytes")
        # Names are display-only, so undecodable bytes are replaced rather than rejecting the packet.
        return self.bytes(length).decode("utf-8", errors="replace")

    def finish(self) -> None:
        if self._pos != len(self._data):
            raise ProtocolError("unexpected trailing bytes")


def _enum(enum_type, value: int, field: str):
    try:
        return enum_type(value)
    except ValueError:
        raise ProtocolError(f"unknown {field} {value}") from None


def parse(data: bytes, channels: int = 1) -> Message:
    """Decode one datagram or raise ProtocolError. `channels` sizes AUDIO payloads (from ACCEPT)."""
    header = peek_header(data)
    if header is None:
        raise ProtocolError("too short or bad magic")
    if header.version != VERSION:
        raise ProtocolError(f"unsupported version {header.version}")
    r = _Reader(data, HEADER_SIZE)
    t = header.type
    if t == MsgType.DISCOVER:
        (pc_id,) = r.take(_U32)
        msg: Message = Discover(pc_id, r.string())
    elif t == MsgType.ANNOUNCE:
        state, rate, ch, phone_id = r.take(_ANNOUNCE_FIXED)
        msg = Announce(_enum(PhoneState, state, "state"), rate, ch, phone_id, r.string())
    elif t == MsgType.CONNECT:
        (session_id,) = r.take(_U32)
        (pc_id,) = r.take(_U32)
        msg = Connect(session_id, pc_id, r.string())
    elif t == MsgType.ACCEPT:
        msg = Accept(*r.take(_ACCEPT))
    elif t == MsgType.REJECT:
        session_id, reason = r.take(_REJECT)
        msg = Reject(session_id, _enum(RejectReason, reason, "reason"))
    elif t == MsgType.AUDIO:
        if channels < 1:
            raise ProtocolError("invalid channel count")
        session_id, seq, count = r.take(_AUDIO_FIXED)
        msg = Audio(session_id, seq, r.bytes(count * channels * 2), bool(header.flags & FLAG_MUTED), channels)
    elif t == MsgType.KEEPALIVE:
        msg = Keepalive(*r.take(_KEEPALIVE))
    elif t == MsgType.BYE:
        (session_id,) = r.take(_U32)
        msg = Bye(session_id)
    else:
        raise ProtocolError(f"unknown message type 0x{t:02x}")
    r.finish()
    return msg


def decode(data: bytes, channels: int = 1) -> Optional[Message]:
    """Like parse(), but returns None for anything malformed, as receivers must ignore such packets."""
    try:
        return parse(data, channels)
    except ProtocolError:
        return None
