"""QApplication entry for the PyQt6 PC UI."""

from __future__ import annotations

import sys

import pyqtgraph as pg
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from qt_ui.main_window import MainWindow
from qt_ui.theme import FONT_FAMILIES, STYLESHEET


def create_app() -> QApplication:
    pg.setConfigOptions(antialias=True, background="w", foreground="#1e293b")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSize(10)
    app.setFont(font)
    return app


def main() -> int:
    app = create_app()
    window = MainWindow()
    window.show()
    return app.exec()
