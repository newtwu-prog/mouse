#!/usr/bin/env bash
# PyQt6 PC UI. Tkinter UI remains: python/pc_ui.py or python/pc_ui.bat
cd "$(dirname "$0")"
exec python3 qt_pc_ui.py "$@"
