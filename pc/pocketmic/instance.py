"""Single-instance guard: a named Windows mutex per settings file.

Two receivers sharing one settings file would share a pc_id and keep taking the phone away from
each other, so only one may run. A run with POCKETMIC_SETTINGS pointing elsewhere is a separate
identity and gets its own mutex.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from typing import Optional

from .settings import is_default_path

MUTEX_NAME = "Local\\PocketMicReceiver"
ERROR_ALREADY_EXISTS = 183


def mutex_name(settings_file: Path) -> str:
    if is_default_path(settings_file):
        return MUTEX_NAME
    digest = hashlib.sha1(os.path.normcase(str(settings_file.resolve())).encode("utf-8")).hexdigest()
    return f"{MUTEX_NAME}-{digest[:12]}"


class SingleInstance:
    def __init__(self, name: str):
        self.name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        """True if this process is now the only holder; False if another instance is running."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self._close(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._close(self._handle)
            self._handle = None

    @staticmethod
    def _close(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)
