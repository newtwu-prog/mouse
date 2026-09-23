"""Placeholder pages. Full editors are later work; wording already uses 判斷*."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
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

from processing.groups import THRESHOLD_MODE_LABELS, GroupSetting, ai_label
from qt_ui.ttl_labels import format_ttl_label

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


class LiveParamsPage(QWidget):
    """Second page. Live edits are not wired yet; values are read from loaded groups."""

    def __init__(self, groups: list[GroupSetting], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups = list(groups)
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        note = QLabel(
            "即時參數會放在這一頁右側。\n\n"
            "此版本只顯示已載入的判斷參數，還不能改完就下發。"
            "判斷在 RT 完成，PC 只顯示結果。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        note.setMinimumWidth(280)
        root.addWidget(note, 1)

        panel = QFrame()
        panel.setObjectName("statusPanel")
        panel.setMinimumWidth(360)
        form = QFormLayout(panel)
        form.setContentsMargins(16, 16, 16, 16)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.group_box = QComboBox()
        self.group_box.setObjectName("liveGroupCombo")
        self.group_box.addItems([g.name for g in groups])
        self.move = _readonly()
        self.ratio = _readonly()
        self.state = _readonly()
        self.mode = _readonly()
        self.threshold = _readonly()
        self.count = _readonly()
        form.addRow("群組", self.group_box)
        form.addRow("Movement 閾值", self.move)
        form.addRow("θ/δ 閾值", self.ratio)
        form.addRow("判斷狀態", self.state)
        form.addRow("判斷方式", self.mode)
        form.addRow("判斷閾值 (V)", self.threshold)
        form.addRow("連續判斷點數", self.count)
        button = QPushButton("即時更新")
        button.setEnabled(False)
        button.setToolTip("後續版本才會把判斷參數下發到 RT。")
        form.addRow(button)
        self.group_box.currentTextChanged.connect(self._fill)
        self._fill(self.group_box.currentText())
        root.addWidget(panel, 0)


    def _fill(self, name: str) -> None:
        group = next((item for item in self._groups if item.name == name), None)
        if group is None:
            return
        self.move.setText(f"{group.movement_threshold:g}")
        self.ratio.setText(f"{group.theta_delta_threshold:g}")
        self.state.setText(group.target_state)
        self.mode.setText(THRESHOLD_MODE_LABELS.get(group.threshold_mode, group.threshold_mode))
        self.threshold.setText(f"{group.threshold_v:g}")
        self.count.setText(str(int(group.threshold_n)))


class GroupsPage(QWidget):
    """Read-only view of loaded groups. Editing stays on the tkinter UI for now."""

    def __init__(self, groups: list[GroupSetting], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        note = QLabel(
            "群組編輯將於後續版本完成。下面是目前已載入的設定。"
            "TTL 通道顯示為 CH0、CH1…；若設定文字是舊的 DIO0 這類寫法，讀取時仍會接受。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        root.addWidget(note)

        table = QTableWidget(len(groups), len(_HEADERS))
        table.setObjectName("groupsTable")
        table.setHorizontalHeaderLabels(list(_HEADERS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, group in enumerate(groups):
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
                table.setItem(row, column, QTableWidgetItem(value))
        root.addWidget(table, 1)


def _readonly() -> QLineEdit:
    edit = QLineEdit()
    edit.setReadOnly(True)
    edit.setFixedWidth(140)
    return edit
