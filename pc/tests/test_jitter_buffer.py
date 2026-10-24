import numpy as np

from pocketmic.audio import FADE_SAMPLES, JitterBuffer

FRAME = 480
TARGET = 3360          # 70 ms at 48 kHz: playback starts once 8 frames (target + 1) are queued
OVERFLOW = 4800        # 100 ms


def frame(seq: int) -> np.ndarray:
    """A recognisable constant frame per sequence number."""
    return np.full(FRAME, (seq % 1000 + 1) / 1000.0, np.float32)


def ident(out: np.ndarray) -> int:
    """Which frame this was (reads past the fade-in)."""
    return int(round(out[-1] * 1000)) - 1


def new_jb() -> JitterBuffer:
    return JitterBuffer(FRAME, TARGET, OVERFLOW)


def fill(jb, seqs):
    return [jb.push(s % (1 << 32), frame(s % (1 << 32))) for s in seqs]


def pull(jb, n):
    return [jb.next_frame() for _ in range(n)]


def test_prefills_to_target_then_plays_in_order():
    jb = new_jb()
    fill(jb, range(7))
    assert not np.any(jb.next_frame())            # 70 ms queued: still prefilling
    fill(jb, [7])
    got = [ident(jb.next_frame())]
    fill(jb, range(8, 12))
    got += [ident(o) for o in pull(jb, 11)]
    assert got == list(range(12))
    assert jb.concealed == 0 and jb.underruns == 0 and jb.late == 0


def test_playback_starts_at_target_even_if_more_piled_up():
    jb = new_jb()
    fill(jb, range(16))                            # 160 ms waiting when the output finally starts
    assert ident(jb.next_frame()) == 8             # oldest dropped: 8 frames (target + 1) remain
    assert jb.level_frames() == 7
    assert jb.resyncs == 0


def test_first_frame_after_prefill_fades_in():
    jb = new_jb()
    fill(jb, range(8))
    out = jb.next_frame()
    assert out[0] == 0.0 and out[FADE_SAMPLES] == frame(0)[0]


def test_reordered_packets_are_played_in_order():
    jb = new_jb()
    fill(jb, [1, 0, 3, 2, 5, 4, 7, 6])
    got = [ident(jb.next_frame())]
    fill(jb, [9, 8, 11, 10])
    got += [ident(o) for o in pull(jb, 11)]
    assert got == list(range(12))
    assert jb.concealed == 0 and jb.late == 0 and jb.duplicates == 0


def test_lost_packet_is_concealed_with_fade_out_then_silence():
    jb = new_jb()
    lost = (4, 9, 10)
    fill(jb, [s for s in range(8) if s not in lost])
    outs = pull(jb, 1)
    fill(jb, [s for s in range(8, 16) if s not in lost])
    outs += pull(jb, 15)
    assert [ident(o) for o in outs[:4]] == [0, 1, 2, 3]
    concealed = outs[4]                            # frame 3 played backwards, fading to zero
    assert abs(concealed[0] - frame(3)[0]) < 1e-6 and abs(concealed[-1]) < 1e-6
    assert np.all(np.diff(concealed) <= 1e-7)
    assert ident(outs[5]) == 5 and outs[5][0] == 0.0   # real audio returns with a fade-in
    assert abs(outs[9][0] - frame(8)[0]) < 1e-6         # first of two lost: fade-out of frame 8...
    assert not np.any(outs[10])                         # ...second: silence
    assert jb.concealed == 3


def test_duplicates_are_dropped_and_counted():
    jb = new_jb()
    results = fill(jb, [0, 1, 1, 2, 0, 3, 4, 5, 6, 7])
    got = [ident(jb.next_frame())]
    results += fill(jb, [8, 9, 8])
    got += [ident(o) for o in pull(jb, 9)]
    assert results.count("duplicate") == 3 and jb.duplicates == 3
    assert got == list(range(10))


def test_too_late_packet_is_dropped_and_counted():
    jb = new_jb()
    fill(jb, [s for s in range(8) if s != 3])
    pull(jb, 5)                                    # frame 3 was concealed here
    assert jb.push(3, frame(3)) == "late"
    assert jb.late == 1 and jb.concealed == 1
    assert ident(jb.next_frame()) == 5


def test_underrun_conceals_and_reprefills():
    jb = new_jb()
    fill(jb, range(8))
    pull(jb, 8)
    tail = jb.next_frame()
    assert jb.underruns == 1 and not jb.playing    # ran dry: counted, back to prefill
    assert abs(tail[0] - frame(7)[0]) < 1e-6 and abs(tail[-1]) < 1e-6
    assert not np.any(jb.next_frame())             # silence while prefilling
    fill(jb, range(8, 16))
    resumed = jb.next_frame()
    assert ident(resumed) == 8 and resumed[0] == 0.0
    assert jb.playing


def test_resume_after_underrun_skips_frames_that_never_arrived():
    jb = new_jb()
    fill(jb, range(8))
    pull(jb, 9)                                    # underrun waiting for frame 8
    fill(jb, range(11, 19))                        # 8..10 were lost in the outage
    assert ident(jb.next_frame()) == 11
    assert jb.concealed == 0


def test_burst_beyond_headroom_resyncs_to_target():
    jb = new_jb()
    fill(jb, range(8))
    jb.next_frame()
    fill(jb, range(8, 19))                         # a stall ends: 110 ms more arrives at once
    assert jb.resyncs == 1
    assert jb.level_frames() == 7                  # dropped the oldest audio, back at 70 ms
    assert ident(jb.next_frame()) == 12


