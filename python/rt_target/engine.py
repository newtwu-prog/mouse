"""cRIO-side acquisition + judgment loop (local RIO0 or simulate)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from fpga_daq import DEFAULT_CHANNELS, FpgaDaq
from processing.groups import GroupSetting
from processing.runtime import GroupRuntime
from processing.state import StateScorer
from protocol.messages import DataPacket, GroupData
from timebase import RateFollow, ai_outside_frame


EmitFn = Callable[[DataPacket], None]
LogFn = Callable[[str], None]


@dataclass
class RtRunConfig:
    period_us: int = 5000
    channels: int = DEFAULT_CHANNELS
    epoch_sec: float = 12.0
    ttl_enabled: bool = True
    ttl_output_ms: float = 10.0
    ttl_refractory_ms: float = 0.0
    timeout_ms: int = 5000
    stream_samples: int = 40  # ~0.2 s at 200 Hz; keep FIFO reads under timeout
    tdms_path: str = ""
    tdms_speed: float = 1.0
    tdms_loop: bool = True
    tdms_channels: tuple[str, ...] = ()


@dataclass
class RtTargetEngine:
    bitfile: Path | None = None
    resource: str = "RIO0"
    simulate: bool = False
    on_data: EmitFn | None = None
    on_log: LogFn | None = None
    config: RtRunConfig = field(default_factory=RtRunConfig)
    groups: list[GroupSetting] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._daq: FpgaDaq | None = None
        self._tdms = None
        self._lock = threading.Lock()
        self._pending_groups: list[GroupSetting] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def replay_mode(self) -> bool:
        return bool(self.config.tdms_path)

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def start(self, groups: list[GroupSetting], config: RtRunConfig | None = None) -> None:
        if self.running:
            return
        self.groups = list(groups)
        if config is not None:
            self.config = config
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rt-target", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._daq is not None:
            try:
                self._daq.stop()
                self._daq.close()
            except Exception:
                pass
            self._daq = None
        if self._tdms is not None:
            try:
                self._tdms.close()
            except Exception:
                pass
            self._tdms = None

    def update_groups(self, groups: list[GroupSetting]) -> None:
        """Hot-swap group thresholds/modes while acquisition keeps running."""
        with self._lock:
            self.groups = list(groups)
            self._pending_groups = list(groups)

    def _consume_pending_groups(self, runtimes: dict[str, GroupRuntime]) -> None:
        with self._lock:
            pending = self._pending_groups
            self._pending_groups = None
        if not pending:
            return
        by_name = {g.name: g for g in pending}
        updated = []
        for name, rt in runtimes.items():
            if name in by_name:
                rt.group = by_name[name]
                updated.append(name)
        self.groups = list(pending)
        self._log(
            "live groups updated: "
            + (", ".join(updated) if updated else "(no matching runtimes)")
        )

    def _open_daq(self) -> None:
        if self.replay_mode or self.simulate:
            self._daq = None
            return
        self._daq = FpgaDaq(
            device=None,
            bitfile=self.bitfile,
            no_run=False,
            resource=self.resource,
        )
        self._log(f"FPGA opened  resource={self._daq.resource}  bitfile={self._daq.bitfile}")

    def _open_tdms(self, fs_hint: float) -> float:
        from tdms_replay import TdmsReplay

        names = list(self.config.tdms_channels) or None
        self._tdms = TdmsReplay(
            path=Path(self.config.tdms_path),
            channels=self.config.channels,
            channel_names=names,
            fs=fs_hint,
            loop=self.config.tdms_loop,
            speed=self.config.tdms_speed,
        )
        self._log(self._tdms.summary())
        return float(self._tdms.fs)

    def _loop(self) -> None:
        cfg = self.config
        groups = self.groups
        fs = 1_000_000.0 / cfg.period_us
        chunk = max(int(cfg.stream_samples), 1)
        samples_per_read = max(cfg.channels * chunk, cfg.channels)
        unfit = ai_outside_frame(groups, cfg.channels)
        if unfit:
            self._log(unfit)
            return

        try:
            if self.replay_mode:
                fs = self._open_tdms(fs)
                # Keep period consistent with TDMS sample rate for timing/sleep.
                cfg.period_us = int(round(1_000_000.0 / fs))
                self._log(
                    f"TDMS replay  fs={fs:.3f} Hz  speed={cfg.tdms_speed:g}x  "
                    f"loop={cfg.tdms_loop}"
                )
            else:
                self._open_daq()
                if self.simulate:
                    self._log("simulate mode: synthetic sine (no FPGA)")
                if self._daq is not None:
                    with self._lock:
                        depth = self._daq.start(period_us=cfg.period_us)
                    self._log(
                        f"acquisition started  required_fs={fs:.1f} Hz"
                        f" (Count(uSec)={cfg.period_us} µs,"
                        f" FIFO {cfg.channels} elements/frame)  "
                        f"{self._daq.period_detail}  FIFO depth={depth}. "
                        f"Wall-clock rate must be near {fs:.1f} Hz."
                    )
        except Exception as exc:
            self._log(f"start failed: {type(exc).__name__}: {exc}")
            return

        epoch_n = max(int(round(fs * cfg.epoch_sec)), 8)
        scorer = StateScorer(fs)
        runtimes = {
            g.name: GroupRuntime(
                g,
                fs,
                ttl_output_ms=cfg.ttl_output_ms,
                ttl_refractory_ms=cfg.ttl_refractory_ms,
            )
            for g in groups
        }
        last_dio = {g.ttl_dio: False for g in groups}
        follow = (
            RateFollow(
                fs,
                cfg.epoch_sec,
                epoch_n,
                period_us=cfg.period_us,
                frame_width=cfg.channels,
            )
            if self._daq is not None
            else None
        )
        pending = np.zeros((0, cfg.channels))
        seq = 0
        t0 = time.perf_counter()
        sim_phase = 0.0
        use_hw_ttl = (not self.simulate) and (not self.replay_mode) and self._daq is not None

        try:
            fifo_timeouts = 0
            while not self._stop.is_set():
                self._consume_pending_groups(runtimes)
                packet_fs = fs
                if self.replay_mode:
                    assert self._tdms is not None
                    frames = self._tdms.read_frames(chunk)
                    if frames is None:
                        self._log("TDMS replay finished")
                        break
                    remaining = 0
                    time.sleep((len(frames) / fs) / cfg.tdms_speed)
                elif self.simulate:
                    frames = self._sim_frames(fs, chunk, cfg.channels, sim_phase)
                    sim_phase += chunk / fs
                    remaining = 0
                    time.sleep(chunk / fs)
                else:
                    assert self._daq is not None
                    try:
                        with self._lock:
                            frames, remaining = self._daq.read_frames(
                                samples_per_read,
                                channels=cfg.channels,
                                timeout_ms=cfg.timeout_ms,
                            )
                    except Exception as exc:
                        # FifoTimeout often means no samples yet; retry instead of
                        # tearing down the whole acquisition (which looks like a PC "disconnect").
                        msg = str(exc)
                        if "FifoTimeout" in msg or "-50400" in msg or "Timeout" in msg:
                            fifo_timeouts += 1
                            if fifo_timeouts == 1 or fifo_timeouts % 10 == 0:
                                self._log(f"FIFO timeout (#{fifo_timeouts}), retrying: {exc}")
                            continue
                        self._log(f"FIFO read failed: {exc}")
                        break
                    fifo_timeouts = 0
                    if frames.size == 0:
                        continue
                    assert follow is not None
                    packet_fs = follow.observe(
                        len(frames), time.perf_counter(), scorer, runtimes, self._log
                    )
                    epoch_n = follow.epoch_n

                pending = np.vstack([pending, frames]) if pending.size else frames

                block_cond1: dict[str, bool] = {}
                block_ttl: dict[str, bool] = {}
                block_ttl_trace: dict[str, np.ndarray] = {}
                for g in groups:
                    rt = runtimes[g.name]
                    outs = rt.on_samples(frames[:, g.eeg_ai], cfg.ttl_enabled)
                    block_cond1[g.name] = bool(rt.score.get("over_threshold"))
                    block_ttl[g.name] = bool(outs[-1]) if outs else False
                    block_ttl_trace[g.name] = np.asarray(outs, dtype=np.float32)
                    if use_hw_ttl:
                        for desired in outs:
                            if desired != last_dio[g.ttl_dio]:
                                with self._lock:
                                    self._daq.write_dio(g.ttl_dio, desired)
                                last_dio[g.ttl_dio] = desired

                while len(pending) >= epoch_n:
                    epoch = pending[:epoch_n]
                    pending = pending[epoch_n:]
                    for g in groups:
                        runtimes[g.name].on_epoch(epoch[:, g.eeg_ai], epoch[:, g.emg_ai], scorer)

                n = len(frames)
                take = min(chunk, n)
                sl = slice(n - take, n)
                elapsed = time.perf_counter() - t0
                gdatas: list[GroupData] = []
                for g in groups:
                    rt = runtimes[g.name]
                    score = rt.score
                    trace = block_ttl_trace.get(g.name)
                    if trace is None or len(trace) != n:
                        trace = np.zeros(n, dtype=np.float32)
                    gdatas.append(
                        GroupData(
                            name=g.name,
                            eeg=frames[sl, g.eeg_ai].astype(np.float32, copy=False),
                            emg=frames[sl, g.emg_ai].astype(np.float32, copy=False),
                            movement=float(score.get("movement", score.get("emg_rms", 0.0))),
                            delta=float(score.get("delta", 0.0)),
                            theta=float(score.get("theta", 0.0)),
                            theta_delta=float(score.get("theta_delta", 0.0)),
                            state=str(score.get("state", "—")),
                            cond1=block_cond1.get(g.name, False),
                            cond2=bool(score.get("state_match", False)),
                            ttl=block_ttl.get(g.name, False),
                            ttl_trace=trace[sl],
                        )
                    )
                packet = DataPacket(elapsed_s=elapsed, seq=seq, fs=packet_fs, groups=gdatas)
                seq += 1
                if self.on_data:
                    self.on_data(packet)
        finally:
            if use_hw_ttl and self._daq is not None:
                try:
                    if cfg.ttl_enabled:
                        with self._lock:
                            for dio in {g.ttl_dio for g in groups}:
                                self._daq.write_dio(dio, False)
                    with self._lock:
                        self._daq.stop()
                except Exception:
                    pass
            if self._tdms is not None:
                try:
                    self._tdms.close()
                except Exception:
                    pass
                self._tdms = None
            self._log("acquisition stopped")

    def _sim_frames(self, fs: float, n: int, channels: int, t0: float) -> np.ndarray:
        t = t0 + np.arange(n) / fs
        frames = np.zeros((n, channels), dtype=float)
        frames[:, 0] = 0.05 * np.sin(2 * np.pi * 2.0 * t) + 0.03 * np.sin(2 * np.pi * 6.0 * t)
        frames[:, 1] = 0.002 * np.random.randn(n)
        if int(t0) % 20 < 2:
            frames[:, 1] += 0.05
        return frames
