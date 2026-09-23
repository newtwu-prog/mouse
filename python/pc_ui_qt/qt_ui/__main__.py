"""python -m qt_ui  (run from python/pc_ui_qt, with python/ on PYTHONPATH)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PC_UI_QT = HERE.parent
PYTHON = PC_UI_QT.parent
for folder in (PYTHON, PC_UI_QT):
    text = str(folder)
    if text not in sys.path:
        sys.path.insert(0, text)

from qt_ui.app import main

raise SystemExit(main())
