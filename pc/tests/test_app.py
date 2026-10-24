"""GUI tests: the real window and tray icon, a null output and the simulator on an ephemeral port."""

import os
import subprocess
import sys

from conftest import PC_DIR
from pocketmic import settings as settings_mod
from pocketmic.app import App
from pocketmic.receiver import Overrides, Receiver
from tools.fake_phone import FakePhone


def test_gui_shows_the_connection_and_quits_cleanly(isolated_settings):
    phone = FakePhone(port=0, host="127.0.0.1", name="GUI Phone").start()
    try:
        receiver = Receiver(settings_mod.load(), isolated_settings,
                            Overrides(device="null", phone_ip="127.0.0.1", port=phone.port))
        app = App(receiver, start_hidden=True)
        receiver.on_event = app.on_receiver_event
        seen = {}

        def inspect():
            seen["status"] = app._status_var.get()
            seen["stats"] = app._stats_var.get()
            seen["rows"] = [app._tree.item(i)["values"] for i in app._tree.get_children()]
            seen["output"] = app._output_var.get()

        app.root.after(1200, app._show_window)         # show it, so the meter and layout are drawn
        app.root.after(2500, inspect)
        app.root.after(2600, app.quit)
        app.run()

        assert seen["status"] == "Connected to GUI Phone (127.0.0.1)"
        assert seen["rows"] == [["GUI Phone", "127.0.0.1", "Connected"]]
        assert seen["stats"].startswith("Buffer ") and "Underruns 0" in seen["stats"]
        assert seen["output"].startswith("Playing to null output")
        assert phone.byes_received == 1
        assert not app._tray_thread.is_alive()
    finally:
        phone.stop()


def run_app(receiver, steps):
    """Run the real App (hidden) and call each (delay_ms, fn) in turn on the Tk thread."""
    app = App(receiver, start_hidden=True)
    receiver.on_event = app.on_receiver_event
    for delay, fn in steps:
        app.root.after(delay, lambda fn=fn: fn(app))
    app.root.after(max(d for d, _ in steps) + 100, app.quit)
    app.run()
    return app


def test_status_says_which_phone_to_connect_when_auto_connect_will_not(isolated_settings):
    phone = FakePhone(port=0, host="127.0.0.1", name="New Phone").start()
    try:
        cfg = settings_mod.load()
        cfg.manual_ip, cfg.port = "127.0.0.1", phone.port
        cfg.last_phone_id = 0x12345678                 # e.g. the phone app was reinstalled: a new phone_id
        receiver = Receiver(cfg, isolated_settings, Overrides(device="null"))
        seen = []

        def look(app):
            seen.append((app._status_var.get(), app._detail_var.get(), receiver.net.status().link.value))

        run_app(receiver, [(1500, look), (1600, lambda app: receiver.set_auto_connect(False)), (2000, look),
                           (2100, lambda app: app._show_window()), (2200, lambda app: app._show_window()),
                           (2400, lambda app: seen.append(app.root.state()))])
        (status, detail, link), (status_off, detail_off, _), window = seen
        assert link == "searching" and status == "Found New Phone"
        assert "not the phone this PC used last" in detail and "press Connect" in detail
        assert status_off == "Found New Phone" and "press Connect" in detail_off and "switched off" in detail_off
        assert window == "normal"                      # a tray double-click (two clicks) leaves it shown
    finally:
        phone.stop()


def test_pc_mute_is_shown_in_the_status(isolated_settings):
    phone = FakePhone(port=0, host="127.0.0.1", name="Muted Phone").start()
    try:
        receiver = Receiver(settings_mod.load(), isolated_settings,
                            Overrides(device="null", phone_ip="127.0.0.1", port=phone.port))
        seen = []
        run_app(receiver, [(1500, lambda app: app._set_muted(True)),
                           (1700, lambda app: seen.append((app._status_var.get(), app._detail_var.get()))),
                           (1800, lambda app: app._set_muted(False))])
        status, detail = seen[0]
        assert status == "Connected to Muted Phone (127.0.0.1)"
        assert detail.startswith("Muted on this PC")
    finally:
        phone.stop()


def test_gui_with_a_real_output_device_starts_and_quits(isolated_settings, tmp_path):
    """The GUI lists devices while the output supervisor opens PortAudio: that must not crash.

    A device name that matches nothing, so no audio device is opened and nothing is played.
    """
    phone = FakePhone(port=0, host="127.0.0.1", name="Device Phone").start()
    try:
        env = dict(os.environ, POCKETMIC_SETTINGS=str(isolated_settings))
        proc = subprocess.run(
            [sys.executable, "-m", "pocketmic", "--minimized", "--device", "NoSuchDevice_PocketMicTest",
             "--port", str(phone.port), "--phone-ip", "127.0.0.1", "--duration", "5"],
            cwd=PC_DIR, env=env, capture_output=True, text=True, timeout=60)
        log_text = (isolated_settings.parent / "pocketmic.log").read_text(encoding="utf-8")
        assert proc.returncode == 0, log_text[-2000:]
        assert "PocketMic stopped" in log_text
        assert 'No output device matching "NoSuchDevice_PocketMicTest"' in log_text
    finally:
        phone.stop()
