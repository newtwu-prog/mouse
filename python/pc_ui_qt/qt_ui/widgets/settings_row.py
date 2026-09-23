"""Sampling, display/save, TTL timing, offline TDMS, and the save-path row."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def _edit(value: str, name: str, width: int = 88) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setObjectName(name)
    edit.setFixedWidth(width)
    return edit


def _form(box: QGroupBox) -> QFormLayout:
    form = QFormLayout(box)
    form.setContentsMargins(10, 8, 10, 8)
    form.setHorizontalSpacing(8)
    form.setVerticalSpacing(6)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    return form


class SettingsRow(QWidget):
    """Controls that sit between the connection bar and the charts."""

    aux_changed = Signal()

    def __init__(self, fields: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        panels = QHBoxLayout()
        panels.setSpacing(8)
        sampling = QGroupBox("取樣設定")
        display = QGroupBox("顯示與存檔")
        ttl = QGroupBox("TTL 時間設定")
        panels.addWidget(sampling, 0)
        panels.addWidget(display, 0)
        panels.addWidget(ttl, 1)
        root.addLayout(panels)

        sample_form = _form(sampling)
        self.period = _edit(fields["period_ms"], "periodEdit")
        self.epoch = _edit(fields["epoch_sec"], "epochEdit")
        sample_form.addRow("取樣週期 ms", self.period)
        sample_form.addRow("狀態窗 s", self.epoch)

        display_row = QHBoxLayout(display)
        display_row.setContentsMargins(10, 8, 10, 8)
        display_row.setSpacing(16)
        checks = QVBoxLayout()
        checks.setSpacing(4)
        self.ttl_enabled = QCheckBox("啟用 TTL（由 RT 輸出）")
        self.ttl_enabled.setObjectName("ttlEnableCheck")
        self.ttl_enabled.setChecked(bool(fields["ttl_enabled"]))
        self.record_enabled = QCheckBox("PC 存檔")
        self.record_enabled.setObjectName("recordCheck")
        self.record_enabled.setChecked(bool(fields["record_enabled"]))
        self.autoscale = QCheckBox("Y軸自動縮放")
        self.autoscale.setObjectName("autoscaleCheck")
        self.autoscale.setChecked(False)
        checks.addWidget(self.ttl_enabled)
        checks.addWidget(self.record_enabled)
        checks.addWidget(self.autoscale)
        checks.addStretch(1)
        display_row.addLayout(checks)

        field_form = QFormLayout()
        field_form.setHorizontalSpacing(8)
        field_form.setVerticalSpacing(6)
        field_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.span = _edit(fields["span_sec"], "spanEdit", 72)
        self.plot_group = QComboBox()
        self.plot_group.setObjectName("plotGroupCombo")
        self.plot_group.setFixedWidth(140)
        self.plot_group.addItems(fields["group_names"])
        field_form.addRow("Display s", self.span)
        field_form.addRow("顯示群組", self.plot_group)
        display_row.addLayout(field_form)
        display_row.addStretch(1)

        ttl_form = _form(ttl)
        self.ttl_out = _edit(fields["ttl_output_ms"], "ttlOutEdit")
        self.ttl_ref = _edit(fields["ttl_refractory_ms"], "ttlRefEdit")
        ttl_form.addRow("輸出時間 ms", self.ttl_out)
        ttl_form.addRow("不應期時間 ms", self.ttl_ref)

        aux_bar = QHBoxLayout()
        aux_bar.setContentsMargins(0, 0, 0, 0)
        self.aux_toggle = QPushButton("收合離線與存檔")
        self.aux_toggle.setObjectName("auxToggle")
        self.aux_toggle.setCheckable(True)
        self.aux_toggle.setChecked(True)
        self.aux_toggle.toggled.connect(self._on_aux_toggled)
        aux_bar.addWidget(self.aux_toggle)
        aux_bar.addStretch(1)
        root.addLayout(aux_bar)

        self.aux = QWidget()
        self.aux.setObjectName("auxBlock")
        aux_layout = QVBoxLayout(self.aux)
        aux_layout.setContentsMargins(0, 0, 0, 0)
        aux_layout.setSpacing(8)

        offline = QGroupBox("離線 TDMS 測試（不需連 cRIO）")
        offline_row = QHBoxLayout(offline)
        offline_row.setContentsMargins(10, 8, 10, 8)
        offline_row.setSpacing(8)
        offline_row.addWidget(QLabel("TDMS"))
        self.tdms_path = QLineEdit()
        self.tdms_path.setObjectName("tdmsPathEdit")
        self.tdms_path.setMinimumWidth(180)
        offline_row.addWidget(self.tdms_path, 1)
        self.tdms_browse = QPushButton("載入 TDMS…")
        self.tdms_browse.setObjectName("tdmsBrowse")
        offline_row.addWidget(self.tdms_browse)
        offline_row.addSpacing(12)
        offline_row.addWidget(QLabel("通道(可選)"))
        self.tdms_channels = QLineEdit()
        self.tdms_channels.setObjectName("tdmsChannels")
        self.tdms_channels.setFixedWidth(160)
        offline_row.addWidget(self.tdms_channels)
        self.offline_start = QPushButton("開始離線實驗")
        self.offline_start.setObjectName("offlineStart")
        self.offline_stop = QPushButton("停止離線")
        self.offline_stop.setObjectName("offlineStop")
        offline_row.addWidget(self.offline_start)
        offline_row.addWidget(self.offline_stop)
        aux_layout.addWidget(offline)

        save_host = QWidget()
        save = QHBoxLayout(save_host)
        save.setContentsMargins(2, 2, 2, 2)
        save.setSpacing(8)
        save.addWidget(QLabel("存檔路徑"))
        self.record_root = QLineEdit(fields["record_root"])
        self.record_root.setObjectName("recordPathEdit")
        save.addWidget(self.record_root, 1)
        self.record_browse = QPushButton("選擇資料夾…")
        self.record_browse.setObjectName("recordBrowse")
        save.addWidget(self.record_browse)
        aux_layout.addWidget(save_host)
        root.addWidget(self.aux)

    def set_aux_open(self, open_: bool) -> None:
        """Show or hide the offline-test row and the save-path row together."""
        self.aux_toggle.blockSignals(True)
        self.aux_toggle.setChecked(bool(open_))
        self.aux_toggle.blockSignals(False)
        self._apply_aux(bool(open_))

    def _on_aux_toggled(self, open_: bool) -> None:
        self._apply_aux(bool(open_))

    def _apply_aux(self, open_: bool) -> None:
        self.aux.setVisible(open_)
        self.aux_toggle.setText("收合離線與存檔" if open_ else "展開離線與存檔")
        self.aux_changed.emit()
