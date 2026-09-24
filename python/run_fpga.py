"""Download the LabVIEW FPGA bitfile to cRIO and stream analog samples.

This is the NI-supported way to "run" a .lvbitx from Python 3.11:

  LabVIEW FPGA compiler  ->  .lvbitx bitstream (already built)
  Python nifpga          ->  download bitstream to FPGA, then read FIFO_AIO

The bitstream cannot be executed as Python. It only runs on the FPGA fabric.

    py -3.11 python/run_fpga.py --inspect
    py -3.11 python/run_fpga.py --seconds 2

If LabVIEW RT_main.vi / startup.rtexe already owns the FPGA, stop that
application first (FPGA session is exclusive).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEFAULT_DEVICE_KEY, DEVICES
from fpga_daq import (
    DEFAULT_CHANNELS,
    FpgaDaq,
    default_bitfile,
    inspect_bitfile,
    resolve_device,
    rio_resource,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download FPGA_DAQ.vi bitfile and read FIFO_AIO from Python."
    )
    parser.add_argument("--device", choices=sorted(DEVICES), default=DEFAULT_DEVICE_KEY)
    parser.add_argument("--ip", help="override RT IP address")
    parser.add_argument("--bitfile", default=str(default_bitfile()))
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="only parse the bitfile (no hardware session)",
    )
    parser.add_argument("--period-us", type=int, default=5000, help="FPGA loop period in microseconds (5000 = 200 Hz)")
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    parser.add_argument("--seconds", type=float, default=1.0, help="how long to stream")
    parser.add_argument("--timeout-ms", type=int, default=2000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bitfile = Path(args.bitfile)
    if not bitfile.is_file():
        print(f"bitfile not found: {bitfile}")
        return 1

    if args.inspect:
        inspect_bitfile(bitfile)
        return 0

    device = resolve_device(args.device, args.ip)
    resource = rio_resource(device)
    print("=" * 64)
    print(" FPGA bitfile download + FIFO_AIO stream")
    print("=" * 64)
    print(f"  device  : {device.alias} ({device.model})")
    print(f"  resource: {resource}")
    print(f"  bitfile : {bitfile}")
    print(f"  period  : {args.period_us} us")
    print(f"  channels: {args.channels}")
    print()

    try:
        daq = FpgaDaq(device, bitfile=bitfile, no_run=False)
    except Exception as exc:
        print(f"FPGA open failed: {type(exc).__name__}: {exc}")
        print(
            "Stop LabVIEW RT_main.vi / startup.rtexe first.\n"
            "The FPGA session is exclusive: Python and LabVIEW RT cannot both own it."
        )
        return 2

    with daq:
        try:
            actual_depth = daq.start(period_us=args.period_us)
            print(f"  FIFO host depth : {actual_depth}")
            print(f"  period          : {daq.period_detail}")
            print(f"  snapshot        : {daq.snapshot()}")
            print()

            samples_per_read = max(args.channels * 10, args.channels)
            deadline = time.perf_counter() + args.seconds
            total = 0
            last_frame: list[float] | None = None
            while time.perf_counter() < deadline:
                volts, remaining = daq.read_volts(
                    samples_per_read, timeout_ms=args.timeout_ms
                )
                if not volts:
                    continue
                total += len(volts)
                nch = args.channels
                usable = (len(volts) // nch) * nch
                if usable:
                    last_frame = volts[usable - nch : usable]
                print(
                    f"  read {len(volts):5d} samples  remaining={remaining:6d}  "
                    f"min={min(volts):8.4f} V  max={max(volts):8.4f} V"
                )
            print()
            print(f"  total samples : {total}")
            if last_frame:
                print("  last frame (V):")
                for i, value in enumerate(last_frame):
                    print(f"    ch{i:02d}  {value:10.6f}")
        finally:
            daq.stop()

    print()
    print(" RESULT: bitfile downloaded and FIFO_AIO streamed from Python.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
