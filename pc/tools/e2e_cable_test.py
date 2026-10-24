"""End-to-end check through the real VB-Audio cable (the only test that touches real devices).

    simulated phone --UDP--> receiver (headless, --device "CABLE Input") --> CABLE Input
                                                            CABLE Output --> this script records

The simulated phone streams a quiet 1 kHz tone (-30 dBFS) for at most 5 seconds; apps listening on
CABLE Output would hear only that. The recording must show ~1000 Hz, about -30 dBFS, and no silent
gap longer than 15 ms after the first 0.5 s. Settings go to a temporary folder, never %APPDATA%.
Exit code 0 = pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pyaudio

PC_DIR = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PC_DIR))

from tools.fake_phone import FakePhone  # noqa: E402

TONE_HZ = 1000.0
TONE_DBFS = -30.0
STREAM_SECONDS = 4.5            # hard limit from the test rules: 5 s
CAPTURE_DEVICE = "CABLE Output"
SETTLE_S = 0.5
MAX_GAP_MS = 15
LEVEL_TOLERANCE_DB = 3.0
FREQ_TOLERANCE_HZ = 2.0


def find_capture_device(pa: pyaudio.PyAudio) -> dict:
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["hostApi"] == wasapi and info["maxInputChannels"] > 0 and CAPTURE_DEVICE in info["name"]:
            return info
    raise SystemExit(f'No WASAPI input device named "{CAPTURE_DEVICE}" (is VB-Audio Virtual Cable installed?)')


class Recorder:
    def __init__(self, pa: pyaudio.PyAudio, info: dict):
        self.channels = min(2, int(info["maxInputChannels"]))
        self.rate = int(info["defaultSampleRate"])
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._stream = pa.open(format=pyaudio.paFloat32, channels=self.channels, rate=self.rate, input=True,
                               input_device_index=info["index"], frames_per_buffer=self.rate // 100,
                               stream_callback=self._callback)

    def _callback(self, in_data, frame_count, time_info, status):
        with self._lock:
            self._chunks.append(in_data)
        return None, pyaudio.paContinue

    def stop(self) -> np.ndarray:
        self._stream.stop_stream()
        self._stream.close()
        with self._lock:
            data = b"".join(self._chunks)
        return np.frombuffer(data, np.float32).reshape(-1, self.channels)


def block_rms(x: np.ndarray, block: int) -> np.ndarray:
    n = len(x) // block
    return np.sqrt(np.mean(x[: n * block].reshape(n, block) ** 2, axis=1))


def longest_run(mask: np.ndarray) -> int:
    best = run = 0
    for value in mask:
        run = run + 1 if value else 0
        best = max(best, run)
    return best


def dominant_frequency(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(np.argmax(spectrum[1:])) + 1
    a, b, c = np.log(spectrum[k - 1:k + 2] + 1e-20)
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * rate / len(x)


def sinad_db(x: np.ndarray, rate: int, freq: float) -> float:
    power = np.abs(np.fft.rfft(x * np.blackman(len(x)))) ** 2
    k = int(round(freq * len(x) / rate))
    signal = power[k - 8:k + 9].sum()
    return 10 * np.log10(signal / max(power[1:].sum() - signal, 1e-30))


def analyse(recording: np.ndarray, rate: int) -> dict:
    mono = recording[:, 0].astype(np.float64)
    expected_rms = 10 ** (TONE_DBFS / 20) / np.sqrt(2)
    ms = rate // 1000
    rms_ms = block_rms(mono, ms)
    present = np.nonzero(rms_ms > 0.25 * expected_rms)[0]
    if len(present) == 0:
        return {"tone_found": False}
    onset, end = present[0], present[-1]
    # Skip the start-up settle time and the final 20 ms (the receiver fades out on BYE).
    first, last = onset + int(SETTLE_S * 1000), end - 20
    body = mono[first * ms:last * ms]
    gaps = rms_ms[first:last] < 0.1 * expected_rms
    rms = float(np.sqrt(np.mean(body ** 2)))
    channel_mismatch = float(np.max(np.abs(recording[first * ms:last * ms, 0] - recording[first * ms:last * ms, -1])))
    return {
        "tone_found": True,
        "tone_seconds": round((end - onset) / 1000, 3),
        "analysed_seconds": round(len(body) / rate, 3),
        "frequency_hz": round(dominant_frequency(body, rate), 2),
        "level_dbfs": round(20 * np.log10(rms * np.sqrt(2)), 2),
        "longest_gap_ms": int(longest_run(gaps)),
        "silent_ms_total": int(gaps.sum()),
        "sinad_db": round(sinad_db(body, rate, TONE_HZ), 1),
        "max_channel_difference": round(channel_mismatch, 6),
    }


def read_stats(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}   # not written yet, or caught mid-replace


def wait_until(condition, timeout: float, process: subprocess.Popen, failure: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline or process.poll() is not None:
            raise SystemExit(failure)
        time.sleep(0.01)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pocketmic-e2e-"))
    stats_file = work / "stats.json"
    env = dict(os.environ, POCKETMIC_SETTINGS=str(work / "settings.json"))

    pa = pyaudio.PyAudio()
    phone = FakePhone(port=0, host="127.0.0.1", name="E2E Phone", freq=TONE_HZ, level_dbfs=TONE_DBFS).start()
    # The phone stays unreachable until the receiver's output device is open, so the whole tone
    # (not just what is left after the device start-up) goes through the cable.
    phone.blackout(3600)
    recorder = None
    receiver = None
    try:
        recorder = Recorder(pa, find_capture_device(pa))
        rate = recorder.rate
        receiver = subprocess.Popen(
            [sys.executable, "-m", "pocketmic", "--nogui", "--device", "CABLE Input", "--phone-ip", "127.0.0.1",
             "--port", str(phone.port), "--duration", "30", "--stats-file", str(stats_file)],
            cwd=PC_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_until(lambda: read_stats(stats_file).get("output", {}).get("state") == "running", 20, receiver,
                   "The receiver could not open CABLE Input.")
        phone.blackout(0)
        wait_until(lambda: phone.session is not None, 10, receiver,
                   "The receiver never connected to the simulated phone.")
        streaming_since = time.monotonic()
        time.sleep(STREAM_SECONDS - 0.6)
        receiver_stats = read_stats(stats_file)
        while time.monotonic() - streaming_since < STREAM_SECONDS:
            time.sleep(0.005)
        phone.stop()                                   # BYE: the tone ends here, < 5 s after it began
        streamed = time.monotonic() - streaming_since
        phone = None
        time.sleep(0.4)
        recording = recorder.stop()
        recorder = None
    finally:
        if recorder is not None:
            recorder.stop()
        if phone is not None:
            phone.stop()
        if receiver is not None and receiver.poll() is None:
            receiver.terminate()                       # the phone is gone; nothing left to say goodbye to
        pa.terminate()

    output, _ = receiver.communicate(timeout=10)
    results = analyse(recording, rate)
    results["streamed_seconds"] = round(streamed, 2)
    results["receiver_output"] = receiver_stats.get("output", {}).get("device", "?")
    for key in ("buffer_ms", "target_ms", "underruns", "concealed", "late", "resyncs", "drift_ppm", "received"):
        results["receiver_" + key] = receiver_stats.get(key)

    checks = {
        "tone present": results.get("tone_found", False),
        f"frequency within {FREQ_TOLERANCE_HZ} Hz of {TONE_HZ:.0f}":
            results.get("tone_found") and abs(results["frequency_hz"] - TONE_HZ) <= FREQ_TOLERANCE_HZ,
        f"level within {LEVEL_TOLERANCE_DB} dB of {TONE_DBFS:.0f} dBFS":
            results.get("tone_found") and abs(results["level_dbfs"] - TONE_DBFS) <= LEVEL_TOLERANCE_DB,
        f"no silent gap > {MAX_GAP_MS} ms": results.get("tone_found") and results["longest_gap_ms"] <= MAX_GAP_MS,
        "tone lasted at most 5 s": streamed <= 5.0,
    }
    print(json.dumps(results, indent=2))
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not all(checks.values()):
        print("\nReceiver output:\n" + output)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
