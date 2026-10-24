import sys
import time
from pathlib import Path

import pytest

PC_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PC_DIR))


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """No test may read or write the real %APPDATA%/PocketMic/settings.json."""
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setenv("POCKETMIC_SETTINGS", str(path))
    return path


def wait_for(condition, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())
