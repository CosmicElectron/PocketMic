import threading
import time

from pocketmic import output
from pocketmic.audio import AudioEngine
from pocketmic.output import (DeviceOutput, NullOutput, OutputDevice, candidates, device_choices, make_output,
                              name_matches)


def dev(index, name, api, rate=48000, ch=2):
    return OutputDevice(index, name, api, rate, ch)


DEVICES = [
    dev(20, "CABLE Input (VB-Audio Virtual C", "MME", 44100, 16),
    dev(37, "CABLE In 16ch (VB-Audio Virtual", "MME", 44100, 16),
    dev(58, "CABLE Input (VB-Audio Virtual Cable)", "Windows DirectSound", 44100, 16),
    dev(77, "CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI"),
    dev(92, "Headphones (MOMENTUM 4 Stereo)", "Windows WASAPI", 44100),
    dev(18, "Speakers (Realtek(R) Audio)", "MME"),
]


def test_name_matching_handles_case_and_mme_truncation():
    assert name_matches("cable input", "CABLE Input (VB-Audio Virtual Cable)")
    assert name_matches("CABLE Input (VB-Audio Virtual Cable)", "CABLE Input (VB-Audio Virtual C")
    assert not name_matches("CABLE Input", "CABLE In 16ch (VB-Audio Virtual")
    assert not name_matches("", "anything")


def test_candidates_prefer_wasapi_then_directsound_then_mme():
    found = candidates(DEVICES, "CABLE Input")
    assert [d.index for d in found] == [77, 58, 20]
    full_name = candidates(DEVICES, "CABLE Input (VB-Audio Virtual Cable)")
    assert [d.index for d in full_name] == [77, 58, 20]


def test_device_choices_lists_each_device_once_with_full_names():
    names = device_choices(DEVICES)
    assert names.count("CABLE Input (VB-Audio Virtual Cable)") == 1
    assert "CABLE Input (VB-Audio Virtual C" not in names
    assert "Speakers (Realtek(R) Audio)" in names and "Headphones (MOMENTUM 4 Stereo)" in names


def test_null_output_consumes_in_real_time():
    engine = AudioEngine()
    rendered = []
    original = engine.render
    engine.render = lambda n: rendered.append(n) or original(n)
    out = make_output(engine, "null")
    assert isinstance(out, NullOutput)
    out.start()
    time.sleep(1.0)
    out.stop()
    assert out.status.state == "stopped"
    assert 0.85 * 48000 <= sum(rendered) <= 1.1 * 48000


class FakePortAudio:
    """Stands in for pyaudio.PyAudio with no devices; records when it is set up and whether two
    threads were ever inside its (slow, not thread-safe) initialise or terminate at once."""
    inits: list = []
    busy = 0
    overlapped = False
    guard = threading.Lock()

    @classmethod
    def _global_setup(cls):
        with cls.guard:
            cls.busy += 1
            cls.overlapped |= cls.busy > 1
        time.sleep(0.02)                  # Pa_Initialize takes a while, which is when races bite
        with cls.guard:
            cls.busy -= 1

    def __init__(self):
        FakePortAudio.inits.append(time.monotonic())
        self._global_setup()

    def get_host_api_count(self):
        return 0

    def get_device_count(self):
        return 0

    def terminate(self):
        self._global_setup()


def test_missing_device_is_looked_for_less_and_less_often(monkeypatch):
    monkeypatch.setattr(output.pyaudio, "PyAudio", FakePortAudio)
    monkeypatch.setattr(output, "RETRY_INTERVAL_S", 0.05)
    monkeypatch.setattr(output, "MISSING_RETRY_MAX_S", 0.4)
    FakePortAudio.inits = []
    out = DeviceOutput(AudioEngine(), "NoSuchDevice")
    out.start()
    time.sleep(1.6)
    out.stop()
    assert out.status.state == "stopped"
    gaps = [b - a for a, b in zip(FakePortAudio.inits, FakePortAudio.inits[1:])]
    assert len(gaps) >= 4
    # Never faster again (within two ticks of the Windows timer), and from the shortest to the cap.
    assert all(later >= earlier - 0.035 for earlier, later in zip(gaps, gaps[1:]))
    assert gaps[0] < 0.2 and gaps[-1] >= 0.4


def test_portaudio_is_never_initialised_by_two_threads_at_once(monkeypatch):
    monkeypatch.setattr(output.pyaudio, "PyAudio", FakePortAudio)
    monkeypatch.setattr(output, "RETRY_INTERVAL_S", 0.0)
    monkeypatch.setattr(output, "MISSING_RETRY_MAX_S", 0.0)
    FakePortAudio.overlapped = False
    out = DeviceOutput(AudioEngine(), "NoSuchDevice")    # the supervisor, retrying flat out
    out.start()
    listers = [threading.Thread(target=lambda: [output.list_output_devices() for _ in range(10)]) for _ in range(3)]
    for t in listers:                                    # and the UI's Refresh, from other threads
        t.start()
    for t in listers:
        t.join()
    out.stop()
    assert not FakePortAudio.overlapped
