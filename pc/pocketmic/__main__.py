"""Command line: `python -m pocketmic` starts the GUI; see --help for headless and diagnostic modes."""

from __future__ import annotations

import argparse
import ctypes
import io
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from . import settings as settings_mod
from .instance import SingleInstance, mutex_name
from .net import valid_ipv4
from .output import HOST_API_SHORT, NULL_DEVICE, candidates, list_output_devices
from .receiver import Overrides, Receiver
from .settings import DEFAULT_DEVICE, LATENCY_PRESETS_MS

log = logging.getLogger("pocketmic")

STATUS_INTERVAL_S = 1.0
CONSOLE_FLAGS = {"--nogui", "--list-devices", "-h", "--help", "--version"}


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pocketmic", description="PocketMic receiver: plays your phone's microphone into a virtual audio cable.")
    parser.add_argument("--nogui", action="store_true", help="run in the console, printing status once per second")
    parser.add_argument("--list-devices", action="store_true", help="list audio output devices and exit")
    parser.add_argument("--device", help=f'output device name (substring), or "{NULL_DEVICE}" to discard audio; '
                                         f'default "{DEFAULT_DEVICE}"')
    parser.add_argument("--phone-ip", help="connect to the phone at this IPv4 address")
    parser.add_argument("--port", type=int, help="phone UDP port (default 50505)")
    parser.add_argument("--latency", choices=sorted(LATENCY_PRESETS_MS, key=LATENCY_PRESETS_MS.get),
                        help="jitter buffer target: low 40 ms, normal 70 ms, high 150 ms")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--stats-file", type=Path, help="write JSON status and statistics here every second")
    parser.add_argument("--duration", type=float, default=0.0, help="exit after N seconds (sends BYE)")
    parser.add_argument("--version", action="version", version=f"PocketMic {__version__}")
    args = parser.parse_args(argv)
    if args.phone_ip and not valid_ipv4(args.phone_ip):
        parser.error(f"--phone-ip: {args.phone_ip!r} is not an IPv4 address")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def setup_streams(console_wanted: bool) -> None:
    """pythonw and the windowed .exe start with stdout/stderr set to None."""
    if sys.stdout is not None:
        if isinstance(sys.stdout, io.TextIOWrapper):
            # Redirected, stdout uses the ANSI code page with strict errors, and a phone name with
            # an emoji or Cyrillic letters would end the run at the first status line.
            sys.stdout.reconfigure(errors="replace")
        return
    if console_wanted and ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        stream = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
    else:
        # Nobody can see the output, but argparse messages and library warnings must not crash us.
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = stream


def say(text: str) -> None:
    print(text, flush=True)


def setup_logging(log_dir: Path, console: bool) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(log_dir / "pocketmic.log", maxBytes=512_000,
                                                       backupCount=1, encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except OSError:
        pass  # logging is a convenience; never refuse to start because of it
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(stream)


def list_devices() -> int:
    devices = list_output_devices()
    chosen = candidates(devices, DEFAULT_DEVICE)
    say(f"{'#':>4}  {'API':<12} {'Rate':>6} {'Ch':>3}  Name")
    for dev in devices:
        mark = "  <- default" if chosen and dev == chosen[0] else ""
        say(f"{dev.index:>4}  {HOST_API_SHORT[dev.host_api]:<12} {dev.sample_rate:>6} "
            f"{dev.channels:>3}  {dev.name}{mark}")
    if not chosen:
        say(f'\nNo "{DEFAULT_DEVICE}" device found: install VB-Audio Virtual Cable (https://vb-audio.com/Cable/).')
    return 0


def format_status(snap: dict) -> str:
    phone = snap["phone"]
    link = snap["link"]
    if link == "connected":
        head = f"Connected to {phone['name']} ({phone['ip']})"
    elif link == "connecting":
        head = f"Connecting to {phone['name']} ({phone['ip']})"
    else:
        head = "Searching" + (f" [{snap['notice']}]" if snap["notice"] != "none" else "")
    out = snap["output"]
    output = out["device"] if out["state"] == "running" else f"{out['state']}: {out['message']}"
    return (f"{time.strftime('%H:%M:%S')}  {head} | buffer {snap['buffer_ms']:.0f}/{snap['target_ms']} ms"
            f" | loss {snap['loss_permille'] / 10:.1f}% | underruns {snap['underruns']} | late {snap['late']}"
            f" | dup {snap['duplicates']} | resyncs {snap['resyncs']} | drift {snap['drift_ppm']:+.0f} ppm"
            f" | out: {output}")


def run_console(receiver: Receiver, duration: float) -> None:
    deadline = time.monotonic() + duration if duration > 0 else None
    receiver.start()
    try:
        while True:
            say(format_status(receiver.snapshot()))
            remaining = STATUS_INTERVAL_S if deadline is None else min(STATUS_INTERVAL_S, deadline - time.monotonic())
            if remaining <= 0:
                break
            time.sleep(remaining)
    except KeyboardInterrupt:
        pass
    finally:
        receiver.stop()


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    setup_streams(console_wanted=bool(CONSOLE_FLAGS.intersection(argv)))
    args = parse_args(argv)
    if args.list_devices:
        return list_devices()

    settings_file = settings_mod.settings_path()
    setup_logging(settings_file.parent, console=args.nogui)
    guard = SingleInstance(mutex_name(settings_file))
    if not guard.acquire():
        if args.nogui:
            say("PocketMic is already running (look for its icon in the system tray).")
        else:
            from .app import show_already_running
            show_already_running()
        return 1
    try:
        settings = settings_mod.load(settings_file)
        log.info("PocketMic %s starting (pc_id %08x, settings %s)", __version__, settings.pc_id, settings_file)
        overrides = Overrides(device=args.device, phone_ip=args.phone_ip, port=args.port, latency=args.latency)
        receiver = Receiver(settings, settings_file, overrides, stats_file=args.stats_file)
        if args.nogui:
            run_console(receiver, args.duration)
        else:
            from .app import run_gui
            run_gui(receiver, start_hidden=args.minimized or settings.start_minimized, duration=args.duration)
        log.info("PocketMic stopped")
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
