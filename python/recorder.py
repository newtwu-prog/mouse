"""Session recorder aligned to LabVIEW TDMS 'pages', but as CSV.

LabVIEW produced four TDMS files; we write four CSVs with matching content:

  raw_data.csv          <-> *_data.tdms          (EEG + Movement per sample)
  group_send_data.csv   <-> *_Group_Send_Data    (epoch state / theta_delta / movement)
  group_setting.csv     <-> *_Group_Setting      (pre-run channel parameters)
  ttl_timepoint.csv     <-> *_TTL_timepoint      (sample indices where TTL fired)
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from protocol.messages import DataPacket, GroupData


def _ai_label(ai: int) -> str:
    return f"Ch{int(ai) + 1}"


def _dio_label(dio: int) -> str:
    return f"Ch{int(dio)}"


class SessionRecorder:
    def __init__(self, root: Path, session_name: str | None = None) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = session_name or f"session_{stamp}"
        self.dir = Path(root) / name
        self.dir.mkdir(parents=True, exist_ok=True)

        self.meta_path = self.dir / "meta.json"
        self.raw_path = self.dir / "raw_data.csv"
        self.send_path = self.dir / "group_send_data.csv"
        self.setting_path = self.dir / "group_setting.csv"
        self.ttl_path = self.dir / "ttl_timepoint.csv"
        self.changes_path = self.dir / "parameter_changes.csv"

        self._raw_fh = None
        self._raw_writer: csv.writer | None = None
        self._raw_header_groups: list[str] | None = None

        self._send_fh = open(self.send_path, "w", newline="", encoding="utf-8")
        self._send_writer = csv.writer(self._send_fh)
        # LabVIEW channels: {group}, {group}_Theta/delta, {group}_Movement
        self._send_writer.writerow(
            ["elapsed_s", "seq", "group", "state", "theta_delta", "movement"]
        )

        self._ttl_fh = open(self.ttl_path, "w", newline="", encoding="utf-8")
        self._ttl_writer = csv.writer(self._ttl_fh)
        self._ttl_writer.writerow(["sample_index", "group", "elapsed_s"])

        self._changes_fh = open(self.changes_path, "w", newline="", encoding="utf-8")
        self._changes_writer = csv.writer(self._changes_fh)
        self._changes_writer.writerow(
            ["wall_time", "elapsed_s", "group", "parameter", "old_value", "new_value", "source"]
        )
        self._start_monotonic = time.monotonic()

        self._sample_index = 0
        self._fs: float | None = None
        self._last_send: dict[str, tuple[Any, ...]] = {}
        self._prev_ttl: dict[str, bool] = {}
        self._closed = False

    @property
    def path(self) -> Path:
        """Alias used by some UI log lines."""
        return self.dir

    def write_meta(self, meta: dict) -> None:
        """Write LabVIEW-like group_setting.csv plus a full meta.json snapshot."""
        self.meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        groups = meta.get("groups") or []
        with open(self.setting_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "Group_Name",
                    "EEG_channel",
                    "Movement_channel",
                    "NREM_code",
                    "REM_code",
                    "WAKE_code",
                    "Theta_delta_Threshold",
                    "Movement_Threshold",
                    "TTL_Ch",
                    "Judge_state",
                    "threshold_v",
                    "threshold_mode",
                    "threshold_n",
                    "sample_period_us",
                    "epoch_sec",
                    "ttl_enabled",
                    "ttl_output_ms",
                    "ttl_refractory_ms",
                ]
            )
            for g in groups:
                w.writerow(
                    [
                        g.get("name", ""),
                        _ai_label(int(g.get("eeg_ai", 0))),
                        _ai_label(int(g.get("emg_ai", 1))),
                        "N",
                        "R",
                        "W",
                        g.get("theta_delta_threshold", ""),
                        g.get("movement_threshold", ""),
                        _dio_label(int(g.get("ttl_dio", 0))),
                        g.get("target_state", ""),
                        g.get("threshold_v", ""),
                        g.get("threshold_mode", ""),
                        g.get("threshold_n", ""),
                        meta.get("sample_period_us", ""),
                        meta.get("epoch_sec", ""),
                        meta.get("ttl_enabled", ""),
                        meta.get("ttl_output_ms", ""),
                        meta.get("ttl_refractory_ms", ""),
                    ]
                )

    def _ensure_raw(self, groups: list[GroupData]) -> None:
        names = [g.name for g in groups]
        if self._raw_writer is not None:
            return
        self._raw_header_groups = names
        self._raw_fh = open(self.raw_path, "w", newline="", encoding="utf-8")
        self._raw_writer = csv.writer(self._raw_fh)
        header = ["sample_index"]
        for name in names:
            header.append(f"EEG_{name}")
            header.append(f"Movement_{name}")
        self._raw_writer.writerow(header)

    def append_parameter_changes(
        self,
        old: dict[str, dict],
        new: dict[str, dict],
        source: str = "live_apply",
    ) -> None:
        """Record each changed live parameter with old/new values."""
        wall = time.strftime("%Y-%m-%d %H:%M:%S")
        elapsed = max(time.monotonic() - self._start_monotonic, 0.0)
        for group, new_values in new.items():
            old_values = old.get(group, {})
            for parameter, new_value in new_values.items():
                old_value = old_values.get(parameter, "")
                if old_value == new_value:
                    continue
                self._changes_writer.writerow(
                    [wall, f"{elapsed:.6f}", group, parameter, old_value, new_value, source]
                )
        self._changes_fh.flush()

    def append_packet(self, packet: DataPacket) -> None:
        if self._closed or not packet.groups:
            return
        if self._fs is None:
            self._fs = float(packet.fs)

        self._ensure_raw(packet.groups)
        assert self._raw_writer is not None

        n = len(packet.groups[0].eeg)
        for i in range(n):
            row: list[Any] = [self._sample_index + i]
            for g in packet.groups:
                eeg = float(g.eeg[i]) if i < len(g.eeg) else 0.0
                emg = float(g.emg[i]) if i < len(g.emg) else 0.0
                row.extend([f"{eeg:.8g}", f"{emg:.8g}"])
            self._raw_writer.writerow(row)

        # Epoch / condition summary (LabVIEW Group_Send_Data): one row when values change.
        for g in packet.groups:
            key = (g.state, round(float(g.theta_delta), 8), round(float(g.movement), 8))
            if self._last_send.get(g.name) == key:
                continue
            self._last_send[g.name] = key
            self._send_writer.writerow(
                [
                    f"{packet.elapsed_s:.6f}",
                    packet.seq,
                    g.name,
                    g.state,
                    f"{float(g.theta_delta):.8g}",
                    f"{float(g.movement):.8g}",
                ]
            )

        # TTL timepoints: rising edge (or high samples in ttl_trace).
        for g in packet.groups:
            if g.ttl_trace is not None and len(g.ttl_trace) == n:
                prev = self._prev_ttl.get(g.name, False)
                for i in range(n):
                    high = bool(float(g.ttl_trace[i]) >= 0.5)
                    if high and not prev:
                        t = packet.elapsed_s
                        if self._fs and self._fs > 0:
                            t = packet.elapsed_s - (n - 1 - i) / self._fs
                        self._ttl_writer.writerow(
                            [self._sample_index + i, g.name, f"{t:.6f}"]
                        )
                    prev = high
                self._prev_ttl[g.name] = prev
            else:
                prev = self._prev_ttl.get(g.name, False)
                if g.ttl and not prev:
                    self._ttl_writer.writerow(
                        [self._sample_index + n - 1, g.name, f"{packet.elapsed_s:.6f}"]
                    )
                self._prev_ttl[g.name] = bool(g.ttl)

        self._sample_index += n
        if self._raw_fh is not None:
            self._raw_fh.flush()
        self._send_fh.flush()
        self._ttl_fh.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fh in (self._raw_fh, self._send_fh, self._ttl_fh, self._changes_fh):
            if fh is None:
                continue
            try:
                fh.close()
            except Exception:
                pass
        self._raw_fh = None
        self._raw_writer = None
