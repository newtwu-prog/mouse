"""Background acquisition loop used by the GUI."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Full, Queue

import numpy as np

from fpga_daq import DEFAULT_CHANNELS, FpgaDaq
from processing.groups import GroupSetting
from processing.runtime import GroupRuntime
from processing.state import StateScorer
from timebase import RateFollow


@dataclass
class RtConfig:
    period_us: int = 5000
    channels: int = DEFAULT_CHANNELS
    epoch_sec: float = 12.0
    ttl_enabled: bool = False
    ttl_pulse_ms: float = 10.0
    ttl_output_ms: float = 10.0
    ttl_refractory_ms: float = 0.0
    timeout_ms: int = 2000
    wave_sec: float = 2.0
    plot_group: str = ""


@dataclass
class RtUpdate:
    kind: str
    elapsed: float = 0.0
    remaining: int = 0
    snapshot: dict = field(default_factory=dict)
    eeg: np.ndarray | None = None
    emg: np.ndarray | None = None
    t: np.ndarray | None = None
    scores: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    message: str = ""
    error: str = ""


class RtEngine:
    def __init__(self, updates: Queue[RtUpdate]) -> None:
        self.updates = updates
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.daq: FpgaDaq | None = None
        self.config = RtConfig()
        self.groups: list[GroupSetting] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def fpga_open(self) -> bool:
        return self.daq is not None

    def open_fpga(self, device, bitfile: Path) -> str:
        self.close_fpga()
        self.daq = FpgaDaq(device, bitfile=bitfile, no_run=False)
        snap = self.daq.snapshot()
        return f"FPGA opened  {self.daq.resource}  snapshot={snap}"

    def close_fpga(self) -> None:
        self.stop()
        if self.daq is not None:
            try:
                self.daq.close()
            except Exception:
                pass
            self.daq = None

    def pulse_dio(self, channel: int, seconds: float = 0.05) -> None:
        if self.daq is None:
            raise RuntimeError("FPGA is not open")
        with self._lock:
            self.daq.write_dio(channel, True)
            time.sleep(seconds)
            self.daq.write_dio(channel, False)

    def start(self, config: RtConfig, groups: list[GroupSetting]) -> None:
        if self.daq is None:
            raise RuntimeError("Open the FPGA first")
        if self.running:
            return
        self.config = config
        self.groups = list(groups)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rt-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self.daq is not None:
            try:
                self.daq.stop()
            except Exception:
                pass

    def _emit(self, update: RtUpdate) -> None:
        try:
            self.updates.put_nowait(update)
        except Full:
            try:
                self.updates.get_nowait()
            except Exception:
                pass
            try:
                self.updates.put_nowait(update)
            except Full:
                pass

    def _loop(self) -> None:
        assert self.daq is not None
        cfg = self.config
        groups = self.groups
        fs = 1_000_000.0 / cfg.period_us
        epoch_n = max(int(round(fs * cfg.epoch_sec)), 8)
        wave_n = max(int(round(fs * cfg.wave_sec)), 8)
        samples_per_read = max(cfg.channels * 20, cfg.channels)
        plot_name = cfg.plot_group or (groups[0].name if groups else "")

        try:
            with self._lock:
                depth = self.daq.start(period_us=cfg.period_us)
            self._emit(
                RtUpdate(
                    kind="status",
                    message=(
                        f"acquisition started  required_fs={fs:.1f} Hz"
                        f" (Count(uSec)={cfg.period_us} µs)  "
                        f"{self.daq.period_detail}  "
                        f"state window={cfg.epoch_sec:g}s ({epoch_n} samples)  "
                        f"FIFO depth={depth}. "
                        f"Wall-clock rate must be near {fs:.1f} Hz."
                    ),
                    snapshot=self.daq.snapshot(),
                )
            )
        except Exception as exc:
            name = type(exc).__name__
            text = f"{name}: {exc}"
            hint = ""
            if "FifoReserved" in text or "FIFO_AIO" in text:
                hint = (
                    "  這通常是先前 LabVIEW 留下的占用。"
                    " 請停止 RT_main.vi / startup.rtexe（NI MAX 取消開機自動執行），"
                    " 必要時重開 cRIO，然後關閉並重新開啟 FPGA。"
                )
            elif "RpcConnection" in text or "-63040" in text or "-63041" in text:
                hint = (
                    "  PC 與 cRIO 的 FPGA RPC 連線斷了。"
                    " 請確認 LabVIEW RT 已停、能 ping 通 192.168.0.110，"
                    " 必要時重開 cRIO，再按「關閉 FPGA」後重新開啟再開始。"
                )
            self._emit(
                RtUpdate(
                    kind="error",
                    error=f"start failed: {text}{hint}",
                )
            )
            try:
                self.daq.stop()
            except Exception:
                pass
            return

        scorer = StateScorer(fs)
        runtimes = {
            g.name: GroupRuntime(
                g,
                fs,
                ttl_output_ms=getattr(cfg, "ttl_output_ms", cfg.ttl_pulse_ms),
                ttl_refractory_ms=getattr(cfg, "ttl_refractory_ms", 0.0),
            )
            for g in groups
        }
        last_dio = {g.ttl_dio: False for g in groups}
        follow = RateFollow(fs, cfg.epoch_sec, epoch_n)
        pending = np.zeros((0, cfg.channels))
        wave = np.zeros((0, cfg.channels))
        t0 = time.perf_counter()

        try:
            while not self._stop.is_set():
                try:
                    with self._lock:
                        frames, remaining = self.daq.read_frames(
                            samples_per_read,
                            channels=cfg.channels,
                            timeout_ms=cfg.timeout_ms,
                        )
                except Exception as exc:
                    self._emit(RtUpdate(kind="error", error=f"FIFO read failed: {exc}"))
                    break
                if frames.size == 0:
                    continue
                display_fs = follow.observe(
                    len(frames),
                    time.perf_counter(),
                    scorer,
                    runtimes,
                    lambda msg: self._emit(RtUpdate(kind="status", message=msg)),
                )
                epoch_n = follow.epoch_n
                wave_n = max(int(round(display_fs * cfg.wave_sec)), 8)
                # No lowpass: condition 2 bandpasses raw 12 s EEG; condition 1 uses raw samples.
                pending = np.vstack([pending, frames]) if pending.size else frames
                wave = np.vstack([wave, frames]) if wave.size else frames
                if len(wave) > wave_n:
                    wave = wave[-wave_n:]

                for g in groups:
                    rt = runtimes[g.name]
                    for desired in rt.on_samples(frames[:, g.eeg_ai], cfg.ttl_enabled):
                        if desired != last_dio[g.ttl_dio]:
                            with self._lock:
                                self.daq.write_dio(g.ttl_dio, desired)
                            last_dio[g.ttl_dio] = desired

                while len(pending) >= epoch_n:
                    epoch = pending[:epoch_n]
                    pending = pending[epoch_n:]
                    for g in groups:
                        rt = runtimes[g.name]
                        rt.on_epoch(epoch[:, g.eeg_ai], epoch[:, g.emg_ai], scorer)

                g = next((x for x in groups if x.name == plot_name), groups[0] if groups else None)
                eeg = wave[:, g.eeg_ai] if g is not None and wave.size else None
                emg = wave[:, g.emg_ai] if g is not None and wave.size else None
                t = (np.arange(len(eeg)) / display_fs) if eeg is not None else None
                self._emit(
                    RtUpdate(
                        kind="data",
                        elapsed=time.perf_counter() - t0,
                        remaining=int(remaining),
                        snapshot=self.daq.snapshot(),
                        eeg=None if eeg is None else eeg.copy(),
                        emg=None if emg is None else emg.copy(),
                        t=t,
                        scores={name: dict(rt.score) for name, rt in runtimes.items()},
                        history={name: list(rt.history) for name, rt in runtimes.items()},
                    )
                )
        finally:
            try:
                if cfg.ttl_enabled:
                    with self._lock:
                        for dio in {g.ttl_dio for g in groups}:
                            self.daq.write_dio(dio, False)
                with self._lock:
                    self.daq.stop()
            except Exception:
                pass
            self._emit(RtUpdate(kind="status", message="acquisition stopped"))
