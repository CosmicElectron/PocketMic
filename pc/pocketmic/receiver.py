"""The receiver core shared by the GUI and the console mode: settings + audio engine + output + network."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import settings as settings_mod
from .audio import AudioEngine
from .net import NetClient, NetEvent, valid_ipv4
from .output import OutputStatus, make_output
from .settings import LATENCY_PRESETS_MS, Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Overrides:
    """Command-line values that apply to this run only and are never written to the settings file."""
    device: Optional[str] = None
    phone_ip: Optional[str] = None
    port: Optional[int] = None
    latency: Optional[str] = None


class Receiver:
    """Owns the running pieces and persists every user-facing change.

    `on_event` receives "status", "phones" or "output" from whichever thread noticed the change;
    UIs must marshal it onto their own thread. It may be set after construction, before start().
    """

    def __init__(self, settings: Settings, settings_file: Path, overrides: Overrides = Overrides(),
                 on_event: Optional[Callable[[str], None]] = None, stats_file: Optional[Path] = None):
        self.settings = settings
        self._settings_file = settings_file
        self._settings_lock = threading.Lock()
        self.on_event = on_event
        self._overrides = overrides
        self._stats_writer = _StatsWriter(self, stats_file) if stats_file else None
        self._device = overrides.device or settings.output_device
        latency = overrides.latency or settings.latency
        self.engine = AudioEngine(LATENCY_PRESETS_MS[latency], settings.gain_percent / 100.0, settings.muted)
        self._latency = latency
        self._output = make_output(self.engine, self._device, self._on_output_status)
        self.net = NetClient(
            self.engine,
            pc_id=settings.pc_id,
            pc_name=settings.pc_name,
            port=overrides.port or settings.port,
            manual_ip=overrides.phone_ip or settings.manual_ip,
            last_phone_id=settings.last_phone_id,
            last_phone_ip=settings.last_phone_ip,
            auto_connect=settings.auto_connect,
            listener=self._on_net_event,
        )

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        self._output.start()
        self.net.start()
        if self._overrides.phone_ip:
            self.net.connect_ip(self._overrides.phone_ip)
        if self._stats_writer is not None:
            self._stats_writer.start()

    def stop(self) -> None:
        if self._stats_writer is not None:
            self._stats_writer.stop()   # the final snapshot still shows the live session
        self.net.stop()                 # sends BYE
        self._output.stop()

    # -- state -------------------------------------------------------------------------------

    @property
    def device(self) -> str:
        return self._device

    @property
    def latency(self) -> str:
        return self._latency

    @property
    def output_status(self) -> OutputStatus:
        return self._output.status

    def snapshot(self) -> dict:
        """Everything worth showing or logging, as plain JSON-able data."""
        st = self.net.status()
        es = self.engine.stats()
        out = self._output.status
        phone = st.phone
        return {
            "time": time.time(),
            "link": st.link.value,
            "notice": st.notice.value,
            "phone": None if phone is None else {
                "name": phone.name, "ip": phone.ip, "port": phone.port, "phone_id": phone.phone_id},
            "session_id": st.session_id,
            "pc_port": st.local_port,
            "phones": [{"name": ph.name, "ip": ph.ip, "state": ph.state.name, "phone_id": ph.phone_id}
                       for ph in self.net.phones()],
            "playing": es.playing,
            "buffer_ms": round(es.buffer_ms, 1),
            "target_ms": es.target_ms,
            "loss_permille": es.loss_permille,
            "received": es.received,
            "duplicates": es.duplicates,
            "late": es.late,
            "concealed": es.concealed,
            "underruns": es.underruns,
            "resyncs": es.resyncs,
            "drift_ppm": round(es.drift_ppm, 1),
            "phone_muted": es.phone_muted,
            "output_peak": round(es.output_peak, 5),
            "output": {"state": out.state, "device": out.device, "message": out.message},
        }

    # -- user actions (any thread) -----------------------------------------------------------

    def update_settings(self, **changes) -> None:
        with self._settings_lock:
            for name, value in changes.items():
                setattr(self.settings, name, value)
            settings_mod.save(self.settings, self._settings_file)

    def set_output_device(self, name: str) -> None:
        self.update_settings(output_device=name)
        if name == self._device and self._output.status.state != "error":
            return
        self._device = name
        self._restart_output()

    def retry_output(self) -> None:
        """Try a failed output again now, rather than at its next (possibly backed-off) retry."""
        if self._output.status.state == "error":
            self._restart_output()

    def _restart_output(self) -> None:
        self._output.stop()
        self._output = make_output(self.engine, self._device, self._on_output_status)
        self._output.start()

    def set_latency(self, preset: str) -> None:
        self._latency = preset
        self.update_settings(latency=preset)
        self.engine.set_latency(LATENCY_PRESETS_MS[preset])

    def set_gain(self, percent: int, save: bool = True) -> None:
        """`save=False` while a slider is being dragged; the final value is saved once."""
        percent = max(0, min(settings_mod.MAX_GAIN_PERCENT, int(percent)))
        self.engine.set_gain(percent / 100.0)
        if save:
            self.update_settings(gain_percent=percent)

    def set_muted(self, muted: bool) -> None:
        self.engine.set_muted(muted)
        self.update_settings(muted=muted)

    def set_auto_connect(self, enabled: bool) -> None:
        self.net.set_auto_connect(enabled)
        self.update_settings(auto_connect=enabled)

    def connect_phone(self, phone_id: int) -> None:
        self.net.connect_phone(phone_id)

    def connect_ip(self, ip: str) -> None:
        """Connect to a typed address and remember it for discovery."""
        ip = ip.strip()
        if not valid_ipv4(ip):
            raise ValueError(f"{ip!r} is not an IPv4 address")
        self.net.set_manual_ip(ip)
        self.net.connect_ip(ip)
        self.update_settings(manual_ip=ip)

    def disconnect(self) -> None:
        self.net.disconnect()

    # -- callbacks ---------------------------------------------------------------------------

    def _on_net_event(self, event: NetEvent) -> None:
        if event is NetEvent.STATUS:
            st = self.net.status()
            if st.last_phone_id and (st.last_phone_id, st.last_phone_ip) != (
                    self.settings.last_phone_id, self.settings.last_phone_ip):
                self.update_settings(last_phone_id=st.last_phone_id, last_phone_ip=st.last_phone_ip)
        self._emit(event.value)

    def _on_output_status(self, status: OutputStatus) -> None:
        self._emit("output")

    def _emit(self, kind: str) -> None:
        if self.on_event is not None:
            self.on_event(kind)


def _write_json_atomic(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    except OSError as exc:
        log.debug("Could not write %s: %s", path, exc)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        # A reader holding the file open can make the replace fail on Windows; the next write retries.
        log.debug("Could not write %s: %s", path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


class _StatsWriter(threading.Thread):
    """Writes Receiver.snapshot() to a JSON file every second (--stats-file, for automated tests)."""

    INTERVAL_S = 1.0

    def __init__(self, receiver: Receiver, path: Path):
        super().__init__(name="pocketmic-stats", daemon=True)
        self._receiver = receiver
        self._path = path
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.INTERVAL_S):
            _write_json_atomic(self._path, self._receiver.snapshot())

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=2)
        final = self._receiver.snapshot()
        final["stopped"] = True
        _write_json_atomic(self._path, final)
