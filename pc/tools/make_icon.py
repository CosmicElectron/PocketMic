"""Writes the application icon (.ico, 16-256 px) used by build_exe.bat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pocketmic.icons import save_ico  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_icon.py OUTPUT.ico")
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    save_ico(target)
    print(f"Wrote {target}")
