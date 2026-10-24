"""Persistent receiver settings, stored as JSON in %APPDATA%/PocketMic/settings.json."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import protocol

log = logging.getLogger(__name__)

SETTINGS_ENV = "POCKETMIC_SETTINGS"
DEFAULT_DEVICE = "CABLE Input"

READ_TRIES = 5              # a settings file another program holds open is retried for about 1 s
READ_RETRY_S = 0.2

LATENCY_PRESETS_MS = {"low": 40, "normal": 70, "high": 150}
DEFAULT_LATENCY = "normal"
MAX_GAIN_PERCENT = 400


def default_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "PocketMic" / "settings.json"


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    return Path(override) if override else default_path()


def is_default_path(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(str(default_path().resolve()))


def new_id() -> int:
    """Random non-zero u32 (pc_id, session_id)."""
    while True:
        value = secrets.randbits(32)
        if value:
            return value


@dataclass
class Settings:
    pc_id: int = 0
    pc_name: str = field(default_factory=socket.gethostname)
    last_phone_id: int = 0
    last_phone_ip: str = ""
    manual_ip: str = ""
    auto_connect: bool = True
    output_device: str = DEFAULT_DEVICE
    latency: str = DEFAULT_LATENCY
    gain_percent: int = 100
    muted: bool = False
    port: int = protocol.DEFAULT_PORT
    start_with_windows: bool = False
    start_minimized: bool = False
    close_to_tray: bool = True

    @property
    def latency_ms(self) -> int:
        return LATENCY_PRESETS_MS[self.latency]


def _valid(name: str, value) -> bool:
    if name in ("pc_id", "last_phone_id"):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < (1 << 32)
    if name == "port":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
    if name == "gain_percent":
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_GAIN_PERCENT
    if name == "latency":
        return value in LATENCY_PRESETS_MS
    if name == "pc_name":
        return isinstance(value, str) and value.strip() != ""
    default = getattr(Settings(), name)
    return isinstance(value, type(default))


def load(path: Optional[Path] = None) -> Settings:
    """Load settings, falling back to defaults per field. Generates and saves pc_id on first run."""
    path = path or settings_path()
    settings = Settings()
    data, writable = _read(path)
    for f in dataclasses.fields(Settings):
        if f.name in data and _valid(f.name, data[f.name]):
            setattr(settings, f.name, data[f.name])
    if settings.pc_id == 0:
        settings.pc_id = new_id()
        if writable:
            save(settings, path)
    return settings


def _read(path: Path) -> tuple[dict, bool]:
    """The file's JSON object, and whether load() may write the file (it was missing or is backed up)."""
    for attempt in range(READ_TRIES):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            return {}, True
        except OSError as exc:
            # Usually another program holding the file for a moment. Never overwrite it with
            # defaults because of that: it would lose the pc_id, the last phone and the port.
            if attempt == READ_TRIES - 1:
                log.warning("Could not read settings file %s (%s); using defaults for this run", path, exc)
                return {}, False
            time.sleep(READ_RETRY_S)
    try:
        # PowerShell's Set-Content -Encoding UTF8 and Notepad's "UTF-8 with BOM" write a BOM.
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data, True
    except ValueError as exc:
        backup = path.with_name(path.name + ".bad")
        log.warning("Settings file %s is damaged (%s); keeping it as %s and using defaults", path, exc, backup.name)
        try:
            os.replace(path, backup)
        except OSError as move_exc:
            log.warning("Could not keep a copy of %s: %s", path, move_exc)
            return {}, False
        return {}, True


def save(settings: Settings, path: Optional[Path] = None) -> None:
    """Write atomically: a crash mid-write must never leave a truncated settings file."""
    path = path or settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dataclasses.asdict(settings), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.error("Could not save settings to %s: %s", path, exc)