def test_sequence_wraparound_is_seamless():
    jb = new_jb()
    seqs = [s % (1 << 32) for s in range((1 << 32) - 5, (1 << 32) + 9)]
    fill(jb, seqs[:8])
    got = pull(jb, 1)
    fill(jb, seqs[8:])
    got += pull(jb, 13)
    assert [ident(o) for o in got] == [s % 1000 for s in seqs]
    assert jb.concealed == 0 and jb.late == 0 and jb.loss_permille() == 0


def test_loss_permille_counts_missing_sequence_numbers():
    jb = new_jb()
    fill(jb, [s for s in range(1000) if s % 10 != 3])
    assert 90 <= jb.loss_permille() <= 110


def test_loss_permille_window_forgets_old_loss():
    jb = new_jb()
    fill(jb, [s for s in range(200) if s % 2])     # 50 % loss early on...
    fill(jb, range(200, 1000))                     # ...then 8 s clean
    assert jb.loss_permille() == 0


def test_retarget_up_reprefills_and_down_trims():
    jb = new_jb()
    fill(jb, range(8))
    jb.next_frame()
    fill(jb, range(8, 12))
    jb.retarget(3 * FRAME)
    fill(jb, [12])                                 # the excess is dropped as the next packet lands
    assert jb.level_frames() == 3
    assert jb.resyncs == 0
    jb.retarget(15 * FRAME)
    assert not jb.playing


def sine_frame(seq: int, freq: float = 220.0) -> np.ndarray:
    """Frames of one continuous 220 Hz sine: 10 ms is not a whole number of its periods."""
    n = np.arange(seq * FRAME, (seq + 1) * FRAME)
    return (0.25 * np.sin(2 * np.pi * freq * n / 48000)).astype(np.float32)


SINE_STEP = 0.25 * 2 * np.pi * 220 / 48000         # largest sample-to-sample step of that sine


def test_concealment_starts_where_the_output_left_off():
    jb = new_jb()
    for s in range(8):
        jb.push(s, sine_frame(s))
    out = [jb.next_frame()]
    for s in range(8, 40):
        if s != 20:                                # lost
            jb.push(s, sine_frame(s))
        out.append(jb.next_frame())
    x = np.concatenate(out)[FADE_SAMPLES:]
    assert jb.concealed == 1
    assert np.max(np.abs(np.diff(x))) <= 1.05 * SINE_STEP


def test_resync_crossfades_from_where_playback_was():
    jb = new_jb()
    for s in range(8):
        jb.push(s, sine_frame(s))
    out = [jb.next_frame() for _ in range(3)]
    for s in range(8, 25):                         # a burst well beyond the headroom: resync
        jb.push(s, sine_frame(s))
    out += [jb.next_frame() for _ in range(5)]
    assert jb.resyncs == 1
    x = np.concatenate(out)[FADE_SAMPLES:]
    assert np.max(np.abs(np.diff(x))) < 4 * SINE_STEP   # a hard cut would jump by up to 0.5


def test_restart_after_underrun_keeps_the_burst_that_refilled_the_buffer():
    jb = new_jb()
    fill(jb, range(8))
    pull(jb, 9)                                    # underrun
    fill(jb, range(8, 20))                         # 120 ms arrive at once
    assert ident(jb.next_frame()) == 8             # nothing dropped: this burst bridges the next gap
    assert jb.level_frames() == 11


def test_output_restart_drops_the_backlog_like_a_first_start():
    jb = new_jb()
    fill(jb, range(8))
    pull(jb, 1)
    fill(jb, range(8, 17))                         # the output was closed while these arrived
    assert jb.resyncs == 0                         # (not enough for the resync headroom)
    jb.restart()
    assert ident(jb.next_frame()) == 9             # 8 frames (target + 1) kept, oldest dropped
    assert jb.level_frames() == 7


def test_underruns_deepen_the_target_until_relaxed_or_retargeted():
    jb = JitterBuffer(FRAME, TARGET, OVERFLOW, max_extra_samples=2 * FRAME)
    fill(jb, range(8))
    pull(jb, 9)                                    # underrun: one frame deeper
    assert jb.effective_target == TARGET + FRAME
    fill(jb, range(8, 16))
    assert not np.any(jb.next_frame())             # 8 frames are no longer enough to start
    fill(jb, [16])
    assert ident(jb.next_frame()) == 8
    pull(jb, 9)                                    # second underrun: two frames deeper
    fill(jb, range(17, 30))
    pull(jb, 14)                                   # third: capped at two
    assert jb.underruns == 3 and jb.effective_target == TARGET + 2 * FRAME
    jb.relax()
    assert jb.effective_target == TARGET + FRAME
    jb.retarget(TARGET)
    assert jb.effective_target == TARGET


def test_drop_surplus_skips_oldest_audio_with_a_crossfade():
    jb = new_jb()
    for s in range(8):
        jb.push(s, sine_frame(s))
    out = [jb.next_frame()]
    for s in range(8, 14):                         # 60 ms more than needed, below the resync headroom
        jb.push(s, sine_frame(s))
    out.append(jb.next_frame())
    assert jb.resyncs == 0 and jb.level_frames() == 12
    jb.drop_surplus(5)
    assert jb.resyncs == 1 and jb.level_frames() == 7
    out += [jb.next_frame() for _ in range(3)]
    x = np.concatenate(out)[FADE_SAMPLES:]
    assert np.max(np.abs(np.diff(x))) < 4 * SINE_STEP
