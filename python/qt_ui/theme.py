"""Visual style for the Qt PC UI."""

from __future__ import annotations

FONT_FAMILIES = (
    "Microsoft JhengHei UI",
    "Microsoft JhengHei",
    "WenQuanYi Micro Hei",
    "Noto Sans CJK TC",
    "Droid Sans Fallback",
    "sans-serif",
)

STATE_COLOR = {
    "WAKE": "#b45309",
    "NREM": "#1d4ed8",
    "REM": "#7e22ce",
    "—": "#64748b",
}

TRACE_COLOR = {
    "EEG": "#2563eb",
    "EMG": "#ea580c",
    "TTL": "#b91c1c",
    "delta": "#dc2626",
    "theta": "#16a34a",
}

STYLESHEET = """
QMainWindow, QWidget#central {
    background: #e8eef4;
    color: #1e293b;
}
QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QPlainTextEdit, QTableWidget {
    font-weight: normal;
    color: #1e293b;
}
QGroupBox {
    background: #ffffff;
    border: 1px solid #d0d7e2;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 8px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 4px;
    color: #334155;
    font-weight: 600;
}
QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 3px 8px;
    min-height: 22px;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #2563eb;
}
QLineEdit:read-only {
    background: #f8fafc;
    color: #334155;
}
QComboBox::drop-down {
    border: none;
    width: 18px;
}
QPushButton {
    background: #f8fafc;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 4px 12px;
    min-height: 26px;
}
QPushButton:hover {
    background: #f1f5f9;
}
QPushButton:disabled {
    color: #94a3b8;
    background: #f1f5f9;
    border-color: #e2e8f0;
}
QPushButton#connectButton {
    background: #1d4ed8;
    color: #ffffff;
    border: 1px solid #1e40af;
    font-weight: 600;
}
QPushButton#connectButton:hover {
    background: #1e40af;
}
QPushButton#connectButton:disabled {
    background: #e2e8f0;
    color: #94a3b8;
    border-color: #e2e8f0;
}
QPushButton#startButton {
    background: #15803d;
    color: #ffffff;
    border: 1px solid #166534;
    font-weight: 600;
}
QPushButton#startButton:hover {
    background: #166534;
}
QPushButton#startButton:disabled {
    background: #e2e8f0;
    color: #94a3b8;
    border-color: #e2e8f0;
}
QPushButton#stopButton {
    background: #ffffff;
    color: #b91c1c;
    border: 1px solid #fca5a5;
    font-weight: 600;
}
QPushButton#stopButton:hover {
    background: #fef2f2;
}
QPushButton#stopButton:disabled {
    color: #fca5a5;
    background: #fff;
    border-color: #fee2e2;
}
QLabel#statusChip {
    background: #ffffff;
    border: 1px solid #d0d7e2;
    border-radius: 12px;
    padding: 4px 12px;
    color: #475569;
}
QLabel#statusChip[state="ok"] {
    color: #166534;
    border-color: #86efac;
    background: #f0fdf4;
}
QLabel#statusChip[state="run"] {
    color: #1e40af;
    border-color: #93c5fd;
    background: #eff6ff;
}
QLabel#statusChip[state="busy"] {
    color: #92400e;
    border-color: #fcd34d;
    background: #fffbeb;
}
QLabel#statusChip[state="bad"] {
    color: #991b1b;
    border-color: #fecaca;
    background: #fef2f2;
}
QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #d0d7e2;
    border-radius: 8px;
    top: -1px;
}
QTabBar::tab {
    background: #dbe3ec;
    color: #334155;
    padding: 7px 16px;
    margin-right: 3px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #0f172a;
    font-weight: 600;
}
QCheckBox {
    spacing: 6px;
}
QPlainTextEdit#logView {
    background: #0f172a;
    color: #e2e8f0;
    border: 1px solid #1e293b;
    border-radius: 6px;
    font-family: Consolas, "WenQuanYi Micro Hei Mono", monospace;
}
QFrame#statusPanel {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
}
QFrame#groupCard {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
}
QFrame#groupCard[selected="true"] {
    border: 1px solid #2563eb;
    background: #eff6ff;
}
QLabel#muted {
    color: #64748b;
}
QLabel#stateBig {
    font-size: 20px;
    font-weight: 700;
}
QLabel#hint {
    color: #64748b;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    gridline-color: #e2e8f0;
}
QHeaderView::section {
    background: #f1f5f9;
    color: #334155;
    padding: 4px 6px;
    border: none;
    border-bottom: 1px solid #d0d7e2;
    font-weight: 600;
}
QSplitter::handle {
    background: transparent;
}
"""
