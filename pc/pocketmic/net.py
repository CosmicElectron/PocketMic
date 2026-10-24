"""PC side of a PocketMic session (PROTOCOL.md, "Behaviour > PC").

One UDP socket, bound to 0.0.0.0:<ephemeral>, carries discovery, control and audio. The PC always
sends first, so every phone packet is a reply on a flow the PC opened and Windows Firewall needs
no inbound rule. All protocol state lives on one network thread; the public methods only queue
commands for it, and readers get immutable snapshots.
"""

from __future__ import annotations

import ctypes
import ipaddress
import logging
import queue
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Optional

import psutil

from . import protocol as p
from .audio import AudioEngine
from .settings import new_id

log = logging.getLogger(__name__)

DISCOVER_INTERVAL_SEARCHING_S = 1.0
DISCOVER_INTERVAL_CONNECTED_S = 5.0
PHONE_EXPIRY_S = 5.0
CONNECT_RETRY_S = 0.5
CONNECT_TRIES = 3
KEEPALIVE_INTERVAL_S = 0.5
AUDIO_TIMEOUT_S = 2.0
RETRY_BACKOFF_S = 3.0          # after a REJECT or an unanswered CONNECT, before auto-connect tries that phone again
INTERFACE_REFRESH_S = 5.0
SOCKET_REOPEN_S = 1.0
RECV_BUFFER_BYTES = 1 << 20    # absorbs the burst a phone sends after a Wi-Fi stall
POLL_S = 0.05                  # upper bound on how long a queued command waits

SIO_UDP_CONNRESET = 0x9800000C

MIN_RATE, MAX_RATE = 8000, 192000
MAX_CHANNELS = 2
MAX_FRAME_SAMPLES = 4096


class Link(Enum):
    SEARCHING = "searching"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class Notice(Enum):
    """Why the last connection attempt or session ended, for the UI."""
    NONE = "none"
    BUSY = "busy"
    MIC_UNAVAILABLE = "mic_unavailable"
    BAD_VERSION = "bad_version"
    NO_ANSWER = "no_answer"
    CONNECTION_LOST = "connection_lost"
    PHONE_ENDED = "phone_ended"
    NETWORK_ERROR = "network_error"
    UNSUPPORTED_FORMAT = "unsupported_format"
    USER_DISCONNECTED = "user_disconnected"   # auto-connect is paused until the user connects again


_REJECT_NOTICE = {
    p.RejectReason.BUSY: Notice.BUSY,
    p.RejectReason.MIC_UNAVAILABLE: Notice.MIC_UNAVAILABLE,
    p.RejectReason.BAD_VERSION: Notice.BAD_VERSION,
}


@dataclass(frozen=True)
class Phone:
    phone_id: int            # 0 while unknown (connected by IP before the phone announced itself)
    name: str
    ip: str
    port: int
    state: p.PhoneState
    sample_rate: int = p.SAMPLE_RATE
    channels: int = p.CHANNELS
    last_seen: float = 0.0

    def same_as(self, other: "Phone") -> bool:
        if self.phone_id and other.phone_id:
            return self.phone_id == other.phone_id
        return (self.ip, self.port) == (other.ip, other.port)


@dataclass(frozen=True)
class NetStatus:
    link: Link = Link.SEARCHING
    phone: Optional[Phone] = None    # the phone being connected to / streaming
    session_id: int = 0
    notice: Notice = Notice.NONE
    last_phone_id: int = 0
    last_phone_ip: str = ""
    auto_connect: bool = True
    local_port: int = 0


class NetEvent(Enum):
    STATUS = "status"
    PHONES = "phones"


def valid_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text.strip())
        return True
    except ValueError:
        return False


def broadcast_addresses() -> list[str]:
    """255.255.255.255 plus the directed broadcast of every usable local IPv4 interface.

    Windows sends the limited broadcast out of one interface only, so the directed ones are what
    reach a phone on a secondary adapter.
    """
    result = ["255.255.255.255"]
    try:
        interfaces = psutil.net_if_addrs()
    except Exception as exc:
        log.debug("Could not list network interfaces: %s", exc)
        return result
    for addrs in interfaces.values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            try:
                ip = ipaddress.IPv4Address(a.address)
                net = ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or net.prefixlen >= 31:
                continue
            addr = str(net.broadcast_address)
            if addr not in result:
                result.append(addr)
    return result


