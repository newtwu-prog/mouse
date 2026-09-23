"""Message payloads for the mouse EEG RT protocol."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from protocol.framing import MsgType, pack_frame, pack_json, unpack_json

PROTOCOL_VERSION = 2
DEFAULT_PORT = 7001

# DATA binary layout (little-endian floats for speed on ARM/x86):
#   double  elapsed_s
#   uint32  seq
#   float32 fs
#   uint16  n_samples
#   uint16  n_groups
#   then for each group:
#     uint8  name_len
#     bytes  name (utf-8)
#     float32 eeg[n_samples]
#     float32 emg[n_samples]
#     float32 movement
#     float32 delta
#     float32 theta
#     float32 theta_delta
#     uint8   state_code   (0=WAKE,1=NREM,2=REM,255=unknown)
#     uint8   cond1        (0/1)
#     uint8   cond2        (0/1)
#     uint8   ttl          (0/1)  last sample
#     float32 ttl_trace[n_samples]   # v2: per-sample TTL 0/1


STATE_TO_CODE = {"WAKE": 0, "NREM": 1, "REM": 2, "—": 255, "": 255}
CODE_TO_STATE = {0: "WAKE", 1: "NREM", 2: "REM", 255: "—"}


@dataclass
class GroupData:
    name: str
    eeg: np.ndarray
    emg: np.ndarray
    movement: float
    delta: float
    theta: float
    theta_delta: float
    state: str
    cond1: bool
    cond2: bool
    ttl: bool
    ttl_trace: np.ndarray | None = None


@dataclass
class DataPacket:
    elapsed_s: float
    seq: int
    fs: float
    groups: list[GroupData]


def hello_payload(role: str, **extra: Any) -> bytes:
    return pack_json(
        MsgType.HELLO,
        {"version": PROTOCOL_VERSION, "role": role, **extra},
    )


def hello_ack_payload(**extra: Any) -> bytes:
    return pack_json(
        MsgType.HELLO_ACK,
        {"version": PROTOCOL_VERSION, "ok": True, **extra},
    )


def config_payload(settings: dict[str, Any]) -> bytes:
    return pack_json(MsgType.CONFIG, settings)


def start_payload(opts: dict[str, Any] | None = None) -> bytes:
    return pack_json(MsgType.START, opts or {})


def stop_payload() -> bytes:
    return pack_json(MsgType.STOP, {})


def heartbeat_payload(elapsed_s: float = 0.0) -> bytes:
    return pack_json(MsgType.HEARTBEAT, {"elapsed_s": elapsed_s})


def status_payload(obj: dict[str, Any]) -> bytes:
    return pack_json(MsgType.STATUS, obj)


def error_payload(message: str) -> bytes:
    return pack_json(MsgType.ERROR, {"error": message})


def log_payload(message: str) -> bytes:
    return pack_json(MsgType.LOG, {"message": message})


def pack_data(packet: DataPacket) -> bytes:
    n_samples = int(packet.groups[0].eeg.shape[0]) if packet.groups else 0
    parts = [
        struct.pack("<dI", float(packet.elapsed_s), int(packet.seq) & 0xFFFFFFFF),
        struct.pack("<fHH", float(packet.fs), n_samples, len(packet.groups)),
    ]
    for g in packet.groups:
        name_b = g.name.encode("utf-8")
        if len(name_b) > 255:
            raise ValueError(f"group name too long: {g.name}")
        eeg = np.asarray(g.eeg, dtype=np.float32).reshape(-1)
        emg = np.asarray(g.emg, dtype=np.float32).reshape(-1)
        if eeg.size != n_samples or emg.size != n_samples:
            raise ValueError("all groups must share n_samples")
        parts.append(struct.pack("<B", len(name_b)))
        parts.append(name_b)
        parts.append(eeg.tobytes(order="C"))
        parts.append(emg.tobytes(order="C"))
        parts.append(
            struct.pack(
                "<ffffBBBB",
                float(g.movement),
                float(g.delta),
                float(g.theta),
                float(g.theta_delta),
                STATE_TO_CODE.get(g.state, 255),
                1 if g.cond1 else 0,
                1 if g.cond2 else 0,
                1 if g.ttl else 0,
            )
        )
        if g.ttl_trace is None:
            trace = np.full(n_samples, 1.0 if g.ttl else 0.0, dtype=np.float32)
        else:
            trace = np.asarray(g.ttl_trace, dtype=np.float32).reshape(-1)
            if trace.size != n_samples:
                raise ValueError("ttl_trace length must match n_samples")
        parts.append(trace.tobytes(order="C"))
    return pack_frame(MsgType.DATA, b"".join(parts))


def unpack_data(payload: bytes) -> DataPacket:
    off = 0
    elapsed_s, seq = struct.unpack_from("<dI", payload, off)
    off += 12
    fs, n_samples, n_groups = struct.unpack_from("<fHH", payload, off)
    off += 8
    groups: list[GroupData] = []
    for _ in range(n_groups):
        (name_len,) = struct.unpack_from("<B", payload, off)
        off += 1
        name = payload[off : off + name_len].decode("utf-8")
        off += name_len
        nbytes = n_samples * 4
        eeg = np.frombuffer(payload, dtype=np.float32, count=n_samples, offset=off).copy()
        off += nbytes
        emg = np.frombuffer(payload, dtype=np.float32, count=n_samples, offset=off).copy()
        off += nbytes
        movement, delta, theta, theta_delta, state_c, c1, c2, ttl = struct.unpack_from(
            "<ffffBBBB", payload, off
        )
        off += 20
        if off + nbytes <= len(payload):
            ttl_trace = np.frombuffer(payload, dtype=np.float32, count=n_samples, offset=off).copy()
            off += nbytes
        else:
            ttl_trace = np.full(n_samples, 1.0 if ttl else 0.0, dtype=np.float32)
        groups.append(
            GroupData(
                name=name,
                eeg=eeg,
                emg=emg,
                movement=float(movement),
                delta=float(delta),
                theta=float(theta),
                theta_delta=float(theta_delta),
                state=CODE_TO_STATE.get(state_c, "—"),
                cond1=bool(c1),
                cond2=bool(c2),
                ttl=bool(ttl),
                ttl_trace=ttl_trace,
            )
        )
    return DataPacket(elapsed_s=float(elapsed_s), seq=int(seq), fs=float(fs), groups=groups)


def parse_control(msg_type: int, payload: bytes) -> dict[str, Any]:
    if msg_type == MsgType.DATA:
        return {"type": "DATA", "packet": unpack_data(payload)}
    obj = unpack_json(payload) if payload else {}
    name = MsgType(msg_type).name if msg_type in MsgType._value2member_map_ else str(msg_type)
    return {"type": name, **(obj if isinstance(obj, dict) else {"value": obj})}
