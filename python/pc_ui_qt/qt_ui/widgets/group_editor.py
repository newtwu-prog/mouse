"""Editable measurement-group page, matching ui_groups.GroupSettingsPanel.

File load/save/confirm are wired by the main window, same as the tkinter app.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from processing.groups import (
    AI_CHANNELS,
    TARGET_STATES,
    THRESHOLD_MODE_LABELS,
    THRESHOLD_MODES,
    TTL_CHANNELS,
    GroupSetting,
    ai_label,
    normalize_threshold_mode,
    parse_ai_label,
)
from qt_ui.ttl_labels import format_ttl_label, parse_ttl_label

AI_CHOICES = [ai_label(i) for i in range(AI_CHANNELS)]
TTL_CHOICES = [format_ttl_label(i) for i in range(TTL_CHANNELS)]
MODE_CHOICES = [THRESHOLD_MODE_LABELS[mode] for mode in THRESHOLD_MODES]
_HEADERS = (
    "群組名稱",
    "EEG",
    "EMG",
    "Movement",
    "θ/δ",
    "判斷狀態",
    "判斷方式",
    "判斷閾值",
    "連續判斷點數",
    "TTL",
)


class GroupEditorPage(QWidget):
    changed = Signal()

    def __init__(self, groups: list[GroupSetting], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.groups: list[GroupSetting] = list(groups)
        self._index = 0 if self.groups else -1

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body, 1)

        table_box = QGroupBox("量測群組")
        table_layout = QVBoxLayout(table_box)
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setObjectName("groupsTable")
        self.table.setHorizontalHeaderLabels(list(_HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_select)
        table_layout.addWidget(self.table)
        body.addWidget(table_box, 3)

        form_box = QGroupBox("編輯選取的群組")
        form_box.setMinimumWidth(320)
        form = QFormLayout(form_box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.name = QLineEdit()
        self.name.setObjectName("groupName")
        self.eeg = _combo(AI_CHOICES)
        self.emg = _combo(AI_CHOICES)
        self.move = QLineEdit()
        self.ratio = QLineEdit()
        self.state = _combo(list(TARGET_STATES))
        self.mode = _combo(MODE_CHOICES)
        self.threshold = QLineEdit()
        self.count = QLineEdit()
        self.ttl = _combo(TTL_CHOICES)
        self.ttl.setObjectName("groupTtl")
        for label, widget in (
            ("群組名稱", self.name),
            ("EEG 通道", self.eeg),
            ("EMG 通道", self.emg),
            ("Movement 閾值", self.move),
            ("θ/δ 閾值", self.ratio),
            ("判斷狀態", self.state),
            ("判斷方式", self.mode),
            ("判斷閾值 (V)", self.threshold),
            ("連續判斷點數", self.count),
            ("TTL 輸出", self.ttl),
        ):
            form.addRow(label, widget)
        self.btn_apply = QPushButton("套用到選取列")
        self.btn_apply.setObjectName("applyGroupRow")
        self.btn_apply.clicked.connect(self._apply_form)
        form.addRow(self.btn_apply)
        body.addWidget(form_box, 2)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("新增群組")
        self.btn_add.setObjectName("addGroup")
        self.btn_delete = QPushButton("刪除群組")
        self.btn_delete.setObjectName("deleteGroup")
        self.btn_load = QPushButton("載入設定")
        self.btn_load.setObjectName("loadGroups")
        self.btn_save = QPushButton("儲存設定")
        self.btn_save.setObjectName("saveGroups")
        self.btn_confirm = QPushButton("套用設定")
        self.btn_confirm.setObjectName("confirmGroups")
        for button in (self.btn_add, self.btn_delete, self.btn_load, self.btn_save, self.btn_confirm):
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.btn_add.clicked.connect(self._add)
        self.btn_delete.clicked.connect(self._delete)

        self.status = QLabel("設定尚未確認，無法開始實驗。")
        self.status.setObjectName("groupEditorStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._refresh_table()
        if self.groups:
            self._select_row(0)
        else:
            self._clear_form()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_groups(self, groups: list[GroupSetting]) -> None:
        self.groups = list(groups)
        self._refresh_table()
        if self.groups:
            self._select_row(0)
        else:
            self._index = -1
            self._clear_form()
        self.changed.emit()

    def _refresh_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.groups))
        for row, group in enumerate(self.groups):
            values = (
                group.name,
                ai_label(group.eeg_ai),
                ai_label(group.emg_ai),
                f"{group.movement_threshold:g}",
                f"{group.theta_delta_threshold:g}",
                group.target_state,
                THRESHOLD_MODE_LABELS.get(group.threshold_mode, group.threshold_mode),
                f"{group.threshold_v:g}",
                str(int(group.threshold_n)),
                format_ttl_label(group.ttl_dio),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)

    def _select_row(self, index: int) -> None:
        if not self.groups:
            self._index = -1
            self._clear_form()
            return
        self._index = max(0, min(index, len(self.groups) - 1))
        self.table.blockSignals(True)
        self.table.selectRow(self._index)
        self.table.blockSignals(False)
        self._fill_form(self.groups[self._index])

    def _on_select(self) -> None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return
        self._index = selected[0].row()
        if 0 <= self._index < len(self.groups):
            self._fill_form(self.groups[self._index])

    def _fill_form(self, group: GroupSetting) -> None:
        self.name.setText(group.name)
        self.eeg.setCurrentText(ai_label(group.eeg_ai))
        self.emg.setCurrentText(ai_label(group.emg_ai))
        self.move.setText(f"{group.movement_threshold:g}")
        self.ratio.setText(f"{group.theta_delta_threshold:g}")
        self.state.setCurrentText(group.target_state)
        self.mode.setCurrentText(
            THRESHOLD_MODE_LABELS.get(group.threshold_mode, THRESHOLD_MODE_LABELS["above"])
        )
        self.threshold.setText(f"{group.threshold_v:g}")
        self.count.setText(str(int(group.threshold_n)))
        self.ttl.setCurrentText(format_ttl_label(group.ttl_dio))

    def _clear_form(self) -> None:
        self.name.clear()
        self.eeg.setCurrentIndex(0)
        self.emg.setCurrentIndex(1 if len(AI_CHOICES) > 1 else 0)
        self.move.setText("0.05")
        self.ratio.setText("1.0")
        self.state.setCurrentText("NREM")
        self.mode.setCurrentText(THRESHOLD_MODE_LABELS["above"])
        self.threshold.setText("0.02")
        self.count.setText("4")
        self.ttl.setCurrentIndex(0)

    def form_group(self, fallback_name: str = "") -> GroupSetting:
        name = self.name.text().strip() or fallback_name
        return GroupSetting(
            name=name,
            eeg_ai=parse_ai_label(self.eeg.currentText()),
            emg_ai=parse_ai_label(self.emg.currentText()),
            movement_threshold=float(self.move.text()),
            theta_delta_threshold=float(self.ratio.text()),
            target_state=self.state.currentText().upper(),
            ttl_dio=parse_ttl_label(self.ttl.currentText()),
            threshold_v=float(self.threshold.text()),
            threshold_mode=normalize_threshold_mode(self.mode.currentText()),
            threshold_n=max(int(float(self.count.text())), 1),
        )

    def _apply_form(self) -> None:
        if self._index < 0 or self._index >= len(self.groups):
            return
        try:
            group = self.form_group(fallback_name=self.groups[self._index].name)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.groups[self._index] = group
        self._refresh_table()
        self._select_row(self._index)
        self.changed.emit()

    def _next_defaults(self) -> GroupSetting:
        used_ai = {group.eeg_ai for group in self.groups} | {group.emg_ai for group in self.groups}
        used_dio = {group.ttl_dio for group in self.groups}
        eeg = next((i for i in range(AI_CHANNELS) if i not in used_ai), 0)
        emg = next(
            (i for i in range(AI_CHANNELS) if i not in used_ai and i != eeg),
            min(eeg + 1, AI_CHANNELS - 1),
        )
        dio = next((i for i in range(TTL_CHANNELS) if i not in used_dio), 0)
        return GroupSetting(
            name=f"group_{len(self.groups) + 1}",
            eeg_ai=eeg,
            emg_ai=emg,
            movement_threshold=0.05,
            theta_delta_threshold=1.0,
            target_state="NREM",
            ttl_dio=dio,
            threshold_v=0.02,
            threshold_mode="above",
            threshold_n=4,
        )

    def _add(self) -> None:
        self.groups.append(self._next_defaults())
        self._refresh_table()
        self._select_row(len(self.groups) - 1)
        self.changed.emit()

    def _delete(self) -> None:
        if self._index < 0 or not self.groups:
            return
        del self.groups[self._index]
        self._refresh_table()
        if self.groups:
            self._select_row(min(self._index, len(self.groups) - 1))
        else:
            self._index = -1
            self._clear_form()
        self.changed.emit()


def _combo(values: list[str]) -> QComboBox:
    box = QComboBox()
    box.addItems(values)
    box.setMinimumWidth(140)
    return box
