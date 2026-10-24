"""AudioEngine end to end, driven in virtual time (much faster than real time)."""

import math

import numpy as np
import pytest

from pocketmic.audio import AudioEngine, SoftLimiter

IN_RATE = 48000
FRAME = 480


class VirtualStream:
    """A phone producing 48 kHz * (1 + ppm) samples per second and a device pulling blocks, in virtual time.

    Packets arrive in order (like a phone's send queue): `arrival(seq, sent)` gives a packet's
    arrival time, default sent + random jitter, and `lost(seq)` drops it.
    """

    def __init__(self, ppm=0.0, out_rate=48000, latency_ms=70, freq=1000.0, amplitude=0.25,
                 jitter_ms=0.0, seed=7, gain=1.0, arrival=None, lost=lambda seq: False):
        self.clock = 0.0
        self.engine = AudioEngine(latency_ms=latency_ms, gain=gain, clock=lambda: self.clock)
        self.engine.set_output_rate(out_rate)
        self.engine.start_session(IN_RATE, 1, FRAME)
        self.out_rate = out_rate
        self.block = out_rate // 100
        self.packet_s = FRAME / (IN_RATE * (1 + ppm / 1e6))
        self.freq, self.amplitude = freq, amplitude
        self.jitter_s = jitter_ms / 1000
        self.rng = np.random.default_rng(seed)
        self.arrival = arrival or self._jittered
        self.lost = lost
        self.seq = 0
        self._seq_arrival = None
        self.renders = 0

    def _jittered(self, seq, sent):
        return sent + (self.rng.uniform(0, self.jitter_s) if self.jitter_s else 0.0)

    def _pcm(self, seq):
        n = np.arange(seq * FRAME, (seq + 1) * FRAME)
        return np.round(self.amplitude * 32767 * np.sin(2 * np.pi * self.freq * n / IN_RATE)).astype("<i2").tobytes()

    def _deliver(self, t):
        while True:
            if self._seq_arrival is None:
                self._seq_arrival = max(self.clock, self.arrival(self.seq, (self.seq + 1) * self.packet_s))
            if self._seq_arrival > t:
                return
            if not self.lost(self.seq):
                self.clock = self._seq_arrival
                self.engine.push(self.seq, self._pcm(self.seq))
            self.seq += 1
            self._seq_arrival = None

    def run(self, seconds):
        """Yields (time, output block, stats) for every device callback."""
        end = self.renders + int(seconds * self.out_rate / self.block)
        while self.renders < end:
            t = self.renders * self.block / self.out_rate
            self._deliver(t)
            self.clock = t
            out = self.engine.render(self.block)
            self.renders += 1
            yield t, out, self.engine.stats()

    def output_gap(self, seconds):
        """The output stops pulling (e.g. the device is reopening) while packets keep arriving."""
        end = self.renders + int(seconds * self.out_rate / self.block)
        while self.renders < end:
            self._deliver(self.renders * self.block / self.out_rate)
            self.renders += 1

    def now(self):
        return self.renders * self.block / self.out_rate


@pytest.mark.parametrize("ppm,out_rate", [(300, 48000), (-300, 48000), (300, 44100), (-300, 44100)])
def test_drift_is_tracked_without_resyncs(ppm, out_rate):
    stream = VirtualStream(ppm=ppm, out_rate=out_rate)
    levels = [(t, s.buffer_ms) for t, _, s in stream.run(60)]
    final = stream.engine.stats()
    settled = np.array([lvl for t, lvl in levels if t >= 40])
    assert final.resyncs == 0 and final.underruns == 0 and final.concealed == 0
    assert np.all(np.abs(settled - 70) < 3), (settled.min(), settled.max())
    # The learned correction matches the simulated clock error.
    assert abs(final.drift_ppm - ppm) < 100


def test_drift_with_network_jitter_stays_near_target():
    stream = VirtualStream(ppm=250, jitter_ms=25)
    levels = [(t, s.buffer_ms) for t, _, s in stream.run(60)]
    settled = np.array([lvl for t, lvl in levels if t >= 40])
    final = stream.engine.stats()
    assert final.resyncs == 0 and final.underruns == 0
    assert np.all(np.abs(settled - 70) < 5)


def dominant_frequency(x, rate):
    windowed = x * np.hanning(len(x))
    spectrum = np.abs(np.fft.rfft(windowed))
    k = int(np.argmax(spectrum))
    # Parabolic interpolation for sub-bin accuracy.
    a, b, c = np.log(spectrum[k - 1:k + 2] + 1e-20)
    k += 0.5 * (a - c) / (a - 2 * b + c)
    return k * rate / len(x)


def sinad_db(x, rate, freq):
    windowed = x * np.blackman(len(x))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    k = int(round(freq * len(x) / rate))
    signal = power[k - 8:k + 9].sum()
    return 10 * np.log10(signal / (power.sum() - signal))


