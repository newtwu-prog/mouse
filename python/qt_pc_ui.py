"""Launch the PyQt6 PC UI. The tkinter app is unchanged.

Tkinter (existing):
    py -3.11 python/pc_ui.py
    python/pc_ui.bat

PyQt6:
    py -3.11 python/qt_pc_ui.py
    python/qt_pc_ui.bat
    python -m qt_ui          # from the python/ directory
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from qt_ui.app import main as run
    except ImportError as exc:
        sys.stderr.write(
            "無法啟動 PyQt6 介面。請安裝：pip install 'PyQt6>=6.6' 'pyqtgraph>=0.13.7'\n"
            f"({exc})\n"
        )
        return 1
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
