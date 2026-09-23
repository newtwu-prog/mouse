"""Live thresholds on the right of the experiment tab. Matches the tkinter panel."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLineEdit, QPushButton

from processing.groups import THRESHOLD_MODE_LABELS, THRESHOLD_MODES, GroupSetting


class LiveParamsPanel(QGroupBox):
    group_changed = Signal(str)
    apply_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__("即時參數（看波形時調整）", parent)
        self.setObjectName("livePanel")
        self._groups: list[GroupSetting] = []
        form = QFormLayout(self)
        form.setContentsMargins(10, 12, 10, 10)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)

        self.group_box = QComboBox()
        self.group_box.setObjectName("liveGroupCombo")
        self.move = QLineEdit()
        self.move.setObjectName("liveMove")
        self.ratio = QLineEdit()
        self.ratio.setObjectName("liveRatio")
        self.mode = QComboBox()
        self.mode.setObjectName("liveMode")
        self.mode.addItems([THRESHOLD_MODE_LABELS[item] for item in THRESHOLD_MODES])
        self.threshold = QLineEdit()
        self.threshold.setObjectName("liveThreshold")
        for widget in (self.move, self.ratio, self.threshold, self.group_box, self.mode):
            widget.setMinimumWidth(120)
        form.addRow("群組", self.group_box)
        form.addRow("Movement 閾值", self.move)
        form.addRow("θ/δ 閾值", self.ratio)
        form.addRow("判斷方式", self.mode)
        form.addRow("判斷閾值 (V)", self.threshold)
        self.btn_apply = QPushButton("即時更新")
        self.btn_apply.setObjectName("liveApply")
        form.addRow(self.btn_apply)

        self.group_box.currentTextChanged.connect(self._on_group)
        self.btn_apply.clicked.connect(self.apply_clicked.emit)

    def set_groups(self, groups: list[GroupSetting], selected: str | None = None) -> None:
        self._groups = list(groups)
        names = [group.name for group in groups]
        current = selected or self.group_box.currentText()
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItems(names)
        if current in names:
            self.group_box.setCurrentText(current)
        elif names:
            self.group_box.setCurrentIndex(0)
        self.group_box.blockSignals(False)
        self._fill(self.group_box.currentText())

    def select_group(self, name: str) -> None:
        if not name or name == self.group_box.currentText():
            self._fill(name)
            return
        if self.group_box.findText(name) < 0:
            return
        self.group_box.setCurrentText(name)

    def selected_name(self) -> str:
        return self.group_box.currentText().strip()

    def values(self) -> tuple[float, float, str, float]:
        return (
            float(self.move.text()),
            float(self.ratio.text()),
            self.mode.currentText(),
            float(self.threshold.text()),
        )

    def _on_group(self, name: str) -> None:
        self._fill(name)
        if name:
            self.group_changed.emit(name)

    def _fill(self, name: str) -> None:
        group = next((item for item in self._groups if item.name == name), None)
        if group is None:
            return
        self.move.setText(f"{group.movement_threshold:g}")
        self.ratio.setText(f"{group.theta_delta_threshold:g}")
        label = THRESHOLD_MODE_LABELS.get(group.threshold_mode, THRESHOLD_MODE_LABELS["above"])
        self.mode.setCurrentText(label)
        self.threshold.setText(f"{group.threshold_v:g}")