@pytest.mark.parametrize("out_rate", [48000, 44100])
def test_resampled_tone_keeps_pitch_level_and_purity(out_rate):
    stream = VirtualStream(ppm=200, out_rate=out_rate, freq=1000.0, amplitude=0.25)
    blocks = [out for t, out, _ in stream.run(12) if t >= 4]
    x = np.concatenate(blocks)
    # Pitch is off by at most the drift correction (a few hundred ppm of 1 kHz).
    assert abs(dominant_frequency(x, out_rate) - 1000.0) < 1.0
    rms = np.sqrt(np.mean(x ** 2))
    assert abs(20 * np.log10(rms / (0.25 / np.sqrt(2)))) < 0.1
    assert sinad_db(x, out_rate, 1000.0) > 60


def test_gain_scales_and_meter_reports_level():
    stream = VirtualStream(amplitude=0.1, gain=2.0)
    blocks = [out for t, out, _ in stream.run(3) if t >= 1]
    x = np.concatenate(blocks)
    assert abs(np.max(np.abs(x)) - 0.2) < 0.01
    stream.engine.read_meter()
    for _ in stream.run(0.5):
        pass
    peak, rms = stream.engine.read_meter()
    assert abs(peak - 0.2) < 0.01 and abs(rms - 0.2 / np.sqrt(2)) < 0.01
    assert abs(stream.engine.stats().output_peak - 0.2) < 0.01   # reading the meter does not reset it


def test_limiter_never_hard_clips():
    stream = VirtualStream(amplitude=0.5, gain=4.0)    # +6 dBFS before limiting
    x = np.concatenate([out for t, out, _ in stream.run(3) if t >= 0.5])
    assert np.max(np.abs(x)) < 1.0
    assert np.max(np.abs(x)) > 0.85                    # loud, but limited rather than squashed
    clipped_run = np.max(np.convolve(np.abs(x) > 0.999, np.ones(3), "valid"))
    assert clipped_run == 0


def test_mute_outputs_silence_but_keeps_metering():
    stream = VirtualStream(amplitude=0.3)
    stream.engine.set_muted(True)
    x = np.concatenate([out for t, out, _ in stream.run(2) if t >= 0.5])
    assert not np.any(x)
    peak, _ = stream.engine.read_meter()
    assert peak > 0.25


def test_session_end_fades_out_instead_of_clicking():
    stream = VirtualStream(amplitude=0.5, freq=200.0)
    for _ in stream.run(1):
        pass
    stream.engine.end_session()
    assert not stream.engine.stats().active
    assert stream.engine.push(10_000, bytes(960)) == "inactive"
    last = stream.engine.render(480)
    assert np.max(np.abs(last[:48])) > 0.2            # still sounding at the start of the block...
    assert np.max(np.abs(last[-24:])) < 0.03          # ...and faded to nothing by its end
    assert not np.any(stream.engine.render(480))


def test_no_session_renders_silence():
    engine = AudioEngine()
    engine.set_output_rate(48000)
    assert not np.any(engine.render(480))
    assert engine.push(0, bytes(960)) == "inactive"
    assert not engine.stats().active


def test_stereo_payload_is_downmixed():
    engine = AudioEngine(latency_ms=40, clock=lambda: 0.0)
    engine.set_output_rate(48000)
    engine.start_session(48000, 2, FRAME)
    left_right = np.empty(2 * FRAME, "<i2")
    left_right[0::2], left_right[1::2] = 10000, -10000 + 2000
    out = []
    for seq in range(12):
        engine.push(seq, left_right.tobytes())
        out.append(engine.render(480))
    assert abs(out[-1][-1] - 1000 / 32768) < 1e-3


def test_latency_change_takes_effect():
    stream = VirtualStream(latency_ms=150)
    for _ in stream.run(3):
        pass
    assert abs(stream.engine.stats().buffer_ms - 150) < 10
    stream.engine.set_latency(40)
    for _ in stream.run(5):
        pass
    assert abs(stream.engine.stats().buffer_ms - 40) < 6


def max_step(x):
    return float(np.max(np.abs(np.diff(x))))


@pytest.mark.parametrize("latency_ms,burst_s", [(40, 0.06), (70, 0.10), (70, 0.13), (150, 0.20), (150, 0.25)])
@pytest.mark.parametrize("offset_s", [0.0005, 0.0045, 0.0095])
def test_bursty_delivery_settles_instead_of_underrunning_on_every_burst(latency_ms, burst_s, offset_s):
    """Wi-Fi power save can deliver audio in bursts longer than the target latency."""
    def bursts(seq, sent):
        return sent if sent < 3 else 3 + math.ceil((sent - 3) / burst_s) * burst_s + offset_s

    stream = VirtualStream(latency_ms=latency_ms, arrival=bursts)
    for _ in stream.run(10):
        pass
    settled = stream.engine.stats().underruns
    for _ in stream.run(30):
        pass
    assert stream.engine.stats().underruns == settled


