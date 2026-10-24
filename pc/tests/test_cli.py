"""The command line, run as a real subprocess (headless, null output, simulator on an ephemeral port)."""

import json
import os
import subprocess
import sys

import pytest

from conftest import PC_DIR, wait_for
from tools.fake_phone import FakePhone


def receiver(*args):
    return subprocess.Popen([sys.executable, "-m", "pocketmic", *map(str, args)], cwd=PC_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.fixture
def phone():
    ph = FakePhone(port=0, host="127.0.0.1", name="CLI Phone").start()
    yield ph
    ph.stop()


def test_headless_run_streams_writes_stats_and_says_bye(phone, tmp_path, isolated_settings):
    stats_file = tmp_path / "stats.json"
    proc = receiver("--nogui", "--device", "null", "--phone-ip", "127.0.0.1", "--port", phone.port,
                    "--latency", "low", "--duration", 3, "--stats-file", stats_file)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert "Connected to CLI Phone (127.0.0.1)" in out
    stats = json.loads(stats_file.read_text(encoding="utf-8"))
    assert stats["stopped"] and stats["link"] == "connected"
    assert stats["received"] > 150 and stats["target_ms"] == 40
    assert stats["output"]["state"] == "running"
    assert stats["output_peak"] == pytest.approx(0.1, abs=0.01)   # the fake phone's -20 dBFS tone
    assert wait_for(lambda: phone.byes_received == 1, 2)
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert saved["last_phone_id"] == phone.phone_id and saved["last_phone_ip"] == "127.0.0.1"
    # Command-line overrides are for this run only.
    assert saved["output_device"] == "CABLE Input" and saved["latency"] == "normal" and saved["manual_ip"] == ""


def test_second_instance_with_same_settings_exits(phone):
    first = receiver("--nogui", "--device", "null", "--port", phone.port, "--phone-ip", "127.0.0.1", "--duration", 4)
    try:
        assert wait_for(lambda: phone.session is not None, 8)
        second = receiver("--nogui", "--device", "null", "--port", phone.port, "--duration", 1)
        out, _ = second.communicate(timeout=15)
        assert second.returncode == 1
        assert "already running" in out
    finally:
        first.communicate(timeout=15)
    assert first.returncode == 0


def test_rejects_invalid_phone_ip():
    proc = receiver("--nogui", "--phone-ip", "not-an-ip")
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 2 and "IPv4" in out


def test_list_devices_runs():
    proc = receiver("--list-devices")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert out.splitlines()[0].split()[:2] == ["#", "API"]


@pytest.mark.parametrize("name", ["Pixel \U0001F525", "\u041f\u0438\u043a\u0441\u0435\u043b\u044c 9"])
def test_redirected_console_survives_phone_names_outside_the_code_page(name):
    phone = FakePhone(port=0, host="127.0.0.1", name=name).start()
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        proc = subprocess.run([sys.executable, "-m", "pocketmic", "--nogui", "--device", "null", "--port",
                               str(phone.port), "--phone-ip", "127.0.0.1", "--duration", "2.5"],
                              cwd=PC_DIR, env=env, capture_output=True, timeout=30)
        out = proc.stdout.decode("utf-8", errors="replace")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Connected to " in out
    finally:
        phone.stop()
