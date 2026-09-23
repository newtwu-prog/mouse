"""Headless smoke check for the PyQt6 experiment page and the tkinter import.

    python -m qt_ui.smoke
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from PyQt6.QtWidgets import QApplication, QCheckBox, QLabel, QMessageBox

from protocol.framing import FrameReader, MsgType
from protocol.messages import (
    DataPacket,
    GroupData,
    hello_ack_payload,
    pack_data,
)
from qt_ui.app import create_app
from qt_ui.main_window import MainWindow
from qt_ui.ttl_labels import format_ttl_label, parse_ttl_label
from qt_ui.widgets.signal_bar import SignalToggleBar


class _FakeRt(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.ready = threading.Event()
        self.got_start = threading.Event()
        self.got_stop = threading.Event()
        self.got_config = threading.Event()
        self.port = 0
        self.error = ""

    def run(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        self.port = srv.getsockname()[1]
        srv.listen(1)
        srv.settimeout(8)
        self.ready.set()
        try:
            conn, _addr = srv.accept()
        except Exception as exc:
            self.error = f"accept: {exc}"
            return
        conn.settimeout(0.3)
        reader = FrameReader()
        seq = 1
        try:
            while not self.got_stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    chunk = b""
                if chunk:
                    for kind, _payload in reader.feed(chunk):
                        if kind == MsgType.HELLO:
                            conn.sendall(hello_ack_payload(simulate=True))
                        elif kind == MsgType.CONFIG:
                            self.got_config.set()
                        elif kind == MsgType.START:
                            self.got_start.set()
                        elif kind == MsgType.STOP:
                            self.got_stop.set()
                if self.got_start.is_set() and not self.got_stop.is_set() and seq <= 4:
                    conn.sendall(pack_data(_packet(seq)))
                    seq += 1
                    time.sleep(0.05)
        except Exception as exc:
            self.error = str(exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            srv.close()


def _packet(seq: int) -> DataPacket:
    fs = 200.0
    n = 50
    t = np.arange(n) / fs
    eeg = (0.25 * np.sin(2 * np.pi * 6 * t + seq)).astype(np.float32)
    emg = (0.08 * np.sin(2 * np.pi * 30 * t)).astype(np.float32)
    ttl = ((np.arange(n) // 10) % 2).astype(np.float32)
    group = GroupData(
        name="group_1",
        eeg=eeg,
        emg=emg,
        movement=0.21,
        delta=0.4,
        theta=0.15,
        theta_delta=0.37,
        state="NREM",
        cond1=True,
        cond2=False,
        ttl=bool(ttl[-1]),
        ttl_trace=ttl,
    )
    return DataPacket(elapsed_s=seq * n / fs, seq=seq, fs=fs, groups=[group])


def _pump(seconds: float = 0.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            text = modal.text()
            modal.reject()
            raise AssertionError(f"unexpected dialog: {text}")
        time.sleep(0.01)


def _wait(predicate, timeout: float = 4.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        _pump(0.05)
        if predicate():
            return True
    return False


def _check_labels(window: MainWindow) -> None:
    assert window.tabs.count() == 2
    assert window.tabs.tabText(0) == "1. 量測群組設定"
    assert window.tabs.tabText(1) == "2. 實驗顯示"
    assert window.tabs.currentIndex() == 0
    assert window.session.configured is False
    assert window.btn_start.isEnabled() is False
    assert window.groups_page.table.item(0, 9).text() == "CH0"
    assert window.groups_page.btn_confirm.text() == "套用設定"
    assert window.findChild(QLabel, "statusChip").text()
    assert window.port.text() == "7001"
    assert window.settings.autoscale.isChecked() is False
    assert window.settings.autoscale.text() == "Y軸自動縮放"
    assert window.settings.record_enabled.isChecked() is True
    before = len(window.groups_page.groups)
    window.groups_page.btn_add.click()
    assert len(window.groups_page.groups) == before + 1
    assert window.session.configured is False
    window.groups_page.btn_delete.click()
    assert len(window.groups_page.groups) == before
    window.groups_page.btn_confirm.click()
    assert window.session.configured is True
    window.tabs.setCurrentIndex(1)
    _pump(0.3)
    window.experiment.charts.apply_ratio()
    _pump(0.2)
    main_h = window.experiment.charts.main.height()
    mid_h = window.experiment.charts.energy.height()
    bot_h = window.experiment.charts.emg.height()
    assert main_h > mid_h * 1.8, (main_h, mid_h, bot_h)
    assert mid_h >= 70 and bot_h >= 70, (main_h, mid_h, bot_h)
    assert abs(mid_h - bot_h) < max(mid_h, 1) * 0.35, (main_h, mid_h, bot_h)
    texts = []
    for label in window.findChildren(QLabel):
        texts.append(label.text())
    blob = "\n".join(texts)
    for banned in ("條件一", "條件二", "Cond1", "Cond2", "輸出固定脈寬"):
        assert banned not in blob, banned
    assert window.findChild(QLabel, "hint") is None
    from qt_ui.widgets.charts import LEGEND_BACKDROP_ALPHA

    backdrop = window.experiment.charts._legends["main"].opts["brush"].color()
    assert backdrop.alpha() == LEGEND_BACKDROP_ALPHA
    assert 0 < backdrop.alpha() < 255
    assert "delta energy" in window.experiment.charts.legend_labels()
    assert "theta energy" in window.experiment.charts.legend_labels()
    assert "TTL" in window.experiment.charts.main.getAxis("right").labelText
    assert "TTL" not in window.experiment.charts.main.getAxis("left").labelText
    assert window.experiment.signals.height() <= 90
    assert window.experiment.live.btn_apply.text() == "即時更新"
    assert window.experiment.live.mode.itemText(0)
    emg = window.findChild(QCheckBox, "sigEMG")
    eeg = window.findChild(QCheckBox, "sigEEG")
    assert emg is not None and eeg is not None
    eeg.setChecked(False)
    assert eeg.isChecked() is False
    emg.setChecked(False)
    assert emg.isChecked() is True  # bottom EMG chart cannot go blank
    eeg.setChecked(True)


def _check_memory() -> None:
    bar = SignalToggleBar()
    bar.set_groups(["A", "B"])
    bar.combo.setCurrentText("A")
    bar._checks["EEG"].setChecked(False)
    bar._checks["delta"].setChecked(False)
    bar.combo.setCurrentText("B")
    assert bar._checks["EEG"].isChecked() is False  # B default off except we only default first group on
    # B was created with default False because index != 0.
    assert bar.is_on("A", "EEG") is False
    assert bar.is_on("A", "delta") is False
    assert bar.is_on("A", "theta") is True
    assert bar.is_on("B", "EMG") is False
    bar.combo.setCurrentText("A")
    assert bar._checks["EEG"].isChecked() is False
    assert bar._checks["theta"].isChecked() is True
    bar._checks["theta"].setChecked(False)
    assert bar._checks["theta"].isChecked() is True  # mid chart keeps theta
    bar._checks["EMG"].setChecked(False)
    assert bar.is_on("A", "EMG") is True  # only EMG on the bottom chart


def _check_ttl_labels() -> None:
    assert parse_ttl_label("CH0") == 0
    assert parse_ttl_label("DIO2") == 2
    assert parse_ttl_label("1") == 1
    assert format_ttl_label(0) == "CH0"
    assert format_ttl_label(1) == "CH1"


def _check_tkinter_import() -> None:
    import pc_ui

    assert hasattr(pc_ui, "PcApp")
    assert hasattr(pc_ui, "main")


def main() -> int:
    _check_ttl_labels()
    _check_tkinter_import()
    from qt_ui.session import DEFAULT_SETTINGS

    original_settings = DEFAULT_SETTINGS.read_bytes()
    app = create_app()
    try:
        _check_memory()
        server = _FakeRt()
        server.start()
        assert server.ready.wait(3), "fake RT did not bind"
        window = MainWindow()
        window.resize(1400, 1080)
        window.show()
        _pump(0.3)
        window.grab().save("/tmp/qt_groups_page.png")
        _check_labels(window)
        window.settings.record_enabled.setChecked(False)
        window.ip.setText("127.0.0.1")
        window.port.setText(str(server.port))
        window.btn_conn.click()
        assert _wait(lambda: window.session.connected), "connect did not complete"
        assert server.got_config.wait(2), "CONFIG was not sent"
        window.btn_start.click()
        assert _wait(lambda: "實驗中" in window.status.text(), 5), window.status.text()
        assert server.got_start.is_set()
        assert window.tabs.currentIndex() == 1
        assert window.experiment.charts.point_count("group_1", "EEG") > 0
        assert window.experiment.charts.point_count("group_1", "TTL") > 0
        assert window.experiment.charts.point_count("group_1", "delta") > 0
        assert "NREM" in window.experiment.status._cards["group_1"].state.text()
        window.settings.span.setText("6")
        window._apply_span()
        _pump(0.3)
        charts = window.experiment.charts
        for plot in (charts.main, charts.energy, charts.emg):
            low, high = plot.getViewBox().viewRange()[0]
            assert abs(low) < 1e-3 and abs(high - 6.0) < 1e-2, (plot, low, high)
        ttl_low, ttl_high = charts._ttl_vb.viewRange()[0]
        assert abs(ttl_low) < 1e-3 and abs(ttl_high - 6.0) < 1e-2, (ttl_low, ttl_high)
        axis = charts.emg.getAxis("bottom")
        visible_ticks = []
        for _step, values in axis.tickValues(0.0, 6.0, max(charts.emg.width(), 200)):
            visible_ticks.extend(values)
        assert visible_ticks
        assert min(visible_ticks) >= -1e-6, visible_ticks
        window.experiment.charts.apply_ratio()
        _pump(0.2)
        shot = Path("/tmp/qt_experiment_page.png")
        window.grab().save(str(shot))
        window.btn_stop.click()
        assert _wait(lambda: server.got_stop.is_set()), "STOP was not sent"
        window.close()
        if server.error:
            raise AssertionError(server.error)
        print(f"qt smoke ok {shot}")
        app.processEvents()
        return 0
    finally:
        DEFAULT_SETTINGS.write_bytes(original_settings)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"qt smoke failed: {exc}", file=sys.stderr)
        raise
