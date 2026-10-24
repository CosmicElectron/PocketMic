"""Audio engine: jitter buffer, clock-drift-tracking resampler, gain/limiter and level meter.

Data flow: the network thread calls AudioEngine.push() for every AUDIO packet; the output device's
callback (or the null sink's timer thread) calls AudioEngine.render() to pull audio at the device
rate. The two sides share only the jitter buffer, guarded by one lock that is held for
microseconds; all filtering happens outside it, so render() never waits on the network.
"""

from __future__ import annotations

import functools
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .protocol import seq_add, seq_diff, seq_newer

FADE_SAMPLES = 240           # 5 ms at 48 kHz: fade-in after a gap, crossfade after skipping audio
RESYNC_HEADROOM_MS = 100     # level above target + max(this, target) -> drop the oldest audio at once
REVIEW_WINDOW_S = 1.0        # the buffer's low point is reviewed over windows this long
SURPLUS_MIN_FRAMES = 2       # unused audio at least this far above target is dropped
MAX_EXTRA_MS = 100           # each underrun deepens the buffer by one frame, up to this much
RELAX_MARGIN_FRAMES = 2      # an underrun's frame comes off if the low point would keep this many
RECENT_SEQ_MEMORY = 1024     # how many recent seq numbers are remembered to tell duplicates from late packets
LOSS_WINDOW_MARKS = 10       # loss_permille covers the last 10 marks...
LOSS_MARK_PACKETS = 50       # ...taken every 50 expected packets (0.5 s): a 5 s window