def test_a_one_off_stall_does_not_raise_the_latency_for_good():
    """An underrun deepens the buffer by a frame; with steady delivery that frame comes off again."""
    rng = np.random.default_rng(1)

    def stall(seq, sent):
        return 5.2 if 5.0 <= sent < 5.2 else sent + rng.uniform(0, 0.01)

    stream = VirtualStream(arrival=stall)
    for _ in stream.run(35):
        pass
    stats = stream.engine.stats()
    assert stats.underruns == 1
    assert abs(stats.buffer_ms - 70) < 5


def test_backlog_after_an_output_reopen_is_dropped():
    stream = VirtualStream()
    for _ in stream.run(5):
        pass
    stream.output_gap(1.5)                 # the device reopens (unplug, switch) while packets arrive
    stream.engine.set_output_rate(48000)   # what the output does before it starts pulling again
    for _ in stream.run(1):
        pass
    assert abs(stream.engine.stats().buffer_ms - 70) < 15


def test_backlog_landing_after_playback_resumed_is_dropped_soon():
    """A stall longer than the buffer; its backlog arrives in order, straddling a device callback."""
    stall_start, stall_end = 5.0, 5.15

    def stalled(seq, sent):
        if not stall_start <= sent < stall_end:
            return sent
        # Enough to restart playback lands just before the 5.15 s callback, the rest just after.
        return stall_end - 0.002 if seq < 507 else stall_end + 0.001

    stream = VirtualStream(arrival=stalled)
    for _ in stream.run(stall_end + 0.2):
        pass
    assert stream.engine.stats().buffer_ms > 120         # the backlog is in the buffer now...
    for _ in stream.run(2.5):
        pass
    assert abs(stream.engine.stats().buffer_ms - 70) < 15  # ...and gone 2.5 s later
    assert stream.engine.stats().underruns == 1


@pytest.mark.parametrize("freq", [220.0, 330.0, 1000.0])
def test_lost_packets_do_not_click(freq):
    """Concealment must join the waveform smoothly even when the frame is not a whole number of periods."""
    stream = VirtualStream(freq=freq, amplitude=0.25, lost=lambda seq: seq >= 100 and seq % 50 == 0)
    x = np.concatenate([out for t, out, _ in stream.run(6) if t >= 1])
    clean = 0.25 * 2 * np.pi * freq / IN_RATE         # largest step of the undisturbed sine
    assert stream.engine.stats().concealed >= 10
    assert max_step(x) < 1.3 * clean


def test_dropping_a_backlog_crossfades_instead_of_cutting():
    def stalled(seq, sent):                            # as in the test above: a backlog lands mid-play
        if not 5.0 <= sent < 5.15:
            return sent
        return 5.148 if seq < 507 else 5.151

    stream = VirtualStream(freq=220.0, amplitude=0.25, arrival=stalled)
    x = np.concatenate([out for t, out, _ in stream.run(9) if t >= 1])
    assert stream.engine.stats().resyncs == 1          # the backlog was dropped while playing...
    # ...and a crossfade between two points of a 220 Hz sine changes by at most a few times the
    # sine's own step per sample, where a hard cut would jump by up to the full amplitude.
    assert max_step(x) < 4 * 0.25 * 2 * np.pi * 220 / IN_RATE


def test_limiter_catches_an_onset_at_a_block_boundary():
    limiter = SoftLimiter()
    n = np.arange(4800)
    x = (0.2 * np.sin(2 * np.pi * 300 * n / 48000)).astype(np.float32)
    x[2400:] *= 10                                     # quiet, then +20 dB from one block to the next
    out = np.concatenate([limiter.process(x[i:i + 480], 48000) for i in range(0, len(x), 480)])
    onset = out[2400:2880]
    assert np.max(np.abs(out)) <= SoftLimiter.CEILING + 1e-6
    # Turned down as a whole, not flat-topped: as many samples near the peak as a scaled sine has.
    ideal = x[2400:2880] * (SoftLimiter.CEILING / np.max(np.abs(x[2400:2880])))
    near_peak = np.sum(np.abs(onset) > 0.97 * SoftLimiter.CEILING)
    assert near_peak <= np.sum(np.abs(ideal) > 0.97 * SoftLimiter.CEILING) + 2


def test_mute_and_gain_changes_ramp_instead_of_clicking():
    stream = VirtualStream(freq=220.0, amplitude=0.25)
    blocks = []
    for step, action in enumerate([None, lambda e: e.set_muted(True), lambda e: e.set_muted(False),
                                   lambda e: e.set_gain(2.0), lambda e: e.set_gain(0.5)]):
        if action is not None:
            action(stream.engine)
        blocks += [out for t, out, _ in stream.run(0.5) if t >= 0.5]
    x = np.concatenate(blocks)
    assert max_step(x) < 2.2 * 0.25 * 2 * np.pi * 220 / IN_RATE   # at most gain 2's own sine step
