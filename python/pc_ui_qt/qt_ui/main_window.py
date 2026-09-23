"""Top-level window: connection chrome, group editor, experiment page, log."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from processing.groups import load_groups, save_groups
from protocol.messages import DataPacket
from qt_ui.session import DEFAULT_SETTINGS, ROOT, TESTDATA, RunOptions, SessionController
from qt_ui.theme import polish_combo_popups
from qt_ui.widgets.experiment_page import ExperimentPage
from qt_ui.widgets.group_editor import GroupEditorPage
from qt_ui.widgets.settings_row import SettingsRow
from tdms_replay import inspect_tdms

_LOG_PATH = ROOT / "qt_ui_debug.log"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EEG mice  /  PC（PyQt6）")
        self.resize(1400, 1080)
        self.setMinimumSize(1180, 900)

        self.session = SessionController()
        fields = self.session.initial_fields()

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        root.addLayout(self._build_connection(fields))
        self.settings = SettingsRow(fields)
        root.addWidget(self.settings)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #d0d7e2;")
        root.addWidget(line)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.groups_page = GroupEditorPage(self.session.groups)
        self.experiment = ExperimentPage()
        self.experiment.set_groups(self.session.groups)
        self.tabs.addTab(self.groups_page, "1. 量測群組設定")
        self.tabs.addTab(self.experiment, "2. 實驗顯示")
        self.tabs.setCurrentIndex(0)
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)

        self._build_log(root)

        self._wire()
        self._aligning = False
        self._run_active = False
        self._set_status("未連線到 RT", "idle")
        self._log(self.session.startup_note())
        self._log("Log 預設隱藏，按「顯示 Log」可開啟。")
        self._sync_buttons()
        if self.session.groups:
            self.experiment.highlight(self.session.groups[0].name)
        polish_combo_popups(self)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.session.poll)
        self._timer.start(40)

    def _build_connection(self, fields: dict) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("RT IP"))
        self.ip = QLineEdit(fields["ip"])
        self.ip.setObjectName("ipEdit")
        self.ip.setFixedWidth(150)
        row.addWidget(self.ip)
        row.addWidget(QLabel("port"))
        self.port = QLineEdit(fields["port"])
        self.port.setObjectName("portEdit")
        self.port.setFixedWidth(72)
        row.addWidget(self.port)
        self.btn_conn = QPushButton("連線 RT")
        self.btn_conn.setObjectName("connectButton")
        self.btn_disc = QPushButton("斷線")
        self.btn_disc.setObjectName("disconnectButton")
        self.btn_start = QPushButton("開始實驗")
        self.btn_start.setObjectName("startButton")
        self.btn_stop = QPushButton("停止實驗")
        self.btn_stop.setObjectName("stopButton")
        for button in (self.btn_conn, self.btn_disc, self.btn_start, self.btn_stop):
            row.addWidget(button)
        row.addStretch(1)
        self.status = QLabel("未連線到 RT")
        self.status.setObjectName("statusChip")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.status)
        return row

    def _build_log(self, root: QVBoxLayout) -> None:
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        self.log_view.setFixedHeight(120)
        self.log_view.hide()
        self._log_visible = False
        bar = QHBoxLayout()
        self.btn_log = QPushButton("顯示 Log")
        self.btn_log.setObjectName("logToggle")
        self.btn_log.clicked.connect(self._toggle_log)
        bar.addWidget(self.btn_log)
        bar.addStretch(1)
        root.addWidget(self.log_view)
        root.addLayout(bar)

    def _wire(self) -> None:
        self.btn_conn.clicked.connect(self._connect)
        self.btn_disc.clicked.connect(self.session.disconnect)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.settings.tdms_browse.clicked.connect(self._browse_tdms)
        self.settings.offline_start.clicked.connect(self._start_offline)
        self.settings.offline_stop.clicked.connect(self.session.stop_offline)
        self.settings.record_browse.clicked.connect(self._browse_record)
        self.settings.span.editingFinished.connect(self._apply_span)
        self.settings.autoscale.toggled.connect(self.experiment.set_autoscale)
        self.groups_page.changed.connect(self._on_groups_edited)
        self.groups_page.btn_confirm.clicked.connect(self._confirm_settings)
        self.groups_page.btn_load.clicked.connect(self._load_settings)
        self.groups_page.btn_save.clicked.connect(self._save_settings)
        self.experiment.live.group_changed.connect(self._align_group)
        self.experiment.signals.group_changed.connect(self._align_group)
        self.experiment.live.apply_clicked.connect(self._live_update)
        self.settings.plot_group.currentTextChanged.connect(self._align_group)
        self.settings.aux_changed.connect(self._on_aux_changed)
        self.session.statusChanged.connect(self._set_status)
        self.session.logMessage.connect(self._log)
        self.session.buttonsChanged.connect(self._sync_buttons)
        self.session.packetReceived.connect(self._on_packet)
        self.session.connectFailed.connect(self._on_connect_failed)
        self.session.actionFailed.connect(self._on_action_failed)

    def _options(self) -> RunOptions | None:
        try:
            period = float(self.settings.period.text() or 5)
            epoch = float(self.settings.epoch.text() or 12)
            ttl_out = float(self.settings.ttl_out.text() or 10)
            ttl_ref = float(self.settings.ttl_ref.text() or 0)
            span = float(self.settings.span.text() or 1)
        except ValueError:
            QMessageBox.warning(self, "參數錯誤", "取樣、狀態窗、TTL 時間與 Display 必須是數字。單位為 ms 或 s。")
            return None
        return RunOptions(
            period_ms=period,
            epoch_sec=epoch,
            ttl_enabled=self.settings.ttl_enabled.isChecked(),
            ttl_output_ms=ttl_out,
            ttl_refractory_ms=ttl_ref,
            record_enabled=self.settings.record_enabled.isChecked(),
            record_root=self.settings.record_root.text().strip(),
            tdms_path=self.settings.tdms_path.text().strip(),
            tdms_channels=self.settings.tdms_channels.text().strip(),
            span_sec=span,
        )

    def _connect(self) -> None:
        options = self._options()
        if options is None:
            return
        self._apply_span()
        self.session.connect_to(self.ip.text(), self.port.text(), options)

    def _start(self) -> None:
        options = self._options()
        if options is None:
            return
        self._apply_span()
        fs = 1_000_000.0 / max(options.period_ms * 1000.0, 1.0)
        self.experiment.reset_waves(fs)
        self.session.start_live(options)
        self.tabs.setCurrentIndex(1)

    def _stop(self) -> None:
        self.session.stop_all()

    def _start_offline(self) -> None:
        options = self._options()
        if options is None:
            return
        self._apply_span()
        try:
            fs = 1_000_000.0 / max(options.period_ms * 1000.0, 1.0)
        except ValueError:
            fs = 200.0
        self.experiment.reset_waves(fs)
        self.session.start_offline(options)
        self.tabs.setCurrentIndex(1)

    def _apply_span(self) -> None:
        try:
            span = float(self.settings.span.text() or 1)
        except ValueError:
            span = 1.0
            self.settings.span.setText("1")
        self.experiment.set_span(span)

    def _on_tab(self, index: int) -> None:
        if index == 1:
            self.experiment.charts.apply_ratio()

    def _align_group(self, name: str) -> None:
        """顯示群組、勾選群組、即時群組 stay on the same group."""
        if not name or self._aligning:
            return
        self._aligning = True
        try:
            if (
                self.settings.plot_group.findText(name) >= 0
                and self.settings.plot_group.currentText() != name
            ):
                self.settings.plot_group.setCurrentText(name)
            self.experiment.highlight(name)
            self.experiment.live.select_group(name)
            self.experiment.signals.select_group(name)
        finally:
            self._aligning = False

    def _on_aux_changed(self) -> None:
        QTimer.singleShot(0, self.experiment.charts.apply_ratio)

    def _on_groups_edited(self) -> None:
        self.session.note_groups_dirty(list(self.groups_page.groups))
        self.groups_page.set_status("設定已變更，請再按「套用設定」。")

    def _confirm_settings(self) -> None:
        options = self._options()
        if options is None:
            return
        if self.groups_page._index >= 0 and self.groups_page.groups:
            self.groups_page._apply_form()
        err = self.session.confirm_groups(list(self.groups_page.groups), options)
        if err:
            QMessageBox.critical(self, "群組設定不完整", err)
            return
        self._sync_group_choices()
        self.experiment.set_groups(self.session.groups)
        self._align_group(self.settings.plot_group.currentText())
        pushed = ""
        if self.session.connected:
            pushed = self.session.push_config(options)
            if not pushed:
                self._log("即時參數已傳送至 RT；從下一批資料開始生效")
        self.groups_page.set_status(
            f"已確認 {len(self.session.groups)} 個群組。可連線 RT，或用下方「開始離線實驗」。"
        )
        if pushed:
            QMessageBox.warning(self, "下發失敗", pushed)

    def _load_settings(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "載入群組設定",
            str(DEFAULT_SETTINGS.parent),
            "JSON (*.json)",
        )
        if not path:
            return
        self.load_settings_from(Path(path))

    def load_settings_from(self, path: Path) -> None:
        groups, raw = load_groups(path)
        self.session.settings.update(raw)
        if "sample_period_us" in raw:
            self.settings.period.setText(str(max(int(raw["sample_period_us"]) // 1000, 1)))
        if "epoch_sec" in raw:
            self.settings.epoch.setText(str(raw["epoch_sec"]))
        if "ttl_output_ms" in raw or "ttl_pulse_ms" in raw:
            self.settings.ttl_out.setText(str(raw.get("ttl_output_ms", raw.get("ttl_pulse_ms", 10.0))))
        if "ttl_refractory_ms" in raw:
            self.settings.ttl_ref.setText(str(raw["ttl_refractory_ms"]))
        if "ttl_enabled" in raw:
            self.settings.ttl_enabled.setChecked(bool(raw["ttl_enabled"]))
        self.groups_page.set_groups(groups)
        self._log(f"已載入 {path}")

    def _save_settings(self) -> None:
        options = self._options()
        if options is None:
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "儲存群組設定",
            str(DEFAULT_SETTINGS.parent),
            "JSON (*.json)",
        )
        if not path:
            return
        save_groups(Path(path), list(self.groups_page.groups), self.session._timing_extra(options))
        self._log(f"已儲存 {path}")

    def _live_update(self) -> None:
        options = self._options()
        if options is None:
            return
        if not self.session.configured:
            QMessageBox.information(self, "尚未套用設定", "請先在第一分頁按「套用設定」。")
            self.tabs.setCurrentIndex(0)
            return
        try:
            movement, ratio, mode, threshold = self.experiment.live.values()
        except ValueError:
            QMessageBox.warning(self, "參數錯誤", "即時參數必須是數字。")
            return
        name = self.experiment.live.selected_name()
        err = self.session.apply_live(name, movement, ratio, mode, threshold, options)
        if err:
            self.experiment.live.clear_note()
            QMessageBox.warning(self, "即時更新", err)
            return
        self.groups_page.groups = list(self.session.groups)
        self.groups_page._refresh_table()
        if self.groups_page.groups:
            self.groups_page._select_row(min(max(self.groups_page._index, 0), len(self.groups_page.groups) - 1))
        self.experiment.status.set_groups(self.session.groups)
        self.experiment.live.set_groups(self.session.groups, selected=name)
        self._align_group(name)
        self.experiment.live.mark_applied(name)
        self.groups_page.set_status(f"已即時更新群組 {name} 的閾值參數。")

    def _sync_group_choices(self) -> None:
        names = [group.name for group in self.session.groups]
        current = self.settings.plot_group.currentText()
        self.settings.plot_group.blockSignals(True)
        self.settings.plot_group.clear()
        self.settings.plot_group.addItems(names)
        if current in names:
            self.settings.plot_group.setCurrentText(current)
        elif names:
            self.settings.plot_group.setCurrentIndex(0)
        self.settings.plot_group.blockSignals(False)

    def _browse_tdms(self) -> None:
        initial = str(TESTDATA if TESTDATA.is_dir() else ROOT)
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "載入 TDMS 檔案",
            initial,
            "TDMS (*.tdms);;All (*.*)",
        )
        if not path:
            return
        self.settings.tdms_path.setText(path)
        try:
            info = inspect_tdms(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "讀取 TDMS 失敗", str(exc))
            self._log(f"TDMS 檢查失敗: {exc}")
            return
        names: list[str] = []
        for group in info.get("groups", []):
            for channel in group.get("channels", []):
                label = str(channel.get("name", ""))
                if label:
                    names.append(label)
        preview = ", ".join(names[:8])
        more = "…" if len(names) > 8 else ""
        self._log(f"已載入 TDMS: {path}")
        self._log(f"通道 ({len(names)}): {preview}{more}")
        if not self.settings.tdms_channels.text().strip() and len(names) >= 2:
            self.settings.tdms_channels.setText(",".join(names[:2]))

    def _browse_record(self) -> None:
        initial = self.settings.record_root.text().strip() or str(ROOT / "recordings")
        path = QFileDialog.getExistingDirectory(self, "選擇存檔資料夾", initial)
        if not path:
            return
        self.settings.record_root.setText(path)
        self._log(f"存檔路徑：{path}")

    def _on_packet(self, packet: object) -> None:
        if not isinstance(packet, DataPacket):
            return
        self.experiment.append_packet(packet)
        npts = 0
        if self.experiment.waves.waves:
            any_wave = next(iter(self.experiment.waves.waves.values()))
            npts = len(any_wave.get("eeg", []))
        self._set_status(
            f"實驗中  t={packet.elapsed_s:.1f}s  fs={packet.fs:.1f}Hz  "
            f"窗={self.experiment.waves.span_sec:g}s/{npts}點  seq={packet.seq}",
            "run",
        )

    def _on_connect_failed(self, message: str) -> None:
        QMessageBox.critical(self, "連線 RT 失敗", message)

    def _on_action_failed(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _sync_buttons(self) -> None:
        connected = self.session.connected
        offline = self.session.offline_running
        busy = connected or offline or self.session.connecting
        self.btn_conn.setEnabled(not busy)
        self.btn_disc.setEnabled(connected)
        self.btn_start.setEnabled(connected and self.session.configured)
        self.btn_stop.setEnabled(connected or offline)
        self.settings.tdms_browse.setEnabled(not offline)
        self.settings.record_browse.setEnabled(not self.session.recording)
        self.settings.offline_start.setEnabled(self.session.configured and not busy)
        self.settings.offline_stop.setEnabled(offline)
        self.ip.setEnabled(not busy)
        self.port.setEnabled(not busy)

    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        self.log_view.setVisible(self._log_visible)
        self.btn_log.setText("隱藏 Log" if self._log_visible else "顯示 Log")

    def _set_status(self, text: str, state: str) -> None:
        self.status.setText(text)
        self.status.setProperty("state", state)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        running = state == "run"
        if running and not self._run_active:
            self.settings.set_aux_open(False)
        elif self._run_active and not running:
            self.settings.set_aux_open(True)
        self._run_active = running

    def _log(self, text: str) -> None:
        line = text.rstrip()
        self.log_view.appendPlainText(line)
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(datetime.now().strftime("%H:%M:%S ") + line + "\n")
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self.session.close()
        super().closeEvent(event)
