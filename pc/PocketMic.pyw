"""Double-click launcher: starts the PocketMic receiver without a console window."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pocketmic.__main__ import main  # noqa: E402

sys.exit(main())
