"""Connect a PC-side Python client to an NI CompactRIO Real-Time target.

This is the first step toward replacing the LabVIEW PC application
(test2.vi / Network Streams) with Python. It does not steal the FPGA
session from LabVIEW RT_main.vi unless you pass --open-fpga.

Use Python 3.11 (NI packages are not installed for 3.14 on this PC):

    py -3.11 python/connect_rt.py
    py -3.11 python/connect_rt.py --ip 192.168.0.110
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    DEFAULT_BITFILE,
    DEFAULT_DEVICE_KEY,
    DEVICES,
    PROBE_PORTS,
    RTDevice,
)

CONNECT_TIMEOUT_S = 1.5


def _safe_attr(obj, name: str, default=""):
    try:
        value = getattr(obj, name)
        return default if value is None else value
    except Exception:
        return default


def _indexed_values(items) -> list[str]:
    if items is None:
        return []
    try:
        return [str(v) for v in items if v]
    except Exception:
        return []


def ping_host(ip: str, timeout_s: float = CONNECT_TIMEOUT_S) -> bool:
    """TCP-connect to a closed port is not ICMP; use a short socket timeout on 80/22."""
    for port in (22, 80, 3580, 2343):
        if probe_port(ip, port):
            return True
    return False


def probe_port(ip: str, port: int, timeout_s: float = CONNECT_TIMEOUT_S) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def print_header(device: RTDevice) -> None:
    print("=" * 64)
    print(" NI CompactRIO RT connection check")
    print("=" * 64)
    print(f"  alias : {device.alias}")
    print(f"  model : {device.model}")
    print(f"  ip    : {device.ip}")
    print(f"  RIO   : {device.rio_resource}")
    print()


def connection_report(device: RTDevice) -> tuple[bool, str]:
    """Return (reachable, text) for GUI / logs without printing."""
    lines = [
        f"alias : {device.alias}",
        f"model : {device.model}",
        f"ip    : {device.ip}",
        f"RIO   : {device.rio_resource}",
        "",
    ]
    reachable = ping_host(device.ip)
    lines.append(f"host reachable : {'YES' if reachable else 'NO'}")
    if not reachable:
        lines.append(f"cannot open TCP to {device.ip}. Check Ethernet / IP / power.")
        return False, "\n".join(lines)

    lines.append("ports:")
    for port, label in PROBE_PORTS:
        state = "OPEN" if probe_port(device.ip, port) else "closed"
        lines.append(f"  {port:<5} {state:<6}  {label}")

    try:
        import nisyscfg

        lines.append("")
        lines.append("hardware:")
        with nisyscfg.Session(device.ip) as session:
            seen: set[tuple[str, str]] = set()
            for res in session.find_hardware():
                product = str(_safe_attr(res, "product_name") or "")
                serial = str(_safe_attr(res, "serial_number") or "")
                slot = _safe_attr(res, "slot_number", None)
                aliases = _indexed_values(_safe_attr(res, "expert_user_alias", None))
                key = (product, serial)
                if not product or key in seen:
                    continue
                seen.add(key)
                slot_txt = f" slot {slot}" if isinstance(slot, int) and slot >= 0 else ""
                alias_txt = f"  {aliases[0]}" if aliases else ""
                lines.append(f"  - {product}  SN={serial}{slot_txt}{alias_txt}")
    except Exception as exc:
        lines.append(f"nisyscfg: {type(exc).__name__}: {exc}")
    return True, "\n".join(lines)


def check_network(device: RTDevice) -> bool:
    print("[1] Network")
    reachable = ping_host(device.ip)
    print(f"  host reachable : {'YES' if reachable else 'NO'}")
    if not reachable:
        print(f"  cannot open TCP to {device.ip}. Check Ethernet / IP / power.")
        return False

    print("  ports:")
    for port, label in PROBE_PORTS:
        state = "OPEN" if probe_port(device.ip, port) else "closed"
        print(f"    {port:<5} {state:<6}  {label}")
    print()
    return True


def check_syscfg(device: RTDevice) -> bool:
    print("[2] NI System Configuration (nisyscfg)")
    try:
        import nisyscfg
    except ImportError:
        print("  nisyscfg is not installed.  py -3.11 -m pip install nisyscfg")
        print()
        return False

    try:
        with nisyscfg.Session(device.ip) as session:
            print(f"  session opened : {device.ip}")
            print("  hardware:")
            seen: set[tuple[str, str, str]] = set()
            listed = 0
            for res in session.find_hardware():
                product = str(_safe_attr(res, "product_name") or "")
                serial = str(_safe_attr(res, "serial_number") or "")
                slot = _safe_attr(res, "slot_number", None)
                firmware = str(_safe_attr(res, "firmware_revision") or "")
                aliases = _indexed_values(_safe_attr(res, "expert_user_alias", None))
                experts = _indexed_values(_safe_attr(res, "expert_name", None))
                if not product and not aliases:
                    continue
                key = (product, serial, ",".join(aliases))
                if key in seen:
                    continue
                seen.add(key)
                listed += 1

                slot_txt = f" slot {slot}" if isinstance(slot, int) and slot >= 0 else ""
                fw_txt = f"  fw={firmware}" if firmware else ""
                alias_txt = f"  alias={aliases[0]}" if aliases else ""
                expert_txt = f"  [{', '.join(experts)}]" if experts else ""
                print(f"    - {product}  SN={serial}{slot_txt}{alias_txt}{fw_txt}{expert_txt}")
            if listed == 0:
                print("    (no named hardware resources returned)")
        print()
        return True
    except Exception as exc:
        print(f"  failed to open nisyscfg session: {type(exc).__name__}: {exc}")
        print()
        return False


def check_daqmx() -> None:
    print("[3] NI-DAQmx local device list")
    try:
        import nidaqmx.system
    except ImportError:
        print("  nidaqmx is not installed.  py -3.11 -m pip install nidaqmx")
        print()
        return

    try:
        system = nidaqmx.system.System.local()
        print(f"  driver : {system.driver_version}")
        devices = list(system.devices)
        if not devices:
            print("  no local DAQmx devices")
        for dev in devices:
            print(f"    - {dev.name}  ({dev.product_type})")
        print(
            "  note: CompactRIO in FPGA mode usually does NOT appear here.\n"
            "        DAQmx acquisition would require switching the chassis out of FPGA mode."
        )
    except Exception as exc:
        print(f"  nidaqmx error: {type(exc).__name__}: {exc}")
    print()


def open_fpga(device: RTDevice, bitfile: Path) -> None:
    print("[4] Optional FPGA session (nifpga)")
    if not bitfile.is_file():
        print(f"  bitfile not found: {bitfile}")
        print()
        return

    try:
        from nifpga import Session
    except ImportError:
        print("  nifpga is not installed.  py -3.11 -m pip install nifpga")
        print()
        return

    resource = f"rio://{device.ip}/{device.rio_resource}"
    print(f"  resource : {resource}")
    print(f"  bitfile  : {bitfile}")
    print("  opening with no_run=True (fails if LabVIEW RT already owns FPGA)")
    try:
        with Session(str(bitfile), resource, no_run=True) as session:
            print("  FPGA session opened")
            print(f"    registers={list(session.registers)}")
            print(f"    fifos={list(session.fifos)}")
    except Exception as exc:
        print(f"  FPGA open failed: {type(exc).__name__}: {exc}")
        print(
            "  this is expected while RT_main.vi / startup.rtexe is running,\n"
            "  because LabVIEW RT already holds the FPGA session."
        )
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect Python to an NI CompactRIO Real-Time target."
    )
    parser.add_argument(
        "--device",
        choices=sorted(DEVICES),
        default=DEFAULT_DEVICE_KEY,
        help="device key from LabVIEW aliases (default: 9043)",
    )
    parser.add_argument("--ip", help="override IP address")
    parser.add_argument(
        "--open-fpga",
        action="store_true",
        help="also try to open the FPGA session (conflicts with LabVIEW RT)",
    )
    parser.add_argument(
        "--bitfile",
        default=str(Path(__file__).resolve().parent / DEFAULT_BITFILE),
        help="path to .lvbitx used with --open-fpga",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    device = DEVICES[args.device]
    if args.ip:
        device = RTDevice(
            alias=device.alias,
            ip=args.ip,
            model=device.model,
            rio_resource=device.rio_resource,
        )

    print_header(device)
    ok = check_network(device)
    if not ok:
        return 1

    syscfg_ok = check_syscfg(device)
    check_daqmx()
    if args.open_fpga:
        open_fpga(device, Path(args.bitfile))

    print("=" * 64)
    if syscfg_ok:
        print(" RESULT: Python connected to the RT target via nisyscfg.")
        print(" Next: replace LabVIEW Network Streams with TCP (or nifpga FIFO).")
        return 0
    print(" RESULT: host is reachable, but nisyscfg session failed.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
