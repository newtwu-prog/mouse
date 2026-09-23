"""Rolling display buffers. Energy traces are for the PC chart only.

Judgment (Cond1∧Cond2 / TTL) stays on the cRIO. Incoming packets already carry
state, cond flags, and TTL; BandEnergy here only builds the delta/theta traces
the middle chart draws, matching pc_ui.py.
"""

from __future__ import annotations

import numpy as np

from processing.energy import BandEnergy
from protocol.messages import DataPacket


class WaveStore:
    def __init__(self) -> None:
        self.fs = 200.0
        self.span_sec = 1.0
        self.waves: dict[str, dict[str, np.ndarray]] = {}
        self._energy_d: dict[str, BandEnergy] = {}
        self._energy_t: dict[str, BandEnergy] = {}

    @property
    def wave_n(self) -> int:
        return max(int(self.fs * max(self.span_sec, 0.2)), 40)

    def set_span(self, seconds: float) -> None:
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = 1.0
        self.span_sec = max(sec, 0.2)
        self._trim()

    def reset(self, names: list[str], fs: float | None = None) -> None:
        if fs is not None and fs > 0:
            self.fs = float(fs)
        self._energy_d.clear()
        self._energy_t.clear()
        self.waves.clear()
        for name in names:
            self._ensure(name)

    def append_packet(self, packet: DataPacket) -> None:
        if packet.fs and packet.fs > 0:
            new_fs = float(packet.fs)
            # Band edges were built at the previous rate. Rebuild when the RT
            # stamp switches from nominal 1e6/period_us to the measured rate.
            if self._energy_d and abs(new_fs - self.fs) / max(self.fs, 1e-9) > 0.01:
                self._energy_d.clear()
                self._energy_t.clear()
            self.fs = new_fs
        for group in packet.groups:
            self._ensure(group.name)
            delta = self._energy_d[group.name].process(group.eeg)
            theta = self._energy_t[group.name].process(group.eeg)
            if group.ttl_trace is not None and len(group.ttl_trace) == len(group.eeg):
                ttl = np.asarray(group.ttl_trace, dtype=float)
            else:
                ttl = np.full(len(group.eeg), 1.0 if group.ttl else 0.0)
            waves = self.waves[group.name]
            limit = self.wave_n
            waves["eeg"] = np.concatenate([waves["eeg"], np.asarray(group.eeg, dtype=float)])[-limit:]
            waves["emg"] = np.concatenate([waves["emg"], np.asarray(group.emg, dtype=float)])[-limit:]
            waves["d"] = np.concatenate([waves["d"], delta])[-limit:]
            waves["t"] = np.concatenate([waves["t"], theta])[-limit:]
            waves["ttl"] = np.concatenate([waves["ttl"], ttl])[-limit:]

    def _ensure(self, name: str) -> None:
        if name not in self._energy_d:
            rate = max(float(self.fs), 1.0)
            self._energy_d[name] = BandEnergy(rate, 0.5, 4.0)
            self._energy_t[name] = BandEnergy(rate, 4.0, 8.0)
        if name not in self.waves:
            self.waves[name] = {
                "eeg": np.zeros(0),
                "emg": np.zeros(0),
                "d": np.zeros(0),
                "t": np.zeros(0),
                "ttl": np.zeros(0),
            }

    def _trim(self) -> None:
        limit = self.wave_n
        for waves in self.waves.values():
            for key, values in list(waves.items()):
                if len(values) > limit:
                    waves[key] = values[-limit:]
