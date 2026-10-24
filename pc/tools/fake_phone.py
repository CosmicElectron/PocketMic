"""Phone-side PocketMic simulator: a spec-correct stand-in for the Android app.

Run it on the LAN or on 127.0.0.1 and point the receiver at it:
    python tools/fake_phone.py --port 50505 --freq 1000 --level-dbfs -20
It can also impair the stream (--loss, --jitter-ms, --reorder, --drift-ppm). Tests use FakePhone
as a library, with port=0 for an ephemeral port.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import math
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pocketmic import protocol as p  # noqa: E402
from pocketmic.net import disable_udp_connreset  # noqa: E402

SESSION_TIMEOUT_S = 3.0


@dataclass
class Session:
    session_id: int
    pc_id: int
    pc_name: str
    addr: tuple[str, int]
    last_keepalive: float
    seq: int = 0


class FakePhone:
    def __init__(self, port: int = p.DEFAULT_PORT, host: str = "0.0.0.0", name: str = "Fake Phone",
                 phone_id: Optional[int] = None, freq: float = 1000.0, level_dbfs: float = -20.0,
                 loss: float = 0.0, jitter_ms: float = 0.0, drift_ppm: float = 0.0, reorder: float = 0.0,
                 mic_available: bool = True, seed: Optional[int] = None):
        self.name = name
        self.phone_id = phone_id if phone_id else random.getrandbits(32) or 1
        self.freq = freq
        self.amplitude = 10 ** (level_dbfs / 20)   # peak, relative to a full-scale sine
        self.loss = loss
        self.jitter_ms = jitter_ms
        self.drift_ppm = drift_ppm
        self.reorder = reorder
        self.mic_available = mic_available
        self.muted = False
        self._rng = random.Random(seed)
        self._host = host
        self._requested_port = port
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._session: Optional[Session] = None
        self._blackout_until = 0.0
        self._sample_pos = 0          # phase-continuous sine across sessions

        # Observations for tests.
        self.sessions_started = 0
        self.connects_received = 0
        self.rejects_sent = 0
        self.keepalives_received = 0
        self.last_keepalive: Optional[p.Keepalive] = None
        self.stale_byes_sent = 0
        self.byes_received = 0
        self.packets_sent = 0
        self.discovers_received = 0

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    @property
    def session(self) -> Optional[Session]:
        with self._lock:
            return None if self._session is None else Session(**vars(self._session))

    def start(self) -> "FakePhone":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Like Android's: sending AUDIO to a PC that has gone away must not make every receive
        # fail on Windows, or the session would never time out.
        disable_udp_connreset(self._sock)
        self._sock.bind((self._host, self._requested_port))
        self._sock.settimeout(0.02)
        self._stop.clear()
        for target, name in ((self._receive_loop, "fake-phone-rx"), (self._audio_loop, "fake-phone-tx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        """Turn PocketMic 'off': stop answering, BYE to the current PC, then close the socket."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            self._send(p.Bye(session.session_id), session.addr)
        self._sock.close()

    def blackout(self, seconds: float) -> None:
        """Simulate a Wi-Fi outage: drop every packet in both directions for a while."""
        self._blackout_until = time.monotonic() + seconds

    def _blacked_out(self) -> bool:
        return time.monotonic() < self._blackout_until

    def _send(self, msg, addr) -> None:
        if self._blacked_out():
            return
        try:
            self._sock.sendto(p.encode(msg), addr)
        except OSError:
            pass

    def _state_for(self, pc_id: int) -> p.PhoneState:
        if self._session is None:
            return p.PhoneState.READY
        return p.PhoneState.STREAMING if self._session.pc_id == pc_id else p.PhoneState.BUSY

    # -- control -----------------------------------------------------------------------------

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                if self._stop.is_set():
                    return
                data = None          # still run the session timeout below
            with self._lock:
                if data is not None and not self._blacked_out():
                    self._handle(data, addr)
                s = self._session
                if s is not None and time.monotonic() - s.last_keepalive > SESSION_TIMEOUT_S:
                    self._session = None

    def _handle(self, data: bytes, addr) -> None:
        header = p.peek_header(data)
        if header is None:
            return
        if header.version != p.VERSION:
            if header.type == p.MsgType.CONNECT and len(data) >= p.HEADER_SIZE + 4:
                session_id = int.from_bytes(data[p.HEADER_SIZE:p.HEADER_SIZE + 4], "little")
                self._reject(session_id, p.RejectReason.BAD_VERSION, addr)
            return
        msg = p.decode(data)
        now = time.monotonic()
        if isinstance(msg, p.Discover):
            self.discovers_received += 1
            self._send(p.Announce(self._state_for(msg.pc_id), p.SAMPLE_RATE, p.CHANNELS,
                                  self.phone_id, self.name), addr)
        elif isinstance(msg, p.Connect):
            self.connects_received += 1
            s = self._session
            if not self.mic_available:
                self._reject(msg.session_id, p.RejectReason.MIC_UNAVAILABLE, addr)
            elif s is not None and s.session_id == msg.session_id:
                self._accept(msg.session_id, addr)          # retried CONNECT: answer again
            elif s is None or s.pc_id == msg.pc_id:
                # READY, or the same PC reconnecting (restart, new port): (re)start the session.
                self._session = Session(msg.session_id, msg.pc_id, msg.pc_name, addr, now)
                self.sessions_started += 1
                self._accept(msg.session_id, addr)
            else:
                self._reject(msg.session_id, p.RejectReason.BUSY, addr)
        elif isinstance(msg, p.Keepalive):
            s = self._session
            if s is not None and msg.session_id == s.session_id:
                s.last_keepalive = now
                self.keepalives_received += 1
                self.last_keepalive = msg
            else:
                self.stale_byes_sent += 1
                self._send(p.Bye(msg.session_id), addr)
        elif isinstance(msg, p.Bye):
            s = self._session
            if s is not None and msg.session_id == s.session_id:
                self.byes_received += 1
                self._session = None

    def _accept(self, session_id: int, addr) -> None:
        self._send(p.Accept(session_id, p.SAMPLE_RATE, p.CHANNELS, p.FRAME_SAMPLES), addr)

    def _reject(self, session_id: int, reason: p.RejectReason, addr) -> None:
        self.rejects_sent += 1
        self._send(p.Reject(session_id, reason), addr)

    # -- audio -------------------------------------------------------------------------------

    def _make_pcm(self) -> bytes:
        n = np.arange(self._sample_pos, self._sample_pos + p.FRAME_SAMPLES)
        self._sample_pos += p.FRAME_SAMPLES
        if self.muted:
            return bytes(2 * p.FRAME_SAMPLES)
        wave = self.amplitude * np.sin(2 * math.pi * self.freq * n / p.SAMPLE_RATE)
        return np.clip(np.round(wave * 32767), -32768, 32767).astype("<i2").tobytes()

    def _audio_loop(self) -> None:
        # Real-time pacing at 48000 * (1 + drift) samples per wall-clock second. Every packet that
        # is due gets produced even if the OS timer woke us late, so the average rate is exact.
        interval = p.FRAME_SAMPLES / (p.SAMPLE_RATE * (1 + self.drift_ppm / 1e6))
        delayed: list[tuple[float, int, bytes, tuple]] = []
        order = itertools.count()
        held: Optional[tuple[bytes, tuple]] = None
        current_session = None
        next_due = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            with self._lock:
                s = self._session
                if s is None or s.session_id != current_session:
                    current_session = s.session_id if s is not None else None
                    next_due = now
                    held = None
                while s is not None and now >= next_due:
                    packet = p.encode(p.Audio(s.session_id, s.seq, self._make_pcm(), self.muted))
                    s.seq = p.seq_add(s.seq, 1)
                    send_at = next_due + (self._rng.uniform(0, self.jitter_ms) / 1000 if self.jitter_ms else 0)
                    next_due += interval
                    if self._rng.random() < self.loss:
                        continue
                    if held is not None:
                        # The held packet goes out right after this one: a swapped pair.
                        heapq.heappush(delayed, (send_at, next(order), packet, s.addr))
                        heapq.heappush(delayed, (send_at, next(order), held[0], held[1]))
                        held = None
                    elif self._rng.random() < self.reorder:
                        held = (packet, s.addr)
                    else:
                        heapq.heappush(delayed, (send_at, next(order), packet, s.addr))
            while delayed and delayed[0][0] <= now:
                _, _, packet, addr = heapq.heappop(delayed)
                if not self._blacked_out():
                    try:
                        self._sock.sendto(packet, addr)
                        self.packets_sent += 1
                    except OSError:
                        pass
            deadlines = ([next_due] if s is not None else []) + ([delayed[0][0]] if delayed else [])
            wait = min(deadlines) - time.perf_counter() if deadlines else 0.005
            self._stop.wait(min(0.005, max(0.0, wait)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Simulate a PocketMic phone on this PC.")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=p.DEFAULT_PORT)
    parser.add_argument("--name", default="Fake Phone")
    parser.add_argument("--phone-id", type=lambda s: int(s, 0), default=None)
    parser.add_argument("--freq", type=float, default=1000.0, help="sine frequency in Hz")
    parser.add_argument("--level-dbfs", type=float, default=-20.0, help="sine peak level")
    parser.add_argument("--loss", type=float, default=0.0, help="fraction of packets to drop")
    parser.add_argument("--jitter-ms", type=float, default=0.0, help="random extra delay per packet")
    parser.add_argument("--drift-ppm", type=float, default=0.0, help="sample clock error")
    parser.add_argument("--reorder", type=float, default=0.0, help="fraction of packets swapped with the next")
    parser.add_argument("--no-mic", action="store_true", help="answer CONNECT with MIC_UNAVAILABLE")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl+C)")
    args = parser.parse_args(argv)

    phone = FakePhone(port=args.port, host=args.host, name=args.name, phone_id=args.phone_id,
                      freq=args.freq, level_dbfs=args.level_dbfs, loss=args.loss,
                      jitter_ms=args.jitter_ms, drift_ppm=args.drift_ppm, reorder=args.reorder,
                      mic_available=not args.no_mic).start()
    print(f"Fake phone '{phone.name}' (id {phone.phone_id:08x}) listening on {args.host}:{phone.port}")
    started = time.monotonic()
    last_session = None
    try:
        while not args.duration or time.monotonic() - started < args.duration:
            time.sleep(0.5)
            s = phone.session
            current = s.session_id if s else None
            if current != last_session:
                print(f"Streaming to {s.pc_name} at {s.addr[0]}:{s.addr[1]} (session {s.session_id:08x})"
                      if s else "Ready (no PC connected)")
                last_session = current
    except KeyboardInterrupt:
        pass
    finally:
        phone.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
