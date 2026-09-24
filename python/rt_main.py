"""Replace LabVIEW RT_main.vi with a PC-side Python Real-Time loop.

FPGA_DAQ.vi stays on the FPGA. This program takes over the RT job:

  FPGA FIFO_AIO (raw)
    condition 1: each sample vs threshold (above or below)
    condition 2: 12 s bandpass δ/θ + movement → WAKE/NREM/REM vs PC target
    TTL = both true
                -> console / live plot

    py -3.11 python/rt_main.py
    py -3.11 python/rt_main.py --no-plot --seconds 12
    py -3.11 python/rt_main.py --ttl

Stop LabVIEW RT_main.vi / startup.rtexe first. The FPGA session is exclusive.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEFAULT_DEVICE_KEY, DEVICES
from fpga_daq import (
    DEFAULT_CHANNELS,
    FpgaDaq,
    default_bitfile,
    resolve_device,
    rio_resource,
)
from processing.groups import load_groups
from processing.runtime import GroupRuntime
from processing.state import StateScorer
from timebase import RateFollow, ai_outside_frame

DEFAULT_SETTINGS = Path(__file__).resolve().parent / "settings" / "default_groups.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python replacement for LabVIEW RT_main.vi"
    )
    parser.add_argument("--device", choices=sorted(DEVICES), default=DEFAULT_DEVICE_KEY)
    parser.add_argument("--ip", help="override cRIO IP")
    parser.add_argument("--bitfile", default=str(default_bitfile()))
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    parser.add_argument("--period-us", type=int, default=0, help="0 = use JSON sample_period_us")
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    parser.add_argument("--seconds", type=float, default=0.0, help="0 = run until Ctrl+C")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--ttl",
        action="store_true",
        help="write FPGA DIO when threshold AND target state both match",
    )
    parser.add_argument("--timeout-ms", type=int, default=2000)
    return parser.parse_args()


def _try_plot():
    try:
        import matplotlib.pyplot as plt

        plt.ion()
        fig, axes = plt.subplots(3, 1, figsize=(10, 7))
        fig.canvas.manager.set_window_title("Python RT (replaces RT_main.vi)")
        return plt, fig, axes
    except Exception as exc:
        print(f"live plot disabled: {exc}")
        return None, None, None


def _update_plot(plt, axes, eeg, emg, fs, states: list[int], title: str) -> None:
    t = np.arange(len(eeg)) / fs
    axes[0].cla()
    axes[1].cla()
    axes[2].cla()
    axes[0].plot(t, eeg, color="C0", linewidth=0.8)
    axes[0].set_ylabel("EEG (V)")
    axes[0].set_title(title)
    axes[1].plot(t, emg, color="C1", linewidth=0.8)
    axes[1].set_ylabel("EMG (V)")
    if states:
        axes[2].plot(states, drawstyle="steps-post", color="C2")
        axes[2].set_yticks([0, 1, 2])
        axes[2].set_yticklabels(["WAKE", "NREM", "REM"])
    axes[2].set_ylabel("state")
    axes[2].set_xlabel("12 s epoch")
    plt.tight_layout()
    plt.pause(0.001)


def main() -> int:
    args = parse_args()
    settings_path = Path(args.settings)
    if not settings_path.is_file():
        print(f"settings not found: {settings_path}")
        return 1

    groups, raw = load_groups(settings_path)
    period_us = args.period_us or int(raw.get("sample_period_us", 5000))
    epoch_sec = float(raw.get("epoch_sec", 12.0))
    ttl_pulse_ms = float(raw.get("ttl_output_ms", raw.get("ttl_pulse_ms", 10.0)))
    ttl_refractory_ms = float(raw.get("ttl_refractory_ms", 0.0))
    fs = 1_000_000.0 / period_us
    epoch_n = max(int(round(fs * epoch_sec)), 8)

    device = resolve_device(args.device, args.ip)
    print("=" * 64)
    print(" Python RT  (replaces LabVIEW RT_main.vi)")
    print("=" * 64)
    print(f"  device   : {device.alias} ({device.model})")
    print(f"  resource : {rio_resource(device)}")
    print(f"  bitfile  : {args.bitfile}")
    print(f"  fs       : {fs:.1f} Hz   period={period_us} us")
    print(f"  state    : every {epoch_sec:g} s ({epoch_n} samples), raw + bandpass δ/θ")
    print(f"  TTL      : cond1=threshold  AND  cond2=target state")
    print(f"  groups   : {len(groups)}")
    for g in groups:
        print(
            f"    {g.name}: EEG=AI{g.eeg_ai} EMG=AI{g.emg_ai} "
            f"move={g.movement_threshold:g} td={g.theta_delta_threshold:g} "
            f"target={g.target_state} mode={g.threshold_mode} thr={g.threshold_v:g} V TTL=DIO{g.ttl_dio}"
        )
    print("  Ctrl+C to stop")
    print()

    try:
        daq = FpgaDaq(device, bitfile=Path(args.bitfile), no_run=False)
    except Exception as exc:
        print(f"FPGA open failed: {type(exc).__name__}: {exc}")
        print("Stop LabVIEW RT_main.vi / startup.rtexe first.")
        return 2

    scorer = StateScorer(fs)
    runtimes = {
        g.name: GroupRuntime(
            g, fs, ttl_output_ms=ttl_pulse_ms, ttl_refractory_ms=ttl_refractory_ms
        )
        for g in groups
    }
    last_dio = {g.ttl_dio: False for g in groups}

    plt = fig = axes = None
    if not args.no_plot:
        plt, fig, axes = _try_plot()

    unfit = ai_outside_frame(groups, args.channels)
    if unfit:
        print(unfit)
        return 1
    samples_per_read = max(args.channels * 20, args.channels)
    pending = np.zeros((0, args.channels))
    t0 = time.perf_counter()
    deadline = t0 + args.seconds if args.seconds > 0 else None
    last_print = 0.0

    with daq:
        try:
            depth = daq.start(period_us=period_us)
            print(f"  FIFO depth : {depth}")
            print(f"  period     : {daq.period_detail}")
            print(f"  snapshot   : {daq.snapshot()}")
            print()
            follow = RateFollow(
                fs, epoch_sec, epoch_n, period_us=period_us, frame_width=args.channels
            )
            while True:
                if deadline is not None and time.perf_counter() >= deadline:
                    break
                frames, remaining = daq.read_frames(
                    samples_per_read, channels=args.channels, timeout_ms=args.timeout_ms
                )
                if frames.size == 0:
                    continue
                display_fs = follow.observe(
                    len(frames), time.perf_counter(), scorer, runtimes, print
                )
                epoch_n = follow.epoch_n
                pending = np.vstack([pending, frames]) if pending.size else frames

                for g in groups:
                    rt = runtimes[g.name]
                    for desired in rt.on_samples(frames[:, g.eeg_ai], args.ttl):
                        if desired != last_dio[g.ttl_dio]:
                            daq.write_dio(g.ttl_dio, desired)
                            last_dio[g.ttl_dio] = desired

                judged = False
                while len(pending) >= epoch_n:
                    epoch = pending[:epoch_n]
                    pending = pending[epoch_n:]
                    judged = True
                    for g in groups:
                        runtimes[g.name].on_epoch(epoch[:, g.eeg_ai], epoch[:, g.emg_ai], scorer)

                now = time.perf_counter()
                if judged or now - last_print >= 1.0:
                    last_print = now
                    parts = []
                    for g in groups:
                        s = runtimes[g.name].score
                        parts.append(
                            f"{g.name}:{s['state']}"
                            f" match={'T' if s['state_match'] else 'F'}"
                            f" thr={'T' if s['over_threshold'] else 'F'}"
                            f" ttl={'T' if s['ttl'] else 'F'}"
                            f" td={s['theta_delta']:.2f}"
                        )
                    elapsed = now - t0
                    print(
                        f"  t={elapsed:7.2f}s  fs={display_fs:.2f}  fifo={remaining:5d}  "
                        + "  ".join(parts)
                    )
                    if plt is not None and groups and judged:
                        g0 = groups[0]
                        _update_plot(
                            plt,
                            axes,
                            epoch[:, g0.eeg_ai],
                            epoch[:, g0.emg_ai],
                            display_fs,
                            runtimes[g0.name].history,
                            f"{g0.name}  {runtimes[g0.name].score['state']}",
                        )
        except KeyboardInterrupt:
            print("\n stopped by user")
        finally:
            if args.ttl:
                for dio in {g.ttl_dio for g in groups}:
                    try:
                        daq.write_dio(dio, False)
                    except Exception:
                        pass
            daq.stop()

    print(" RESULT: Python RT loop finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
