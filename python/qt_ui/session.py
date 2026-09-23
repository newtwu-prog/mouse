"""PC session: TCP client, offline TDMS runner, and recorder.

The UI asks the cRIO (or the existing offline runner) to compute judgment.
This module only connects, forwards settings, and queues packets for display.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue

from PyQt6.QtCore import QObject, pyqtSignal as Signal

from config import DEFAULT_DEVICE_KEY, DEVICES
from offline_tdms import OfflineTdmsRunner
from pc_client import RtTcpClient
from processing.groups import group_to_dict, load_groups, validate_groups
from protocol.messages import DEFAULT_PORT, DataPacket
from recorder import SessionRecorder

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "settings" / "default_groups.json"
RECORD_ROOT = ROOT / "recordings"
TESTDATA = ROOT / "testdata"
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass
class RunOptions:
    period_ms: float
    epoch_sec: float
    ttl_enabled: bool
    ttl_output_ms: float
    ttl_refractory_ms: float
    record_enabled: bool
    record_root: str
    tdms_path: str = ""
    tdms_channels: str = ""
    span_sec: float = 1.0


def local_record_root(raw: str) -> str:
    """Show a usable folder when settings still hold a Windows path."""
    text = (raw or "").strip()
    if not text:
        return str(RECORD_ROOT)
    if os.name != "nt" and _WINDOWS_PATH.match(text):
        return str(RECORD_ROOT)
    return text


class SessionController(QObject):
    statusChanged = Signal(str, str)  # text, chip state: idle|busy|ok|run|bad
    logMessage = Signal(str)
    buttonsChanged = Signal()
    packetReceived = Signal(object)
    connectFailed = Signal(str)
    actionFailed = Signal(str, str)  # title, message
    _connect_result = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        loaded, self.settings = load_groups(DEFAULT_SETTINGS)
        self.groups = list(loaded)
        errors = validate_groups(self.groups)
        self.configured = not errors
        self.load_error = "\n".join(errors)

        host = str(self.settings.get("rt_ip") or DEVICES[DEFAULT_DEVICE_KEY].ip)
        self.client = RtTcpClient(host, DEFAULT_PORT)
        self.client.on_packet = self._on_packet
        self.client.on_message = self._on_message
        self.offline = OfflineTdmsRunner(on_data=self._on_packet, on_log=self._on_offline_log)
        self.updates: Queue = Queue(maxsize=32)
        self.recorder: SessionRecorder | None = None
        self._connecting = False
        self._link_up = False
        self._last_options: RunOptions | None = None
        self._connect_result.connect(self._finish_connect)

    @property
    def connected(self) -> bool:
        return bool(self.client.connected)

    @property
    def offline_running(self) -> bool:
        return bool(self.offline.running)

    @property
    def recording(self) -> bool:
        return self.recorder is not None

    @property
    def connecting(self) -> bool:
        return self._connecting

    def initial_fields(self) -> dict:
        period_us = int(self.settings.get("sample_period_us", 5000))
        record_raw = str(self.settings.get("record_root", str(RECORD_ROOT)))
        return {
            "ip": self.client.host,
            "port": str(self.client.port or DEFAULT_PORT),
            "period_ms": str(max(period_us // 1000, 1)),
            "epoch_sec": str(self.settings.get("epoch_sec", 12.0)),
            "ttl_enabled": bool(self.settings.get("ttl_enabled", True)),
            "ttl_output_ms": str(
                self.settings.get("ttl_output_ms", self.settings.get("ttl_pulse_ms", 10.0))
            ),
            "ttl_refractory_ms": str(self.settings.get("ttl_refractory_ms", 0.0)),
            "record_enabled": True,
            "record_root": local_record_root(record_raw),
            "span_sec": "1",
            "group_names": [g.name for g in self.groups],
        }

    def startup_note(self) -> str:
        if self.configured:
            return f"已載入 {len(self.groups)} 個群組，可連線 RT。判斷在 RT，PC 只顯示與存檔。"
        return "群組設定不完整：" + self.load_error

    def connect_to(self, host: str, port_text: str, options: RunOptions | None = None) -> None:
        if self.offline.running:
            self.actionFailed.emit("離線測試中", "請先停止離線 TDMS 測試，再連線 RT。")
            return
        host = host.strip()
        if not host:
            self.actionFailed.emit("連線 RT 失敗", "請輸入 RT IP。")
            return
        try:
            port = int(port_text)
        except ValueError:
            self.actionFailed.emit("連線 RT 失敗", f"埠必須是整數：{port_text}")
            return
        if options is not None:
            self._last_options = options
        self._connecting = True
        self.statusChanged.emit("連線中…", "busy")
        self.buttonsChanged.emit()

        def work() -> None:
            try:
                self.client.host = host
                self.client.port = port
                self.client.connect(timeout=5.0)
                err = ""
            except Exception as exc:
                err = str(exc)
            self._connect_result.emit(err)

        threading.Thread(target=work, name="qt-rt-connect", daemon=True).start()

    def _finish_connect(self, err: str) -> None:
        self._connecting = False
        if err:
            self._link_up = False
            self.statusChanged.emit("連線失敗", "bad")
            self.logMessage.emit(f"連線失敗：{err}")
            self.connectFailed.emit(err)
            self.buttonsChanged.emit()
            return
        self._link_up = True
        self.statusChanged.emit("已連線", "ok")
        self.logMessage.emit(f"已連線 {self.client.host}:{self.client.port}")
        self.buttonsChanged.emit()
        if self.configured:
            pushed = self.push_config(self._last_options)
            if pushed:
                self.actionFailed.emit("下發失敗", pushed)

    def disconnect(self) -> None:
        self._link_up = False
        self._stop_recording()
        self.client.close()
        self.statusChanged.emit("已斷線", "idle")
        self.logMessage.emit("已斷線")
        self.buttonsChanged.emit()

    def start_live(self, options: RunOptions) -> None:
        if self.offline.running:
            self.actionFailed.emit("離線測試中", "請先停止離線 TDMS 測試。")
            return
        if not self.client.connected:
            self.actionFailed.emit("尚未連線", "請先連線 RT。")
            return
        if not self.configured:
            self.actionFailed.emit("尚未確認", "群組設定不完整，無法開始實驗。")
            return
        self._last_options = options
        pushed = self.push_config(options)
        if pushed:
            self.actionFailed.emit("下發失敗", pushed)
            return
        if options.record_enabled:
            try:
                root = self._record_dir(options.record_root)
                self.recorder = SessionRecorder(root)
                meta = self.settings_dict(options)
                meta["mode"] = "online_rt"
                self.recorder.write_meta(meta)
                self.logMessage.emit(f"開始存檔  {self.recorder.dir}")
            except Exception as exc:
                self.recorder = None
                self.actionFailed.emit("存檔失敗", str(exc))
                return
        try:
            self.client.send_start()
        except Exception as exc:
            self._stop_recording()
            self.actionFailed.emit("開始失敗", str(exc))
            return
        self.statusChanged.emit("實驗進行中（判斷在 RT）", "run")
        self.logMessage.emit("START 已送到 RT")
        self.buttonsChanged.emit()

    def stop_all(self) -> None:
        try:
            if self.client.connected:
                self.client.send_stop()
        except Exception:
            pass
        if self.offline.running:
            self.offline.stop()
        self._stop_recording()
        if self.client.connected:
            self.statusChanged.emit("已停止", "ok")
        else:
            self.statusChanged.emit("已停止", "idle")
        self.logMessage.emit("STOP")
        self.buttonsChanged.emit()

    def start_offline(self, options: RunOptions) -> None:
        if self.client.connected or self._connecting:
            self.actionFailed.emit("已連線 RT", "請先斷線 RT，再做離線 TDMS 測試。")
            return
        if not self.configured:
            self.actionFailed.emit("尚未確認", "群組設定不完整，無法開始離線實驗。")
            return
        path = Path(options.tdms_path.strip())
        if not path.is_file():
            self.actionFailed.emit("缺少檔案", "請先按「載入 TDMS…」選擇檔案。")
            return
        self._last_options = options
        if options.record_enabled:
            try:
                root = self._record_dir(options.record_root)
                self.recorder = SessionRecorder(root)
                meta = self.settings_dict(options)
                meta["mode"] = "offline_tdms"
                meta["tdms_path"] = str(path)
                self.recorder.write_meta(meta)
                self.logMessage.emit(f"開始存檔  {self.recorder.dir}")
            except Exception as exc:
                self.recorder = None
                self.actionFailed.emit("存檔失敗", str(exc))
                return
        channels = _parse_channels(options.tdms_channels)
        self.offline.start(
            path,
            self.groups,
            ttl_enabled=options.ttl_enabled,
            ttl_output_ms=options.ttl_output_ms,
            ttl_refractory_ms=options.ttl_refractory_ms,
            epoch_sec=options.epoch_sec,
            speed=1.0,
            loop=True,
            channel_names=channels,
        )
        self.statusChanged.emit("離線測試進行中", "run")
        self.logMessage.emit(f"離線 TDMS 開始  {path.name}")
        self.buttonsChanged.emit()

    def stop_offline(self) -> None:
        if self.offline.running:
            self.offline.stop()
        self._stop_recording()
        self.statusChanged.emit("離線測試已停止", "idle")
        self.logMessage.emit("離線測試已停止")
        self.buttonsChanged.emit()

    def push_config(self, options: RunOptions | None) -> str:
        if options is None or not self.client.connected:
            return ""
        try:
            self.client.send_config(self.settings_dict(options))
            self.logMessage.emit("已下發設定到 RT")
            return ""
        except Exception as exc:
            return str(exc)

    def settings_dict(self, options: RunOptions) -> dict:
        return {
            "sample_period_us": _period_us(options.period_ms),
            "epoch_sec": float(options.epoch_sec),
            "ttl_enabled": bool(options.ttl_enabled),
            "ttl_output_ms": float(options.ttl_output_ms),
            "ttl_refractory_ms": float(options.ttl_refractory_ms),
            "record_root": options.record_root.strip(),
            "groups": [group_to_dict(g) for g in self.groups],
        }

    def close(self) -> None:
        self._link_up = False
        try:
            if self.client.connected:
                self.client.send_stop()
        except Exception:
            pass
        if self.offline.running:
            self.offline.stop()
        self._stop_recording()
        self.client.close()

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.updates.get_nowait()
                if kind == "data":
                    if self.recorder is not None:
                        try:
                            self.recorder.append_packet(payload)
                        except Exception as exc:
                            self.logMessage.emit(f"存檔錯誤：{exc}")
                    self.packetReceived.emit(payload)
                elif kind == "msg":
                    self._handle_msg(payload)
                elif kind == "log":
                    self.logMessage.emit(str(payload))
                    self.buttonsChanged.emit()
        except Empty:
            pass
        if self._link_up and not self.client.connected:
            self._link_up = False
            self._stop_recording()
            self.statusChanged.emit("連線中斷", "bad")
            self.logMessage.emit("偵測到 TCP 連線中斷")
            self.buttonsChanged.emit()

    def _handle_msg(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "LOG":
            self.logMessage.emit(str(msg.get("message", "")))
        elif kind == "ERROR":
            self.logMessage.emit("ERROR: " + str(msg.get("error", msg)))
        elif kind == "STATUS":
            self.logMessage.emit(f"STATUS: {msg}")
        elif kind == "HELLO_ACK":
            self.logMessage.emit(f"RT hello ok  simulate={msg.get('simulate')}")

    def _on_packet(self, packet: DataPacket) -> None:
        self._enqueue(("data", packet))

    def _on_message(self, msg: dict) -> None:
        self._enqueue(("msg", msg))

    def _on_offline_log(self, text: str) -> None:
        self._enqueue(("log", text))

    def _enqueue(self, item: tuple) -> None:
        try:
            self.updates.put_nowait(item)
        except Full:
            try:
                self.updates.get_nowait()
            except Empty:
                pass
            try:
                self.updates.put_nowait(item)
            except Full:
                pass

    def _stop_recording(self) -> None:
        if self.recorder is None:
            return
        path = self.recorder.dir
        try:
            self.recorder.close()
        except Exception:
            pass
        self.recorder = None
        self.logMessage.emit(f"存檔結束  {path}")

    def _record_dir(self, raw: str) -> Path:
        path = Path(local_record_root(raw))
        path.mkdir(parents=True, exist_ok=True)
        return path


def _period_us(period_ms: float) -> int:
    return int(round(max(float(period_ms), 0.001) * 1000.0))


def _parse_channels(text: str) -> list[str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return parts or None
