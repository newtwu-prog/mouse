"""TDMS inspection + chunked replay for offline testing with real mouse data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_nptdms():
    try:
        from nptdms import TdmsFile  # noqa: F401
    except ImportError as exc:
        raise ImportError("請先安裝: py -3.11 -m pip install nptdms") from exc
    from nptdms import TdmsFile

    return TdmsFile


def inspect_tdms(path: Path) -> dict:
    TdmsFile = _require_nptdms()
    info: dict = {"path": str(path), "groups": []}
    with TdmsFile.open(str(path)) as tdms:
        for group in tdms.groups():
            ginfo = {"name": group.name, "channels": []}
            for ch in group.channels():
                props = {k: _jsonable(v) for k, v in ch.properties.items()}
                n = len(ch)
                ginfo["channels"].append(
                    {
                        "name": ch.name,
                        "length": n,
                        "dtype": str(getattr(ch, "dtype", "")),
                        "properties": props,
                    }
                )
            info["groups"].append(ginfo)
    return info


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _guess_wf_increment(props: dict) -> float | None:
    for key in (
        "wf_increment",
        "NI_ExpXIncrement",
        "wf_xincrement",
        "dt",
        "Sample Period",
    ):
        if key in props:
            try:
                return float(props[key])
            except (TypeError, ValueError):
                pass
    return None


class TdmsReplay:
    """Stream TDMS channels as (n, channels) frames like FPGA FIFO output.

    Maps the first N selected channels into AI0..AI{N-1}.
    Channel order can be given explicitly; otherwise uses first group channels.
    """

    def __init__(
        self,
        path: Path,
        channels: int = 16,
        channel_names: list[str] | None = None,
        group_name: str | None = None,
        fs: float | None = None,
        loop: bool = True,
        speed: float = 1.0,
    ) -> None:
        TdmsFile = _require_nptdms()
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.channels = int(channels)
        self.loop = bool(loop)
        self.speed = max(float(speed), 0.01)
        self._tdms = TdmsFile.open(str(self.path))
        self._ch_objs = self._select_channels(group_name, channel_names)
        if len(self._ch_objs) < 2:
            raise RuntimeError(
                "TDMS 至少需要 2 個通道（EEG/EMG）。請用 inspect 看通道名稱後指定。"
            )
        # Pad / trim to requested channel count.
        self._n_src = len(self._ch_objs)
        lengths = [len(ch) for ch in self._ch_objs]
        self.total = int(min(lengths))
        if self.total <= 0:
            raise RuntimeError("TDMS 通道長度為 0")
        props = dict(self._ch_objs[0].properties)
        dt = _guess_wf_increment(props)
        if fs is not None:
            self.fs = float(fs)
        elif dt and dt > 0:
            self.fs = 1.0 / dt
        else:
            self.fs = 200.0
        self._pos = 0
        self.channel_names = [ch.name for ch in self._ch_objs]

    def _select_channels(self, group_name: str | None, channel_names: list[str] | None):
        groups = list(self._tdms.groups())
        if not groups:
            raise RuntimeError("TDMS 沒有 group")
        if group_name:
            group = self._tdms[group_name]
        else:
            # Prefer group with the most channels.
            group = max(groups, key=lambda g: len(list(g.channels())))
        available = {ch.name: ch for ch in group.channels()}
        if channel_names:
            missing = [n for n in channel_names if n not in available]
            if missing:
                raise KeyError(f"TDMS 找不到通道: {missing}; 有: {list(available)}")
            return [available[n] for n in channel_names]
        # Heuristic order: names containing EEG/EMG first, else file order.
        names = list(available)
        prefer = []
        rest = []
        for n in names:
            u = n.upper()
            if "EEG" in u or "EMG" in u or "CH" in u or "AI" in u:
                prefer.append(n)
            else:
                rest.append(n)
        ordered = prefer + rest
        take = ordered[: max(self.channels, 2)]
        return [available[n] for n in take]

    def close(self) -> None:
        try:
            self._tdms.close()
        except Exception:
            pass

    def read_frames(self, n: int) -> np.ndarray | None:
        """Return shape (n, channels) or None at end when loop=False."""
        if self.total <= 0:
            return None
        if self._pos >= self.total:
            if not self.loop:
                return None
            self._pos = 0
        end = min(self._pos + n, self.total)
        count = end - self._pos
        frames = np.zeros((count, self.channels), dtype=float)
        for i, ch in enumerate(self._ch_objs):
            if i >= self.channels:
                break
            # nptdms supports slicing without loading whole file into one array ideally
            segment = ch[self._pos : end]
            frames[:, i] = np.asarray(segment, dtype=float)
        self._pos = end
        return frames

    def summary(self) -> str:
        hours = self.total / self.fs / 3600.0
        return (
            f"TDMS {self.path.name}  fs≈{self.fs:.3f} Hz  samples={self.total}  "
            f"(~{hours:.2f} h)  channels={self.channel_names}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inspect a mouse-signal TDMS file")
    p.add_argument("tdms", type=Path, help="path to .tdms")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    info = inspect_tdms(args.tdms)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print(f"file: {info['path']}")
        for g in info["groups"]:
            print(f"  group: {g['name']}")
            for ch in g["channels"]:
                print(f"    - {ch['name']}: n={ch['length']}  props={list(ch['properties'])[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