class JitterBuffer:
    """Orders packets by sequence number and hands out one frame at a time, concealing gaps.

    Not thread-safe by itself: AudioEngine serialises access with its lock.
    """

    def __init__(self, frame_samples: int, target_samples: int, overflow_samples: int,
                 max_extra_samples: int = 0):
        self.frame_samples = frame_samples
        self.target_samples = target_samples   # what the user chose
        self.overflow_samples = overflow_samples
        self.max_extra_frames = max_extra_samples // frame_samples
        self.extra_frames = 0                  # added to the target after underruns (see next_frame)
        self._frames: dict[int, np.ndarray] = {}
        self._play_seq: Optional[int] = None   # next frame to hand out
        self._highest: Optional[int] = None
        self._first: Optional[int] = None
        self.playing = False
        self._fresh = True                     # the next start is a first start (see _try_start)
        self._recent: deque[int] = deque()
        self._recent_set: set[int] = set()
        self._last_frame = np.zeros(frame_samples, np.float32)   # last frame handed out
        self._concealed_run = 0                # consecutive frames concealed so far
        self._fade_in = True                   # the output went silent; the next frame fades in
        self._xfade_from: Optional[np.ndarray] = None   # after a skip: what would have played next
        self._trim_pending = False             # a lowered target is applied on the next arrival
        self.received = 0
        self.duplicates = 0
        self.late = 0
        self.concealed = 0
        self.underruns = 0
        self.resyncs = 0
        self._loss_marks: deque[tuple[int, int]] = deque(maxlen=LOSS_WINDOW_MARKS + 1)

    # -- intake (network side) ---------------------------------------------------------------

    def push(self, seq: int, samples: np.ndarray) -> str:
        """Returns 'ok', 'duplicate' or 'late'."""
        if seq in self._recent_set:
            self.duplicates += 1
            return "duplicate"
        self._remember(seq)
        self.received += 1
        if self._first is None or seq_newer(self._first, seq):
            self._first = seq
        if self._highest is None or seq_newer(seq, self._highest):
            self._highest = seq
        self._mark_loss_window()
        if self._play_seq is not None and seq_diff(seq, self._play_seq) < 0:
            self.late += 1
            return "late"
        self._frames[seq] = samples
        # Bursty delivery peaks at up to twice the target just after a burst, so a deep buffer
        # gets proportionally more headroom before its peak counts as a backlog.
        target = self.effective_target
        overflow = self.level_frames() * self.frame_samples > target + max(self.overflow_samples, target)
        if overflow or self._trim_pending:
            # Trimming right after an arrival leaves exactly the target once playback catches up.
            self._trim_to_target()
            self._trim_pending = False
            if overflow and self.playing:
                self.resyncs += 1
        return "ok"

    def _remember(self, seq: int) -> None:
        self._recent.append(seq)
        self._recent_set.add(seq)
        if len(self._recent) > RECENT_SEQ_MEMORY:
            self._recent_set.discard(self._recent.popleft())

    def _expected(self) -> int:
        return seq_diff(self._highest, self._first) + 1

    def _mark_loss_window(self) -> None:
        expected = self._expected()
        if not self._loss_marks or expected - self._loss_marks[-1][0] >= LOSS_MARK_PACKETS:
            self._loss_marks.append((expected, self.received))

    def loss_permille(self) -> int:
        """Network loss (packets never received, from seq gaps) over roughly the last 5 s."""
        if self._highest is None:
            return 0
        expected, received = self._expected(), self.received
        if self._loss_marks:
            base_expected, base_received = self._loss_marks[0]
            if expected - base_expected >= LOSS_MARK_PACKETS:
                expected -= base_expected
                received -= base_received
        if expected <= 0:
            return 0
        return max(0, min(1000, round(1000 * (expected - received) / expected)))

    # -- playout (audio side) ----------------------------------------------------------------

    @property
    def effective_target(self) -> int:
        return self.target_samples + self.extra_frames * self.frame_samples

    def level_frames(self) -> int:
        """Frames from the playout point to the newest packet, gaps included (they will be concealed)."""
        if self._highest is None:
            return 0
        base = self._play_seq if self._play_seq is not None else self._earliest()
        if base is None:
            return 0
        return max(0, seq_diff(self._highest, base) + 1)

    def _earliest(self) -> Optional[int]:
        if not self._frames:
            return None
        return min(self._frames, key=lambda s: seq_diff(s, self._highest))

    def _trim_to_target(self, extra_frames: int = 0) -> None:
        keep = max(1, math.ceil(self.effective_target / self.frame_samples)) + extra_frames
        self._skip_to(seq_add(self._highest, 1 - keep))

    def _skip_to(self, new_base: int) -> None:
        """Move the playout point forward to `new_base`, dropping the older frames."""
        if self.playing and not self._fade_in and self._xfade_from is None:
            # Audio is flowing: the next frame crossfades from what would have played next, so
            # the jump in the waveform is not a click.
            frame = self._frames.get(self._play_seq)
            self._xfade_from = frame if frame is not None else self._last_frame[::-1]
        for seq in [s for s in self._frames if seq_diff(s, new_base) < 0]:
            del self._frames[seq]
        self._play_seq = new_base

    def _try_start(self) -> bool:
        earliest = self._earliest()
        if earliest is None:
            return False
        # One frame beyond the target: playback consumes it smoothly while packets add whole
        # frames, so the time-averaged level then starts out at the target.
        span = seq_diff(self._highest, earliest)
        if span * self.frame_samples < self.effective_target:
            return False
        self._play_seq = earliest
        if self._fresh and (span - 1) * self.frame_samples >= self.effective_target:
            # More piled up before the output started pulling (it opened late, or reopened):
            # start at the target latency. Not after an underrun: with bursty delivery the burst
            # that refills the buffer is exactly what bridges the next gap, and trimming it would
            # underrun again on every burst. A backlog that playback never needs is removed by
            # drop_surplus() instead.
            self._trim_to_target(extra_frames=1)
        self._fresh = False
        self.playing = True
        return True

    def restart(self) -> None:
        """The output (re)opened: prefill again, and drop what piled up meanwhile like a first start."""
        self.playing = False
        self._fresh = True
        self._fade_in = True
        self._xfade_from = None
        self._last_frame = np.zeros(self.frame_samples, np.float32)   # nothing left to fade out

    def drop_surplus(self, frames: int) -> None:
        """Skip up to `frames` of the oldest queued audio: a backlog that playback has not needed."""
        frames = min(frames, self.level_frames() - 1)
        if not self.playing or frames <= 0:
            return
        self._skip_to(seq_add(self._play_seq, frames))
        self.resyncs += 1

    def relax(self) -> None:
        """Take one underrun frame back off the target (the engine saw it go unused)."""
        self.extra_frames = max(0, self.extra_frames - 1)

    def retarget(self, target_samples: int) -> None:
        self.target_samples = target_samples
        self.extra_frames = 0
        if not self.playing:
            return
        level = self.level_frames() * self.frame_samples
        if level > target_samples + self.frame_samples:
            self._trim_pending = True
        elif level < target_samples:
            self.playing = False  # re-prefill to the deeper target instead of slowly drifting up to it

    def next_frame(self) -> np.ndarray:
        if not self.playing and not self._try_start():
            return self._conceal()
        frame = self._frames.pop(self._play_seq, None)
        if frame is not None:
            self._play_seq = seq_add(self._play_seq, 1)
            frame = self._join(frame)
            self._concealed_run = 0
            self._last_frame = frame
            return frame
        if seq_newer(self._highest, self._play_seq):
            # Newer audio exists, so this frame was lost (or is too late to matter).
            self.concealed += 1
            self._play_seq = seq_add(self._play_seq, 1)
            return self._conceal()
        self.underruns += 1
        self.playing = False
        # Audio arrives with gaps longer than the buffer bridges (typically Wi-Fi power save
        # delivering in bursts): prefill and play one frame deeper from now on.
        self.extra_frames = min(self.extra_frames + 1, self.max_extra_frames)
        return self._conceal()

    def _join(self, frame: np.ndarray) -> np.ndarray:
        """Make `frame` continue smoothly from the previous output (fade in, or crossfade after a skip)."""
        if self._fade_in:
            n = min(FADE_SAMPLES, len(frame))
            frame = frame.copy()
            frame[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
        elif self._xfade_from is not None:
            n = min(FADE_SAMPLES, len(frame), len(self._xfade_from))
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            frame = frame.copy()
            frame[:n] = frame[:n] * ramp + self._xfade_from[:n] * (1.0 - ramp)
        self._fade_in = False
        self._xfade_from = None
        return frame

    def _conceal(self) -> np.ndarray:
        """Fade out the last frame played backwards, then silence; the next real frame fades back in.

        Backwards, because the reversed frame starts with the very sample the output just played,
        so the concealment joins without a step whatever the waveform's period is.
        """
        self._fade_in = True
        self._xfade_from = None
        self._concealed_run += 1
        if self._concealed_run == 1:
            last = self._last_frame[::-1]
            return last * np.linspace(1.0, 0.0, len(last), dtype=np.float32)
        return np.zeros(self.frame_samples, np.float32)


@functools.lru_cache(maxsize=8)
def _sinc_table(in_rate: int, out_rate: int, half_taps: int, phases: int, beta: float) -> np.ndarray:
    # Cut off just below the lower Nyquist so a 48 kHz -> 44.1 kHz conversion does not alias.
    cutoff = 0.91 * min(1.0, out_rate / in_rate)
    offsets = np.arange(-half_taps + 1, half_taps + 1)
    x = offsets[None, :] - np.arange(phases + 1)[:, None] / phases
    window = np.i0(beta * np.sqrt(np.clip(1.0 - (x / half_taps) ** 2, 0.0, None))) / np.i0(beta)
    table = cutoff * np.sinc(cutoff * x) * window
    table /= table.sum(axis=1, keepdims=True)  # unity DC gain for every phase
    table = table.astype(np.float32)
    table.setflags(write=False)
    return table


class SincResampler:
    """Streaming windowed-sinc (Kaiser) resampler whose ratio may change on every call.

    Positions are measured in input samples. A polyphase table with linear interpolation between
    adjacent phases gives sub-sample accuracy far beyond what the ratio adjustments need.
    """

    HALF_TAPS = 32
    PHASES = 256
    KAISER_BETA = 8.0

    def __init__(self, in_rate: int, out_rate: int):
        h = self.HALF_TAPS
        self.nominal_step = in_rate / out_rate
        self._table = _sinc_table(in_rate, out_rate, h, self.PHASES, self.KAISER_BETA)
        self._offsets = np.arange(-h + 1, h + 1)
        self._hist = np.zeros(2 * h, np.float32)
        self._pos = float(h - 1)

    @classmethod
    def prepare(cls, in_rate: int, out_rate: int) -> None:
        """Build the filter table ahead of time, outside any lock the audio thread might need."""
        _sinc_table(in_rate, out_rate, cls.HALF_TAPS, cls.PHASES, cls.KAISER_BETA)

    def pending(self) -> float:
        """Input samples held here that have not been played yet."""
        return len(self._hist) - self._pos

    def missing(self, n: int, step: float) -> int:
        """How many more input samples are needed before process(n, step) can run."""
        last = self._pos + (n - 1) * step
        return max(0, math.floor(last) + self.HALF_TAPS + 1 - len(self._hist))

    def append(self, samples: np.ndarray) -> None:
        self._hist = np.concatenate((self._hist, samples))

    def process(self, n: int, step: float) -> np.ndarray:
        t = self._pos + step * np.arange(n)
        base = np.floor(t).astype(np.int64)
        phase = (t - base) * self.PHASES
        p = phase.astype(np.int64)
        a = (phase - p).astype(np.float32)[:, None]
        coef = self._table[p] + (self._table[p + 1] - self._table[p]) * a
        out = np.einsum("ij,ij->i", self._hist[base[:, None] + self._offsets], coef)
        new_pos = self._pos + n * step
        drop = math.floor(new_pos) - (self.HALF_TAPS - 1)
        if drop > 0:
            self._hist = self._hist[drop:]
            new_pos -= drop
        self._pos = new_pos
        return out.astype(np.float32, copy=False)


class DriftController:
    """PI controller turning the buffer-level error into a resampling-ratio correction.

    The phone's and the sound card's clocks differ by up to a few hundred ppm; consuming input
    slightly faster or slower than nominal keeps the buffer at its target without audible
    artefacts. The error is smoothed first because network jitter makes the level noisy.
    """

    KP = 0.15           # correction per second of (smoothed) buffer error: 1 ms -> 150 ppm
    KI = 0.01           # per second^2; damping KP / (2 * sqrt(KI)) = 0.75
    LIMIT = 1000e-6     # +/-1000 ppm
    SMOOTHING_S = 1.5

    def __init__(self):
        self.integral = 0.0
        self.correction = 0.0
        self.smoothed_error: Optional[float] = None   # seconds; None while not playing

    def pause(self) -> None:
        """Forget the level estimate (playback stopped); the learned drift in `integral` is kept."""
        self.smoothed_error = None

    def update(self, error_s: float, dt: float) -> float:
        if self.smoothed_error is None:
            self.smoothed_error = error_s
        else:
            self.smoothed_error += (error_s - self.smoothed_error) * min(1.0, dt / self.SMOOTHING_S)
        e = self.smoothed_error
        # Conditional integration (anti-windup): don't integrate further into saturation.
        if abs(self.KP * e + self.integral) < self.LIMIT or (e > 0) != (self.integral > 0):
            self.integral = min(self.LIMIT, max(-self.LIMIT, self.integral + self.KI * e * dt))
        self.correction = min(self.LIMIT, max(-self.LIMIT, self.KP * e + self.integral))
        return self.correction


class SoftLimiter:
    """Peak limiter: no sample exceeds the ceiling, and nothing is clipped.

    Signals below the ceiling pass untouched. On a loud block the gain glides down to what the
    block's peak needs, arriving no later than the first sample that would go over; afterwards
    it recovers smoothly.
    """

    CEILING = 0.89      # -1 dBFS
    RELEASE_S = 0.25

    def __init__(self):
        self._gain = 1.0

    def process(self, x: np.ndarray, rate: int) -> np.ndarray:
        if len(x) == 0:
            return x
        peak = float(np.max(np.abs(x)))
        wanted = 1.0 if peak <= self.CEILING else self.CEILING / peak
        if wanted < self._gain:
            # Every sample before the first one that is over the ceiling at the old gain stays
            # under it at any gain between old and new, so the ramp ends there.
            ramp = int(np.argmax(np.abs(x) * self._gain > self.CEILING))
            gains = np.full(len(x), wanted, np.float32)
            gains[:ramp] = np.linspace(self._gain, wanted, ramp, endpoint=False, dtype=np.float32)
            self._gain = wanted
            return x * gains
        # Release. Both ends are at most `wanted`, so the ramp cannot push a sample over.
        new_gain = wanted + (self._gain - wanted) * math.exp(-len(x) / (rate * self.RELEASE_S))
        if self._gain != 1.0 or new_gain != 1.0:
            x = x * np.linspace(self._gain, new_gain, len(x), dtype=np.float32)
        self._gain = new_gain
        return x


class _LevelWindow:
    """The buffer level over one review window, relative to the target, in input samples."""

    def __init__(self):
        self.seconds = 0.0
        self.level_low = math.inf     # lowest time-averaged level estimate
        self.left_low = math.inf      # least audio actually left queued after a block
        self._level_sum = 0.0

    def add(self, level: float, left: float, dt: float) -> None:
        self.level_low = min(self.level_low, level)
        self.left_low = min(self.left_low, left)
        self._level_sum += level * dt
        self.seconds += dt

    @property
    def level_mean(self) -> float:
        return self._level_sum / self.seconds


def _ramp(x: np.ndarray, start: float, end: float) -> np.ndarray:
    """x scaled by a gain going linearly from `start` to `end` across the block."""
    if start == end:
        return x if start == 1.0 else x * np.float32(start)
    return x * np.linspace(start, end, len(x), dtype=np.float32)


@dataclass(frozen=True)
class EngineStats:
    active: bool             # a session is running
    playing: bool            # past prefill, audio is flowing to the output
    buffer_ms: float
    target_ms: int
    loss_permille: int
    received: int
    duplicates: int
    late: int
    concealed: int
    underruns: int
    resyncs: int
    drift_ppm: float
    phone_muted: bool
    output_rate: int
    output_peak: float       # the meter's peak (after gain, before mute) over the last whole second


class AudioEngine:
    """Thread-safe bridge between the network thread (push) and the output (render).

    `clock` timestamps packet arrivals for the level estimate, so it needs sub-millisecond
    resolution (on Windows time.monotonic() ticks every 15.6 ms). Simulations pass a virtual clock.
    """

    def __init__(self, latency_ms: int = 70, gain: float = 1.0, muted: bool = False,
                 clock: Callable[[], float] = time.perf_counter):
        self._lock = threading.Lock()
        self._clock = clock
        self._latency_ms = latency_ms
        self._gain = gain
        self._muted = muted
        self._in_rate = 48000
        self._channels = 1
        self._out_rate = 48000
        self._jb: Optional[JitterBuffer] = None
        self._resampler: Optional[SincResampler] = None
        self._ending = False        # session over; the next render fades out instead of cutting off
        self._controller = DriftController()
        self._limiter = SoftLimiter()
        self._phone_muted = False
        self._last_arrival = 0.0
        self._resampler_pending = 0.0
        # Render-thread state: the buffer level's review window, and the gain and mute factors
        # last applied, which changes ramp away from instead of stepping.
        self._window = _LevelWindow()
        self._applied_gain = gain
        self._applied_mute = 0.0 if muted else 1.0
        self._peak = 0.0
        self._sum_sq = 0.0
        self._meter_count = 0
        # The meter's peak per whole second of output, kept apart from read_meter() so that
        # stats() can report it without taking readings away from the UI's meter.
        self._second_peak = 0.0
        self._second_count = 0
        self._last_second_peak = 0.0

    # -- configuration (any thread) ----------------------------------------------------------

    def set_output_rate(self, rate: int) -> None:
        """Called by the output before it starts pulling audio at `rate`."""
        SincResampler.prepare(self._in_rate, rate)
        with self._lock:
            self._out_rate = rate
            self._resampler = SincResampler(self._in_rate, rate) if self._jb is not None else None
            self._controller = DriftController()
            if self._jb is not None:
                # Packets kept arriving while the output was closed; start again at the target
                # latency instead of playing that backlog late.
                self._jb.restart()

    def start_session(self, sample_rate: int, channels: int, frame_samples: int) -> None:
        SincResampler.prepare(sample_rate, self._out_rate)
        with self._lock:
            self._in_rate = sample_rate
            self._channels = channels
            self._jb = JitterBuffer(frame_samples, self._ms_to_samples(self._latency_ms),
                                    self._ms_to_samples(RESYNC_HEADROOM_MS),
                                    self._ms_to_samples(MAX_EXTRA_MS))
            self._resampler = SincResampler(sample_rate, self._out_rate)
            self._ending = False
            self._phone_muted = False
            self._resampler_pending = 0.0
            self._controller.pause()

    def end_session(self) -> None:
        with self._lock:
            self._ending = self._jb is not None
            self._phone_muted = False

    def set_latency(self, latency_ms: int) -> None:
        with self._lock:
            self._latency_ms = latency_ms
            if self._jb is not None:
                self._jb.retarget(self._ms_to_samples(latency_ms))

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, gain)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def _ms_to_samples(self, ms: float) -> int:
        return int(round(ms * self._in_rate / 1000))

    # -- network side ------------------------------------------------------------------------

    def push(self, seq: int, pcm: bytes, phone_muted: bool = False) -> str:
        """Queue one AUDIO payload (int16 LE). Returns 'ok', 'duplicate', 'late' or 'inactive'."""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        channels = self._channels
        if channels > 1:
            samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
        now = self._clock()
        with self._lock:
            if self._jb is None or self._ending:
                return "inactive"
            self._phone_muted = phone_muted
            result = self._jb.push(seq, samples)
            if result == "ok":
                self._last_arrival = now
            return result

    # -- audio side --------------------------------------------------------------------------

    def render(self, n: int) -> np.ndarray:
        """Produce n mono float32 samples at the output rate. Never blocks on the network."""
        now = self._clock()
        with self._lock:
            jb, resampler = self._jb, self._resampler
            ending = self._ending
            out_rate, in_rate = self._out_rate, self._in_rate
            last_arrival = self._last_arrival
            if jb is not None:
                target = jb.effective_target
                level_frames = jb.level_frames()
                playing = jb.playing
                frame_samples = jb.frame_samples
        if jb is None or resampler is None or n <= 0:
            out = np.zeros(max(n, 0), np.float32)
            self._meter(out)
            return out

        if playing:
            # Packets add a whole frame at once and each call removes a whole block, so the raw
            # count is a sawtooth whose phase slowly slides with the clock drift; sampled at a
            # fixed point it would show a bias of up to a frame that changes every few tens of
            # seconds. Interpolating both staircases (time since the last packet, half of this
            # block) estimates the time-averaged level, which is what has to sit at the target.
            frame_s = frame_samples / in_rate
            since = min(max(now - last_arrival, 0.0), frame_s)
            queued = level_frames * frame_samples + resampler.pending()
            level = queued + (since - frame_s / 2) * in_rate - n * resampler.nominal_step / 2
            correction = self._controller.update((level - target) / in_rate, n / out_rate)
            left = queued - n * resampler.nominal_step   # what remains once this block is played
            self._review_depth(jb, level - target, left - target, n / out_rate, frame_samples)
        else:
            self._controller.pause()
            self._window = _LevelWindow()
            correction = 0.0
        step = resampler.nominal_step * (1.0 + correction)
        while resampler.missing(n, step) > 0:
            with self._lock:
                frame = jb.next_frame()
            resampler.append(frame)
        out = resampler.process(n, step)
        self._resampler_pending = resampler.pending()
        if ending:
            out = out * np.linspace(1.0, 0.0, n, dtype=np.float32)
            with self._lock:
                if self._jb is jb:      # unless a new session has started meanwhile
                    self._jb = None
                    self._resampler = None
                    self._ending = False
                    self._resampler_pending = 0.0

        # Gain and mute changes ramp across the block: a step at a block boundary would click.
        gain = self._gain
        out = _ramp(out, self._applied_gain, gain)
        self._applied_gain = gain
        out = self._limiter.process(out, out_rate)
        self._meter(out)
        mute = 0.0 if self._muted else 1.0
        out = _ramp(out, self._applied_mute, mute)
        self._applied_mute = mute
        return out

    def _review_depth(self, jb: JitterBuffer, level: float, left: float, dt: float,
                      frame_samples: int) -> None:
        """Once per window, adjust the buffer depth from its low point, i.e. what playback never needed.

        `level` is the controller's time-averaged estimate and `left` what is really still
        queued after this block, both relative to the target. Going by the low point rather than
        the current level keeps the swings of bursty delivery, which the buffer is there to
        bridge. An underrun's extra frame comes off when, with the average held a frame lower,
        the buffer would still keep a margin; how far it dips below its average does not depend
        on where the controller has got to. Audio that stayed well above the target all window
        is a backlog (it piled up while the output stalled, say) and is dropped, as the drift
        controller could only drain it at 1 ms per second.
        """
        window = self._window
        window.add(level, left, dt)
        if window.seconds < REVIEW_WINDOW_S:
            return
        self._window = _LevelWindow()
        backlog = window.level_low
        with self._lock:
            if self._jb is not jb:
                return
            lowest_if_relaxed = jb.effective_target - frame_samples - (window.level_mean - window.left_low)
            if jb.extra_frames and lowest_if_relaxed >= RELAX_MARGIN_FRAMES * frame_samples:
                jb.relax()
                backlog += frame_samples
            surplus = int(backlog // frame_samples)
            if surplus < SURPLUS_MIN_FRAMES:
                return
            jb.drop_surplus(surplus)
        self._controller.pause()   # its smoothed level still includes what was just dropped

    def _meter(self, block: np.ndarray) -> None:
        if len(block) == 0:
            return
        peak = float(np.max(np.abs(block)))
        sum_sq = float(np.dot(block, block))
        with self._lock:
            self._peak = max(self._peak, peak)
            self._sum_sq += sum_sq
            self._meter_count += len(block)
            self._second_peak = max(self._second_peak, peak)
            self._second_count += len(block)
            if self._second_count >= self._out_rate:
                self._last_second_peak = self._second_peak
                self._second_peak, self._second_count = 0.0, 0

    # -- monitoring (any thread) -------------------------------------------------------------

    def read_meter(self) -> tuple[float, float]:
        """(peak, rms) of the output (after gain, before mute) since the previous call."""
        with self._lock:
            peak, sum_sq, count = self._peak, self._sum_sq, self._meter_count
            self._peak, self._sum_sq, self._meter_count = 0.0, 0.0, 0
        return peak, (math.sqrt(sum_sq / count) if count else 0.0)

    def stats(self) -> EngineStats:
        with self._lock:
            jb = self._jb
            if jb is None or self._ending:
                return EngineStats(False, False, 0.0, self._latency_ms, 0, 0, 0, 0, 0, 0, 0,
                                   self._controller.correction * 1e6, False, self._out_rate,
                                   self._last_second_peak)
            smoothed = self._controller.smoothed_error
            if jb.playing and smoothed is not None:
                level_s = jb.effective_target / self._in_rate + smoothed
            else:
                level_s = (jb.level_frames() * jb.frame_samples + self._resampler_pending) / self._in_rate
            return EngineStats(
                active=True,
                playing=jb.playing,
                buffer_ms=max(0.0, level_s * 1000.0),
                target_ms=self._latency_ms,
                loss_permille=jb.loss_permille(),
                received=jb.received,
                duplicates=jb.duplicates,
                late=jb.late,
                concealed=jb.concealed,
                underruns=jb.underruns,
                resyncs=jb.resyncs,
                drift_ppm=self._controller.correction * 1e6,
                phone_muted=self._phone_muted,
                output_rate=self._out_rate,
                output_peak=self._last_second_peak,
            )
