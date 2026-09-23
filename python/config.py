"""Device addresses copied from Untitled Project 1.aliases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RTDevice:
    alias: str
    ip: str
    model: str
    rio_resource: str = "RIO0"


# LabVIEW project aliases (Untitled Project 1.aliases)
DEVICES: dict[str, RTDevice] = {
    "9043": RTDevice(
        alias="NI-cRIO-9043-01CF63E4",
        ip="192.168.0.110",
        model="cRIO-9043",
    ),
    "9055": RTDevice(
        alias="NI-cRIO-9055-022DEB83",
        ip="192.168.0.150",
        model="cRIO-9055",
    ),
    "9055b": RTDevice(
        alias="NI-cRIO-9055-01E955BE",
        ip="192.168.0.180",
        model="cRIO-9055",
    ),
}

DEFAULT_DEVICE_KEY = "9043"

# FPGA bitfile currently used by the 9043 target (FPGA_DAQ.vi).
DEFAULT_BITFILE = (
    r"..\FPGA Bitfiles\untitledproject1_FPGATarget_FPGADAQ6_GFVDrwkaPc4.lvbitx"
)

# NI / Linux RT ports that are useful for a first connectivity check.
PROBE_PORTS: tuple[tuple[int, str], ...] = (
    (22, "SSH (Linux RT shell)"),
    (80, "HTTP"),
    (443, "HTTPS"),
    (2343, "NI Service Locator"),
    (3363, "LabVIEW VI Server"),
    (3580, "NI-PSP / Network Streams"),
    (8000, "RT Web Server"),
    (8001, "RT Debug Web Server"),
)
