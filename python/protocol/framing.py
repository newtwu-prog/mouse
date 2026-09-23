"""Shared TCP framing for PC <-> cRIO RT agent.

Wire format (big-endian):
  uint32 payload_length
  uint8  msg_type
  bytes  payload

Config / control payloads are UTF-8 JSON.
Waveform DATA payloads are binary (see messages.pack_data / unpack_data).
"""

from __future__ import annotations

import json
import struct
from enum import IntEnum
from typing import Any


HEADER = struct.Struct(">IB")  # length includes type byte? No: length = 1 + payload
# length field = size of (type + payload)


class MsgType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    CONFIG = 3
    START = 4
    STOP = 5
    HEARTBEAT = 6
    STATUS = 7
    DATA = 8
    ERROR = 9
    LOG = 10


def pack_frame(msg_type: MsgType | int, payload: bytes = b"") -> bytes:
    body = bytes([int(msg_type)]) + payload
    return struct.pack(">I", len(body)) + body


def pack_json(msg_type: MsgType | int, obj: Any) -> bytes:
    return pack_frame(msg_type, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def unpack_json(payload: bytes) -> Any:
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


class FrameReader:
    """Incremental TCP frame reassembly."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while True:
            if len(self._buf) < 4:
                break
            (nbytes,) = struct.unpack_from(">I", self._buf, 0)
            if nbytes < 1 or nbytes > 64 * 1024 * 1024:
                raise ValueError(f"invalid frame length: {nbytes}")
            total = 4 + nbytes
            if len(self._buf) < total:
                break
            body = bytes(self._buf[4:total])
            del self._buf[:total]
            out.append((body[0], body[1:]))
        return out
