#!/usr/bin/env bash
# PyQt6 PC UI. Tkinter UI: python/pc_ui_tk/pc_ui.py or python/pc_ui_tk/pc_ui.bat
cd "$(dirname "$0")"
exec python3 qt_pc_ui.py "$@"
