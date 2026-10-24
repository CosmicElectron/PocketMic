import json
import sys

from pocketmic import autostart
from pocketmic.autostart import Autostart
from pocketmic import settings as s
from pocketmic.instance import MUTEX_NAME, SingleInstance, mutex_name


def test_settings_path_comes_from_env(isolated_settings):
    assert s.settings_path() == isolated_settings
    assert not s.is_default_path(isolated_settings)


def test_first_load_creates_file_with_persistent_pc_id(isolated_settings):
    first = s.load()
    assert first.pc_id != 0 and first.pc_id < 1 << 32
    assert isolated_settings.exists()
    assert s.load().pc_id == first.pc_id
    assert first.pc_name                      # defaults to the hostname
    assert (first.output_device, first.latency, first.gain_percent, first.port) == ("CABLE Input", "normal", 100, 50505)
    assert first.auto_connect and first.close_to_tray and not first.start_with_windows


def test_round_trip_and_atomic_write(isolated_settings):
    cfg = s.load()
    cfg.last_phone_id, cfg.last_phone_ip, cfg.manual_ip = 0xA1B2C3D4, "192.168.0.23", "192.168.0.23"
    cfg.latency, cfg.gain_percent, cfg.muted = "low", 250, True
    s.save(cfg)
    assert s.load() == cfg
    assert [p.name for p in isolated_settings.parent.iterdir()] == ["settings.json"]   # no temp files left


def test_bad_values_fall_back_per_field(isolated_settings):
    isolated_settings.parent.mkdir(parents=True)
    isolated_settings.write_text(json.dumps({
        "pc_id": 1234, "gain_percent": 9000, "latency": "turbo", "port": "50505", "muted": "yes",
        "manual_ip": "10.0.0.5", "auto_connect": False, "unknown_key": 1}), encoding="utf-8")
    cfg = s.load()
    assert cfg.pc_id == 1234 and cfg.manual_ip == "10.0.0.5" and cfg.auto_connect is False
    assert (cfg.gain_percent, cfg.latency, cfg.port, cfg.muted) == (100, "normal", 50505, False)


def test_corrupt_file_is_kept_aside_and_replaced_with_defaults(isolated_settings):
    isolated_settings.parent.mkdir(parents=True)
    isolated_settings.write_text("{not json", encoding="utf-8")
    cfg = s.load()
    assert cfg.pc_id != 0
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))["pc_id"] == cfg.pc_id
    assert (isolated_settings.parent / "settings.json.bad").read_text(encoding="utf-8") == "{not json"


def test_file_with_a_utf8_bom_is_read(isolated_settings):
    """PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM."""
    isolated_settings.parent.mkdir(parents=True)
    content = json.dumps({"pc_id": 0x11223344, "last_phone_id": 0xA1B2C3D4, "port": 50600, "gain_percent": 250})
    isolated_settings.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    cfg = s.load()
    assert (cfg.pc_id, cfg.last_phone_id, cfg.port, cfg.gain_percent) == (0x11223344, 0xA1B2C3D4, 50600, 250)
    assert isolated_settings.read_bytes() == b"\xef\xbb\xbf" + content.encode("utf-8")   # left alone


def test_unreadable_file_is_never_overwritten(isolated_settings, monkeypatch):
    isolated_settings.parent.mkdir(parents=True)
    isolated_settings.write_text(json.dumps({"pc_id": 0x11223344, "port": 50600}), encoding="utf-8")
    before = isolated_settings.read_bytes()
    reads = []

    def locked(self):
        reads.append(self)
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(s, "READ_RETRY_S", 0.0)
    monkeypatch.setattr(type(isolated_settings), "read_bytes", locked)
    cfg = s.load()
    monkeypatch.undo()
    assert len(reads) == s.READ_TRIES
    assert cfg.pc_id != 0 and cfg.port == 50505        # defaults for this run...
    assert isolated_settings.read_bytes() == before    # ...but the user's file is untouched


def test_autostart_command_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = autostart.launch_command()
    assert cmd.endswith('PocketMic.pyw" --minimized')
    interpreter, script = cmd.split('" "')
    assert interpreter.lower().endswith("pythonw.exe") and interpreter.startswith('"')
    assert script.startswith(str(autostart.LAUNCHER))


def test_autostart_command_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\PocketMic\PocketMicReceiver.exe")
    assert autostart.launch_command() == r'"C:\Program Files\PocketMic\PocketMicReceiver.exe" --minimized'


def test_autostart_state_checks_the_command_and_task_manager():
    ours = r'"C:\PocketMic\PocketMicReceiver.exe" --minimized'
    assert autostart.evaluate(None, None, ours) is Autostart.OFF
    assert autostart.evaluate(ours, None, ours) is Autostart.ON
    assert autostart.evaluate(ours.lower(), None, ours) is Autostart.ON              # paths ignore case
    assert autostart.evaluate(r'"D:\old\PocketMicReceiver.exe" --minimized', None, ours) is Autostart.STALE
    assert autostart.evaluate(ours, bytes([2] + [0] * 11), ours) is Autostart.ON     # enabled in Task Manager
    assert autostart.evaluate(ours, bytes([3] + [0] * 11), ours) is Autostart.DISABLED


def test_autostart_state_reads_both_registry_values(monkeypatch):
    values = {autostart.RUN_KEY: autostart.launch_command(), autostart.STARTUP_APPROVED_KEY: bytes([3] + [0] * 11)}
    monkeypatch.setattr(autostart, "_read_value", values.get)   # read-only: nothing touches the registry
    assert autostart.state() is Autostart.DISABLED


def test_single_instance_mutex(tmp_path):
    name = mutex_name(tmp_path / "x" / "settings.json")
    assert name.startswith(MUTEX_NAME + "-") and name != mutex_name(tmp_path / "y" / "settings.json")
    first, second = SingleInstance(name), SingleInstance(name)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_default_settings_file_uses_the_documented_mutex_name():
    assert mutex_name(s.default_path()) == "Local\\PocketMicReceiver"
