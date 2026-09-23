"""Offline TDMS → same judgment pipeline as RT (no FPGA / no TCP)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from processing.groups import GroupSetting
from processing.runtime import GroupRuntime
from processing.state import StateScorer
from protocol.messages import DataPacket, GroupData
from tdms_replay import TdmsReplay


EmitFn = Callable[[DataPacket], None]
LogFn = Callable[[str], None]


class OfflineTdmsRunner:
    def __init__(
        self,
        on_data: EmitFn | None = None,
        on_log: LogFn | None = None,
    ) -> None:
        self.on_data = on_data
        self.on_log = on_log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending_groups: list[GroupSetting] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def start(
        self,
        tdms_path: Path,
        groups: list[GroupSetting],
        *,
        ttl_enabled: bool = True,
        ttl_output_ms: float = 10.0,
        ttl_refractory_ms: float = 0.0,
        epoch_sec: float = 12.0,
        speed: float = 10.0,
        loop: bool = True,
        channel_names: list[str] | None = None,
        chunk: int = 200,
        n_ai: int = 16,
    ) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            kwargs=dict(
                tdms_path=Path(tdms_path),
                groups=list(groups),
                ttl_enabled=ttl_enabled,
                ttl_output_ms=ttl_output_ms,
                ttl_refractory_ms=ttl_refractory_ms,
                epoch_sec=epoch_sec,
                speed=speed,
                loop=loop,
                channel_names=channel_names,
                chunk=chunk,
                n_ai=n_ai,
            ),
            name="offline-tdms",
            daemon=True,
        )
        self._thread.start()

    def update_groups(self, groups: list[GroupSetting]) -> None:
        """Update thresholds/modes for a running offline replay."""
        with self._lock:
            self._pending_groups = list(groups)

    def _take_pending_groups(self) -> list[GroupSetting] | None:
        with self._lock:
            pending = self._pending_groups
            self._pending_groups = None
        return pending

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(
        self,
        tdms_path: Path,
        groups: list[GroupSetting],
        ttl_enabled: bool,
        ttl_output_ms: float,
        ttl_refractory_ms: float,
        epoch_sec: float,
        speed: float,
        loop: bool,
        channel_names: list[str] | None,
        chunk: int,
        n_ai: int,
    ) -> None:
        replay = None
        try:
            replay = TdmsReplay(
                path=tdms_path,
                channels=n_ai,
                channel_names=channel_names,
                loop=loop,
                speed=speed,
            )
            fs = float(replay.fs)
            self._log(replay.summary())
            self._log(
                f"離線測試開始  speed={speed:g}x  epoch={epoch_sec:g}s  "
                f"TTL out={ttl_output_ms:g}ms refr={ttl_refractory_ms:g}ms"
            )
            scorer = StateScorer(fs)
            runtimes = {
                g.name: GroupRuntime(
                    g,
                    fs,
                    ttl_output_ms=ttl_output_ms,
                    ttl_refractory_ms=ttl_refractory_ms,
                )
                for g in groups
            }
            pending = np.zeros((0, n_ai))
            epoch_n = max(int(round(fs * epoch_sec)), 8)
            chunk = max(int(chunk), 1)
            seq = 0
            t0 = time.perf_counter()

            while not self._stop.is_set():
                pending_groups = self._take_pending_groups()
                if pending_groups:
                    by_name = {g.name: g for g in pending_groups}
                    for name, rt in runtimes.items():
                        if name in by_name:
                            rt.group = by_name[name]
                    groups = list(pending_groups)
                    self._log("離線即時參數已更新")
                frames = replay.read_frames(chunk)
                if frames is None:
                    self._log("TDMS 重播結束")
                    break
                # Pad to n_ai if TDMS has fewer mapped channels
                if frames.shape[1] < n_ai:
                    pad = np.zeros((frames.shape[0], n_ai - frames.shape[1]))
                    frames = np.hstack([frames, pad])

                pending = np.vstack([pending, frames]) if pending.size else frames
                block_cond1: dict[str, bool] = {}
                block_ttl: dict[str, bool] = {}
                block_trace: dict[str, np.ndarray] = {}
                for g in groups:
                    rt = runtimes[g.name]
                    outs = rt.on_samples(frames[:, g.eeg_ai], ttl_enabled)
                    block_cond1[g.name] = bool(rt.score.get("over_threshold"))
                    block_ttl[g.name] = bool(outs[-1]) if outs else False
                    block_trace[g.name] = np.asarray(outs, dtype=np.float32)

                while len(pending) >= epoch_n:
                    epoch = pending[:epoch_n]
                    pending = pending[epoch_n:]
                    for g in groups:
                        runtimes[g.name].on_epoch(epoch[:, g.eeg_ai], epoch[:, g.emg_ai], scorer)

                n = len(frames)
                gdatas: list[GroupData] = []
                for g in groups:
                    rt = runtimes[g.name]
                    score = rt.score
                    trace = block_trace.get(g.name, np.zeros(n, dtype=np.float32))
                    gdatas.append(
                        GroupData(
                            name=g.name,
                            eeg=frames[:, g.eeg_ai].astype(np.float32, copy=False),
                            emg=frames[:, g.emg_ai].astype(np.float32, copy=False),
                            movement=float(score.get("movement", score.get("emg_rms", 0.0))),
                            delta=float(score.get("delta", 0.0)),
                            theta=float(score.get("theta", 0.0)),
                            theta_delta=float(score.get("theta_delta", 0.0)),
                            state=str(score.get("state", "—")),
                            cond1=block_cond1.get(g.name, False),
                            cond2=bool(score.get("state_match", False)),
                            ttl=block_ttl.get(g.name, False),
                            ttl_trace=trace,
                        )
                    )
                packet = DataPacket(
                    elapsed_s=time.perf_counter() - t0,
                    seq=seq,
                    fs=fs,
                    groups=gdatas,
                )
                seq += 1
                if self.on_data:
                    self.on_data(packet)
                time.sleep((n / fs) / max(speed, 0.01))
        except Exception as exc:
            self._log(f"離線測試失敗: {type(exc).__name__}: {exc}")
        finally:
            if replay is not None:
                try:
                    replay.close()
                except Exception:
                    pass
            self._log("離線測試已停止")
