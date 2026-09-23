"""One short row: pick a group, then toggle that group's traces.

Checkbox state is remembered per group. Drawing still uses every group's flags.
Each chart keeps at least one visible trace (the bottom chart is EMG only).
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QWidget

SIGNALS = ("EEG", "EMG", "TTL", "delta", "theta")


class SignalToggleBar(QGroupBox):
    visibility_changed = Signal()
    group_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("顯示訊號", parent)
        self.setObjectName("signalBar")
        self._states: dict[str, dict[str, bool]] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        self.group_label = QLabel("勾選群組")
        self.group_label.setObjectName("signalGroupLabel")
        row.addWidget(self.group_label)
        self.combo = QComboBox()
        self.combo.setObjectName("signalGroupCombo")
        self.combo.setMinimumWidth(140)
        self.combo.currentTextChanged.connect(self._on_group)
        row.addWidget(self.combo)
        self._empty = QLabel("（尚無群組）")
        self._empty.setObjectName("muted")
        row.addWidget(self._empty)
        self._checks: dict[str, QCheckBox] = {}
        for signal in SIGNALS:
            box = QCheckBox(signal)
            box.setObjectName(f"sig{signal}")
            box.toggled.connect(lambda checked, sig=signal: self._on_toggled(sig, checked))
            self._checks[signal] = box
            row.addWidget(box)
        row.addStretch(1)
        self.setMaximumHeight(78)
        self._load_checks()

    def set_groups(self, names: list[str]) -> None:
        previous = self._states
        states: dict[str, dict[str, bool]] = {}
        for index, name in enumerate(names):
            if name in previous:
                states[name] = {sig: bool(previous[name].get(sig, False)) for sig in SIGNALS}
            else:
                default = index == 0
                states[name] = {sig: default for sig in SIGNALS}
        self._states = states
        self._ensure_minimum()
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(names)
        if current in names:
            self.combo.setCurrentText(current)
        elif names:
            self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)
        self._load_checks()
        self.visibility_changed.emit()

    def visible_map(self) -> dict[str, dict[str, bool]]:
        return {name: dict(flags) for name, flags in self._states.items()}

    def is_on(self, group: str, signal: str) -> bool:
        return bool(self._states.get(group, {}).get(signal, False))

    def select_group(self, name: str) -> None:
        if not name or name == self.combo.currentText():
            return
        if self.combo.findText(name) < 0:
            return
        self.combo.setCurrentText(name)

    def _on_group(self, name: str) -> None:
        self._load_checks()
        if name:
            self.group_changed.emit(name)

    def _on_toggled(self, signal: str, checked: bool) -> None:
        name = self.combo.currentText()
        if name not in self._states:
            return
        self._states[name][signal] = checked
        if not checked and any(not self._subplot_has_trace(part) for part in self._affected(signal)):
            self._states[name][signal] = True
            box = self._checks[signal]
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)
            return
        self.visibility_changed.emit()

    def _load_checks(self) -> None:
        name = self.combo.currentText()
        flags = self._states.get(name)
        self._empty.setVisible(flags is None)
        for signal, box in self._checks.items():
            box.setVisible(flags is not None)
            box.blockSignals(True)
            box.setChecked(bool(flags.get(signal)) if flags else False)
            box.blockSignals(False)

    def _affected(self, signal: str) -> tuple[str, ...]:
        if signal == "EMG":
            return ("main", "bot")
        if signal in ("EEG", "TTL"):
            return ("main",)
        return ("mid",)

    def _subplot_has_trace(self, subplot: str) -> bool:
        for flags in self._states.values():
            if subplot == "main" and (flags["EEG"] or flags["EMG"] or flags["TTL"]):
                return True
            if subplot == "mid" and (flags["delta"] or flags["theta"]):
                return True
            if subplot == "bot" and flags["EMG"]:
                return True
        return False

    def _ensure_minimum(self) -> None:
        if not self._states:
            return
        first = next(iter(self._states))
        preferred = {
            "main": ("EEG", "EMG", "TTL"),
            "mid": ("delta", "theta"),
            "bot": ("EMG",),
        }
        for subplot, signals in preferred.items():
            if not self._subplot_has_trace(subplot):
                self._states[first][signals[0]] = True
