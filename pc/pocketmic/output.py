"""Where rendered audio goes: a PyAudio output device (normally VB-Audio "CABLE Input") or a null sink.

Both pull from AudioEngine.render() on their own clock. DeviceOutput supervises its stream and
reopens it when it fails, so a vanished device never crashes the receiver.

PortAudio's initialisation and termination are not thread-safe (they rebuild global device
tables), and the UI lists devices on its own thread while the supervisor reopens the stream, so
every PortAudio instance is created, used to open or enumerate, and terminated under _PA_LOCK.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pyaudio

from .audio import AudioEngine

log = logging.getLogger(__name__)

_PA_LOCK = threading.Lock()

NULL_DEVICE = "null"
HOST_API_PREFERENCE = ("Windows WASAPI", "Windows DirectSound", "MME")
HOST_API_SHORT = {"Windows WASAPI": "WASAPI", "Windows DirectSound": "DirectSound", "MME": "MME"}
MME_NAME_LIMIT = 31          # MME truncates device names to 31 characters
RETRY_INTERVAL_S = 2.0
MISSING_RETRY_MAX_S = 30.0   # while the device is absent, look for it less and less often, up to this
CALLBACK_STALL_S = 1.0       # no callback for this long means the device has gone away
BLOCK_SECONDS = 0.01


@dataclass(frozen=True)
class OutputDevice:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class OutputStatus:
    state: str               # "starting" | "running" | "error" | "stopped"
    device: str = ""         # what is actually open, e.g. "CABLE Input (...) [WASAPI, 48000 Hz, 2 ch]"
    message: str = ""


def name_matches(query: str, name: str) -> bool:
    """Case-insensitive substring match that also accepts MME's truncated copy of a long name."""
    q, n = query.strip().casefold(), name.casefold()
    if not q:
        return False
    return q in n or (len(name) >= MME_NAME_LIMIT and q.startswith(n))


def _enumerate(pa) -> list[OutputDevice]:
    apis = {}
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        apis[info["index"]] = info["name"]
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        api = apis.get(info["hostApi"], "")
        if info["maxOutputChannels"] > 0 and api in HOST_API_PREFERENCE:
            devices.append(OutputDevice(i, info["name"], api, int(info["defaultSampleRate"]),
                                        int(info["maxOutputChannels"])))
    return devices


def list_output_devices() -> list[OutputDevice]:
    """All output devices on the supported host APIs, in host-API preference order."""
    with _PA_LOCK:
        pa = pyaudio.PyAudio()
        try:
            devices = _enumerate(pa)
        finally:
            pa.terminate()
    return sorted(devices, key=lambda d: HOST_API_PREFERENCE.index(d.host_api))


def device_choices(devices: list[OutputDevice]) -> list[str]:
    """One name per physical device for the UI, preferring the untruncated WASAPI/DirectSound names."""
    names: list[str] = []
    for dev in sorted(devices, key=lambda d: HOST_API_PREFERENCE.index(d.host_api)):
        if not any(name_matches(dev.name, existing) or name_matches(existing, dev.name) for existing in names):
            names.append(dev.name)
    return names


def candidates(devices: list[OutputDevice], query: str) -> list[OutputDevice]:
    """Devices matching `query`, best host API first (WASAPI, then DirectSound, then MME)."""
    matching = [d for d in devices if name_matches(query, d.name)]
    return sorted(matching, key=lambda d: HOST_API_PREFERENCE.index(d.host_api))


class NullOutput:
    """Consumes audio at real-time pace on a timer thread and discards it (tests, headless runs)."""

    def __init__(self, engine: AudioEngine, rate: int = 48000,
                 on_status: Optional[Callable[[OutputStatus], None]] = None):
        self._engine = engine
        self._rate = rate
        self._block = int(rate * BLOCK_SECONDS)
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = OutputStatus("stopped")

    def start(self) -> None:
        self._engine.set_output_rate(self._rate)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pocketmic-null-output", daemon=True)
        self._thread.start()
        self._set_status(OutputStatus("running", f"null output [{self._rate} Hz]"))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._set_status(OutputStatus("stopped"))

    def _set_status(self, status: OutputStatus) -> None:
        self.status = status
        if self._on_status is not None:
            self._on_status(status)

    def _run(self) -> None:
        start = time.perf_counter()
        rendered = 0
        while not self._stop.wait(0.005):
            due = int((time.perf_counter() - start) * self._rate) - rendered
            if due > self._rate // 2:
                # We were starved of CPU; like a real device, skip ahead instead of racing to catch up.
                rendered += due - self._block
                due = self._block
            while due >= self._block:
                self._engine.render(self._block)
                rendered += self._block
                due -= self._block


