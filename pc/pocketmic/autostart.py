"""'Start with Windows': the HKCU Run value that launches the receiver minimized to the tray.

The registry is only written when the user toggles the checkbox; nothing here runs on import.
Reading also looks at the entry Task Manager keeps for it, which is never written here.
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Task Manager > Startup records "disabled" here without touching the Run value itself.
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "PocketMic"
LAUNCHER = Path(__file__).resolve().parent.parent / "PocketMic.pyw"


class Autostart(Enum):
    OFF = "off"
    ON = "on"
    STALE = "stale"          # the entry starts another copy of PocketMic (moved or rebuilt elsewhere)
    DISABLED = "disabled"    # the entry is ours, but switched off in Task Manager > Startup


def launch_command() -> str:
    """Command line for the Run value, quoted so paths with spaces work."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    return f'"{interpreter}" "{LAUNCHER}" --minimized'


def evaluate(command: Optional[str], approved: Optional[bytes], expected: str) -> Autostart:
    """The state, from the Run value, Task Manager's flag for it and the command this copy needs."""
    if not isinstance(command, str):
        return Autostart.OFF
    if os.path.normcase(command.strip()) != os.path.normcase(expected):
        return Autostart.STALE
    # Task Manager sets the low bit of the first byte when disabled (03 ...), clears it when enabled (02 ...).
    if isinstance(approved, bytes) and approved and approved[0] & 1:
        return Autostart.DISABLED
    return Autostart.ON


def state() -> Autostart:
    return evaluate(_read_value(RUN_KEY), _read_value(STARTUP_APPROVED_KEY), launch_command())


def _read_value(key_path: str):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def set_enabled(enabled: bool) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
