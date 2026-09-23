"""python -m qt_ui  (run from the python/ directory, or with it on PYTHONPATH)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qt_ui.app import main

raise SystemExit(main())