class DeviceOutput:
    """PyAudio callback stream on the device matching `query`, supervised and reopened on failure."""

    def __init__(self, engine: AudioEngine, query: str,
                 on_status: Optional[Callable[[OutputStatus], None]] = None):
        self._engine = engine
        self._query = query
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._channels = 2
        self._last_callback = 0.0
        self._callback_error: Optional[str] = None
        self.status = OutputStatus("stopped")

    def start(self) -> None:
        self._stop.clear()
        self._set_status(OutputStatus("starting", message=f"Opening {self._query}"))
        self._thread = threading.Thread(target=self._supervise, name="pocketmic-output", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._set_status(OutputStatus("stopped"))

    def _set_status(self, status: OutputStatus) -> None:
        if status != self.status:
            if status.state == "error":
                log.warning("Output: %s", status.message)
            elif status.state == "running":
                log.info("Output running: %s", status.device)
        self.status = status
        if self._on_status is not None:
            self._on_status(status)

    def _callback(self, in_data, frame_count, time_info, status_flags):
        self._last_callback = time.monotonic()
        channels = self._channels
        try:
            mono = self._engine.render(frame_count)
            data = (np.repeat(mono, channels) if channels > 1 else mono).astype(np.float32).tobytes()
        except Exception as exc:  # the audio callback must never raise
            self._callback_error = repr(exc)
            data = bytes(frame_count * channels * 4)
        return data, pyaudio.paContinue

    def _open(self, pa, found: list[OutputDevice]) -> tuple[Optional[object], str]:
        errors = []
        for dev in found:
            # WASAPI shared mode wants the device's own mix format; the older APIs report up to
            # 16 channels for VB-Cable, where stereo is the sensible choice.
            channels = dev.channels if dev.host_api == "Windows WASAPI" else min(dev.channels, 2)
            self._channels = channels
            self._engine.set_output_rate(dev.sample_rate)
            try:
                self._last_callback = time.monotonic()
                stream = pa.open(format=pyaudio.paFloat32, channels=channels, rate=dev.sample_rate,
                                 output=True, output_device_index=dev.index,
                                 frames_per_buffer=int(dev.sample_rate * BLOCK_SECONDS),
                                 stream_callback=self._callback)
            except Exception as exc:
                errors.append(f"{HOST_API_SHORT[dev.host_api]}: {exc}")
                continue
            short = HOST_API_SHORT[dev.host_api]
            self._set_status(OutputStatus(
                "running", f"{dev.name} [{short}, {dev.sample_rate} Hz, {channels} ch]"))
            return stream, ""
        return None, "Could not open " + found[0].name + " (" + "; ".join(errors) + ")"

    @staticmethod
    def _close(stream, pa) -> None:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception as exc:
            log.debug("Ignoring error while closing the stream: %s", exc)
        try:
            if pa is not None:
                pa.terminate()
        except Exception as exc:
            log.debug("Ignoring error while terminating PortAudio: %s", exc)

    def _supervise(self) -> None:
        missing_wait = RETRY_INTERVAL_S
        while not self._stop.is_set():
            pa = stream = None
            missing = False
            try:
                with _PA_LOCK:
                    # A fresh PortAudio instance per attempt: its device list is fixed at
                    # initialisation, so this is how a re-plugged or newly installed device gets noticed.
                    pa = pyaudio.PyAudio()
                    found = candidates(_enumerate(pa), self._query)
                    if found:
                        stream, error = self._open(pa, found)
                    else:
                        missing = True
                        error = f'No output device matching "{self._query}" was found'
                if stream is None:
                    self._set_status(OutputStatus("error", message=error))
                else:
                    error = self._watch(stream)
                    if error:
                        self._set_status(OutputStatus("error", message=error))
            except Exception as exc:
                self._set_status(OutputStatus("error", message=f"Audio output failed: {exc}"))
            finally:
                with _PA_LOCK:
                    self._close(stream, pa)
            if missing:
                # Each look costs a full PortAudio initialisation (over a second of CPU with many
                # devices), so a device that stays absent is looked for less and less often.
                wait, missing_wait = missing_wait, min(missing_wait * 2, MISSING_RETRY_MAX_S)
            else:
                wait = missing_wait = RETRY_INTERVAL_S
            self._stop.wait(wait)

    def _watch(self, stream) -> str:
        """Block while the stream is healthy; return why it stopped ('' when asked to stop)."""
        while not self._stop.wait(0.25):
            if self._callback_error:
                log.error("Audio callback error: %s", self._callback_error)
                self._callback_error = None
            try:
                active = stream.is_active()
            except Exception as exc:
                return f"Output device failed: {exc}"
            if not active:
                return "Output device stopped"
            if time.monotonic() - self._last_callback > CALLBACK_STALL_S:
                return "Output device stopped responding (unplugged or reconfigured?)"
        return ""


def make_output(engine: AudioEngine, query: str,
                on_status: Optional[Callable[[OutputStatus], None]] = None):
    if query.strip().casefold() == NULL_DEVICE:
        return NullOutput(engine, on_status=on_status)
    return DeviceOutput(engine, query, on_status=on_status)