def disable_udp_connreset(sock: socket.socket) -> None:
    """Stop Windows from failing recvfrom() with WinError 10054 after an ICMP port-unreachable.

    Python's socket.ioctl() does not accept SIO_UDP_CONNRESET, so call WSAIoctl directly.
    """
    if sys.platform != "win32":
        return
    ws2 = ctypes.windll.ws2_32
    flag = ctypes.c_uint32(0)
    returned = ctypes.c_uint32(0)
    ws2.WSAIoctl.argtypes = [ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.c_void_p, ctypes.c_void_p]
    if ws2.WSAIoctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(flag), ctypes.sizeof(flag),
                    None, 0, ctypes.byref(returned), None, None) != 0:
        log.debug("SIO_UDP_CONNRESET failed: WSA error %d", ws2.WSAGetLastError())


class NetClient:
    def __init__(self, engine: AudioEngine, *, pc_id: int, pc_name: str, port: int = p.DEFAULT_PORT,
                 manual_ip: str = "", last_phone_id: int = 0, last_phone_ip: str = "",
                 auto_connect: bool = True, broadcast: bool = True,
                 listener: Optional[Callable[[NetEvent], None]] = None):
        self._engine = engine
        self._pc_id = pc_id
        self._pc_name = pc_name
        self._port = port
        self._manual_ip = manual_ip if valid_ipv4(manual_ip) else ""
        self._last_phone_id = last_phone_id
        self._last_phone_ip = last_phone_ip if valid_ipv4(last_phone_ip) else ""
        self._auto_connect = auto_connect
        self._auto_suspended = False       # set by a user disconnect, cleared by a user connect
        self._broadcast = broadcast
        self._listener = listener

        self._commands: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._reopen_at = 0.0

        self._link = Link.SEARCHING
        self._notice = Notice.NONE
        self._target: Optional[Phone] = None
        self._session_id = 0
        self._channels = p.CHANNELS
        self._connect_tries = 0
        self._next_connect = 0.0
        self._next_discover = 0.0
        self._next_keepalive = 0.0
        self._last_audio = 0.0
        self._highest_seq: Optional[int] = None
        self._phones: dict[int, Phone] = {}
        self._backoff: dict[tuple[str, int], float] = {}
        # Phones whose session just ended without the user asking (BYE, timeout, socket loss):
        # auto-connect waits for a fresh ANNOUNCE, i.e. proof the phone is reachable and on again,
        # instead of firing CONNECTs into an outage or at a phone that was just switched off.
        self._awaiting_announce: set[tuple[str, int]] = set()
        self._broadcast_targets: list[str] = []
        self._next_interface_refresh = 0.0

        self._status = NetStatus(auto_connect=auto_connect, last_phone_id=last_phone_id,
                                 last_phone_ip=self._last_phone_ip)
        self._phones_snapshot: tuple[Phone, ...] = ()

    # -- public API (any thread) -------------------------------------------------------------

    def start(self) -> None:
        self._open_socket()
        self._publish_status()
        self._thread = threading.Thread(target=self._run, name="pocketmic-net", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Send BYE if a session is active, then close the socket."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def status(self) -> NetStatus:
        return self._status

    def phones(self) -> tuple[Phone, ...]:
        return self._phones_snapshot

    @property
    def local_port(self) -> int:
        return self._status.local_port

    def connect_phone(self, phone_id: int) -> None:
        self._commands.put(lambda: self._user_connect_phone(phone_id))

    def connect_ip(self, ip: str) -> None:
        if not valid_ipv4(ip):
            raise ValueError(f"not an IPv4 address: {ip!r}")
        self._commands.put(lambda: self._user_connect_ip(ip.strip()))

    def disconnect(self) -> None:
        self._commands.put(self._user_disconnect)

    def set_manual_ip(self, ip: str) -> None:
        ip = ip.strip()
        if ip and not valid_ipv4(ip):
            raise ValueError(f"not an IPv4 address: {ip!r}")
        self._commands.put(lambda: self._set_manual_ip(ip))

    def set_auto_connect(self, enabled: bool) -> None:
        self._commands.put(lambda: self._set_auto_connect(enabled))

    # -- network thread ----------------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._run_commands()
                    self._timers(time.monotonic())
                    self._receive(self._wait_time(time.monotonic()))
                except Exception:
                    # A bug must not leave the receiver deaf for the rest of the run.
                    log.exception("Network loop error")
                    self._stop.wait(POLL_S)
        finally:
            self._end_session(send_bye=True, notice=Notice.NONE, publish=False)
            self._close_socket()

    def _run_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                command()
            except Exception:
                log.exception("Network command failed")

    def _wait_time(self, now: float) -> float:
        deadlines = [self._next_discover]
        if self._link is Link.CONNECTING:
            deadlines.append(self._next_connect)
        elif self._link is Link.CONNECTED:
            deadlines += [self._next_keepalive, self._last_audio + AUDIO_TIMEOUT_S]
        return min(POLL_S, max(0.0, min(deadlines) - now))

    def _timers(self, now: float) -> None:
        if self._sock is None and now >= self._reopen_at:
            self._open_socket()
        if now >= self._next_interface_refresh:
            self._broadcast_targets = broadcast_addresses() if self._broadcast else []
            self._next_interface_refresh = now + INTERFACE_REFRESH_S
        if now >= self._next_discover:
            self._send_discover()
            interval = (DISCOVER_INTERVAL_CONNECTED_S if self._link is Link.CONNECTED
                        else DISCOVER_INTERVAL_SEARCHING_S)
            self._next_discover = now + interval
        self._expire_phones(now)

        if self._link is Link.CONNECTING and now >= self._next_connect:
            if self._connect_tries < CONNECT_TRIES:
                self._send_connect(now)
            else:
                log.info("No answer from %s:%d", self._target.ip, self._target.port)
                self._backoff[(self._target.ip, self._target.port)] = now + RETRY_BACKOFF_S
                self._end_session(send_bye=False, notice=Notice.NO_ANSWER)
        elif self._link is Link.CONNECTED:
            if now - self._last_audio > AUDIO_TIMEOUT_S:
                log.info("No audio for %.0f s: connection lost", AUDIO_TIMEOUT_S)
                self._end_session(send_bye=False, notice=Notice.CONNECTION_LOST)
            elif now >= self._next_keepalive:
                self._send_keepalive()
                self._next_keepalive = now + KEEPALIVE_INTERVAL_S
        self._maybe_auto_connect(now)

    def _receive(self, timeout: float) -> None:
        sock = self._sock
        if sock is None:
            self._stop.wait(timeout)
            return
        try:
            readable, _, _ = select.select([sock], [], [], timeout)
        except (OSError, ValueError) as exc:
            self._socket_failed(exc)
            return
        if not readable:
            return
        for _ in range(256):
            try:
                data, addr = sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except ConnectionResetError:
                continue  # ICMP port unreachable from an earlier send; harmless for UDP
            except OSError as exc:
                self._socket_failed(exc)
                return
            self._handle(data, addr, time.monotonic())

    # -- socket ------------------------------------------------------------------------------

    def _open_socket(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
            except OSError:
                pass
            disable_udp_connreset(sock)
            sock.bind(("0.0.0.0", 0))
            sock.setblocking(False)
        except OSError as exc:
            log.warning("Could not open the UDP socket: %s", exc)
            self._reopen_at = time.monotonic() + SOCKET_REOPEN_S
            return
        self._sock = sock
        self._next_discover = 0.0
        self._status = replace(self._status, local_port=sock.getsockname()[1])
        log.info("Listening for phone replies on UDP port %d", self._status.local_port)

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _socket_failed(self, exc: Exception) -> None:
        log.warning("UDP socket failed (%s); recreating it", exc)
        self._close_socket()
        self._reopen_at = time.monotonic() + SOCKET_REOPEN_S
        if self._link is not Link.SEARCHING:
            # The phone streams to the old port, so the session is gone; auto-connect will resume it.
            self._end_session(send_bye=False, notice=Notice.NETWORK_ERROR)
        else:
            self._publish_status()

    def _send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(data, addr)
        except OSError as exc:
            # e.g. no route while the network is changing; the timers simply try again.
            log.debug("sendto %s failed: %s", addr, exc)

    # -- outgoing ----------------------------------------------------------------------------

    def _send_discover(self) -> None:
        packet = p.encode(p.Discover(self._pc_id, self._pc_name))
        targets = list(self._broadcast_targets)
        for ip in (self._manual_ip, self._last_phone_ip):
            if ip and ip not in targets:
                targets.append(ip)
        for ip in targets:
            self._send(packet, (ip, self._port))

    def _send_connect(self, now: float) -> None:
        self._connect_tries += 1
        self._next_connect = now + CONNECT_RETRY_S
        self._send(p.encode(p.Connect(self._session_id, self._pc_id, self._pc_name)),
                   (self._target.ip, self._target.port))

    def _send_keepalive(self) -> None:
        stats = self._engine.stats()
        self._send(p.encode(p.Keepalive(self._session_id,
                                        self._highest_seq if self._highest_seq is not None else 0,
                                        min(stats.loss_permille, 1000),
                                        min(int(round(stats.buffer_ms)), 0xFFFF))),
                   (self._target.ip, self._target.port))

    # -- incoming ----------------------------------------------------------------------------

    def _handle(self, data: bytes, addr: tuple[str, int], now: float) -> None:
        msg = p.decode(data, self._channels)
        if isinstance(msg, p.Audio):
            if self._link is Link.CONNECTED and msg.session_id == self._session_id:
                self._last_audio = now
                if self._highest_seq is None or p.seq_newer(msg.seq, self._highest_seq):
                    self._highest_seq = msg.seq
                self._engine.push(msg.seq, msg.pcm, msg.muted)
        elif isinstance(msg, p.Announce):
            self._on_announce(msg, addr, now)
        elif isinstance(msg, p.Accept):
            self._on_accept(msg, addr, now)
        elif isinstance(msg, p.Reject):
            if self._link is Link.CONNECTING and msg.session_id == self._session_id:
                log.info("Phone rejected the connection: %s", msg.reason.name)
                self._backoff[(self._target.ip, self._target.port)] = now + RETRY_BACKOFF_S
                self._end_session(send_bye=False, notice=_REJECT_NOTICE[msg.reason])
        elif isinstance(msg, p.Bye):
            if self._link is not Link.SEARCHING and msg.session_id == self._session_id:
                log.info("Phone ended the session")
                self._end_session(send_bye=False, notice=Notice.PHONE_ENDED)
        # Anything else (malformed, or PC-to-phone types) is ignored.

    def _on_announce(self, msg: p.Announce, addr: tuple[str, int], now: float) -> None:
        phone = Phone(msg.phone_id, msg.phone_name, addr[0], addr[1], msg.state,
                      msg.sample_rate, msg.channels, now)
        old = self._phones.get(msg.phone_id)
        self._phones[msg.phone_id] = phone
        self._awaiting_announce.discard((phone.ip, phone.port))
        if old is None or replace(old, last_seen=now) != phone:
            self._publish_phones()
        target = self._target
        if target is not None and (target.ip, target.port) == (phone.ip, phone.port):
            if target.phone_id != phone.phone_id or target.name != phone.name:
                self._target = replace(target, phone_id=phone.phone_id, name=phone.name)
                if self._link is Link.CONNECTED:
                    self._last_phone_id = phone.phone_id
                self._publish_status()

    def _on_accept(self, msg: p.Accept, addr: tuple[str, int], now: float) -> None:
        if self._link is not Link.CONNECTING or msg.session_id != self._session_id:
            return
        if not (MIN_RATE <= msg.sample_rate <= MAX_RATE and 1 <= msg.channels <= MAX_CHANNELS
                and 1 <= msg.frame_samples <= MAX_FRAME_SAMPLES):
            log.warning("Phone offered an unsupported format: %s", msg)
            self._backoff[(self._target.ip, self._target.port)] = now + RETRY_BACKOFF_S
            self._end_session(send_bye=True, notice=Notice.UNSUPPORTED_FORMAT)
            return
        self._target = replace(self._target, ip=addr[0], port=addr[1])
        self._channels = msg.channels
        self._engine.start_session(msg.sample_rate, msg.channels, msg.frame_samples)
        self._link = Link.CONNECTED
        self._notice = Notice.NONE
        self._last_audio = now
        self._next_keepalive = now + KEEPALIVE_INTERVAL_S
        self._highest_seq = None
        self._last_phone_ip = addr[0]
        if self._target.phone_id:
            self._last_phone_id = self._target.phone_id
        log.info("Connected to %s (%s:%d), session %08x", self._target.name, addr[0], addr[1],
                 self._session_id)
        self._publish_status()

    # -- session management ------------------------------------------------------------------

    def _start_connect(self, phone: Phone, now: float) -> None:
        if self._link is not Link.SEARCHING:
            self._end_session(send_bye=True, notice=Notice.NONE, publish=False)
        self._session_id = new_id()
        self._target = phone
        self._link = Link.CONNECTING
        self._notice = Notice.NONE
        self._connect_tries = 0
        log.info("Connecting to %s (%s:%d)", phone.name, phone.ip, phone.port)
        self._send_connect(now)
        self._publish_status()

    def _end_session(self, *, send_bye: bool, notice: Notice, publish: bool = True) -> None:
        if self._link is Link.SEARCHING:
            return
        if send_bye and self._target is not None:
            self._send(p.encode(p.Bye(self._session_id)), (self._target.ip, self._target.port))
        if self._link is Link.CONNECTED:
            self._engine.end_session()
        if self._target is not None and notice in (Notice.PHONE_ENDED, Notice.CONNECTION_LOST,
                                                   Notice.NETWORK_ERROR):
            self._awaiting_announce.add((self._target.ip, self._target.port))
        self._link = Link.SEARCHING
        self._notice = notice
        self._target = None
        self._session_id = 0
        self._channels = p.CHANNELS
        # The last DISCOVER may have been scheduled 5 s ahead while connected. Ask again now:
        # auto-connect waits for a fresh ANNOUNCE, and the phone list should show the phone free.
        self._next_discover = min(self._next_discover, time.monotonic())
        if publish:
            self._publish_status()

    def _maybe_auto_connect(self, now: float) -> None:
        if self._link is not Link.SEARCHING or not self._auto_connect or self._auto_suspended:
            return

        def available(phone: Phone) -> bool:
            # STREAMING means "streaming to this pc_id" (e.g. after our restart): reconnecting is right.
            address = (phone.ip, phone.port)
            return (phone.state in (p.PhoneState.READY, p.PhoneState.STREAMING)
                    and self._backoff.get(address, 0.0) <= now
                    and address not in self._awaiting_announce)

        if self._last_phone_id:
            choice = self._phones.get(self._last_phone_id)
            if choice is not None and not available(choice):
                choice = None
        else:
            choice = next((ph for ph in self._phones.values()
                           if ph.state is p.PhoneState.READY and available(ph)), None)
        if choice is not None:
            self._start_connect(choice, now)

    def _expire_phones(self, now: float) -> None:
        # While connected, DISCOVER goes out only every 5 s, so allow one extra interval of slack
        # before declaring a phone gone; otherwise the list would flicker on every round.
        expiry = max(PHONE_EXPIRY_S, (DISCOVER_INTERVAL_CONNECTED_S if self._link is Link.CONNECTED
                                      else DISCOVER_INTERVAL_SEARCHING_S) + 1.0)
        target = self._target if self._link is Link.CONNECTED else None
        gone = [pid for pid, ph in self._phones.items()
                if now - ph.last_seen > expiry and not (target is not None and ph.same_as(target))]
        for pid in gone:
            del self._phones[pid]
        if gone:
            self._publish_phones()

    # -- commands ----------------------------------------------------------------------------

    def _user_connect_phone(self, phone_id: int) -> None:
        phone = self._phones.get(phone_id)
        if phone is None:
            log.info("Phone %08x is no longer listed", phone_id)
            return
        self._user_connect(phone)

    def _user_connect_ip(self, ip: str) -> None:
        known = next((ph for ph in self._phones.values() if ph.ip == ip and ph.port == self._port), None)
        self._user_connect(known or Phone(0, ip, ip, self._port, p.PhoneState.READY))

    def _user_connect(self, phone: Phone) -> None:
        # An explicit request overrides every automatic hold-off.
        self._auto_suspended = False
        self._backoff.pop((phone.ip, phone.port), None)
        self._awaiting_announce.discard((phone.ip, phone.port))
        if self._link is not Link.SEARCHING and self._target is not None and self._target.same_as(phone):
            return  # already connected or connecting to it: a new session would only cause a dropout
        self._start_connect(phone, time.monotonic())

    def _user_disconnect(self) -> None:
        self._auto_suspended = True
        if self._link is Link.SEARCHING:
            self._notice = Notice.USER_DISCONNECTED
            self._publish_status()
        else:
            self._end_session(send_bye=True, notice=Notice.USER_DISCONNECTED)

    def _set_manual_ip(self, ip: str) -> None:
        self._manual_ip = ip
        self._next_discover = 0.0   # ask the new address right away

    def _set_auto_connect(self, enabled: bool) -> None:
        self._auto_connect = enabled
        if enabled:
            self._auto_suspended = False
            if self._notice is Notice.USER_DISCONNECTED:
                self._notice = Notice.NONE
        self._publish_status()

    # -- snapshots ---------------------------------------------------------------------------

    def _publish_status(self) -> None:
        self._status = NetStatus(
            link=self._link,
            phone=self._target,
            session_id=self._session_id,
            notice=self._notice,
            last_phone_id=self._last_phone_id,
            last_phone_ip=self._last_phone_ip,
            auto_connect=self._auto_connect,
            local_port=self._status.local_port,
        )
        self._notify(NetEvent.STATUS)

    def _publish_phones(self) -> None:
        self._phones_snapshot = tuple(self._phones.values())
        self._notify(NetEvent.PHONES)

    def _notify(self, event: NetEvent) -> None:
        if self._listener is None:
            return
        try:
            self._listener(event)
        except Exception:
            log.exception("Network event listener failed")
