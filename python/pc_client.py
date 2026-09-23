"""PC-side TCP client to the cRIO RT agent."""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from protocol.framing import FrameReader, MsgType
from protocol.messages import (
    DEFAULT_PORT,
    DataPacket,
    config_payload,
    heartbeat_payload,
    hello_payload,
    parse_control,
    start_payload,
    stop_payload,
)


OnPacket = Callable[[DataPacket], None]
OnMessage = Callable[[dict], None]


class RtTcpClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.reader = FrameReader()
        self._recv_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self.on_packet: OnPacket | None = None
        self.on_message: OnMessage | None = None
        self.connected = False

    def connect(self, timeout: float = 5.0) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # create_connection leaves the connect timeout on the socket; clear it so
        # an idle linked session (connected, not yet START) does not die in 5s.
        sock.settimeout(None)
        self.sock = sock
        self.connected = True
        self._stop.clear()
        self.reader = FrameReader()
        self._recv_thread = threading.Thread(target=self._recv_loop, name="rt-tcp-recv", daemon=True)
        self._recv_thread.start()
        self.send_raw(hello_payload(role="pc"))

    def close(self) -> None:
        self._stop.set()
        self.connected = False
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)
            self._recv_thread = None

    def send_raw(self, frame: bytes) -> None:
        with self._send_lock:
            if self.sock is None:
                raise RuntimeError("not connected")
            self.sock.sendall(frame)

    def send_config(self, settings: dict) -> None:
        self.send_raw(config_payload(settings))

    def send_start(self, opts: dict | None = None) -> None:
        self.send_raw(start_payload(opts))

    def send_stop(self) -> None:
        self.send_raw(stop_payload())

    def send_heartbeat(self) -> None:
        self.send_raw(heartbeat_payload(time.time()))

    def _recv_loop(self) -> None:
        assert self.sock is not None
        sock = self.sock
        reason = "peer closed"
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except OSError as exc:
                    reason = f"recv OSError: {exc}"
                    break
                if not chunk:
                    reason = "peer closed (empty recv)"
                    break
                try:
                    frames = list(self.reader.feed(chunk))
                except Exception as exc:
                    reason = f"frame parse error: {type(exc).__name__}: {exc}"
                    if self.on_message:
                        try:
                            self.on_message({"type": "ERROR", "error": reason})
                        except Exception:
                            pass
                    break
                for msg_type, payload in frames:
                    try:
                        if msg_type == MsgType.DATA:
                            packet = parse_control(msg_type, payload)["packet"]
                            if self.on_packet:
                                self.on_packet(packet)
                        else:
                            msg = parse_control(msg_type, payload)
                            if self.on_message:
                                self.on_message(msg)
                    except Exception as exc:
                        # Handler/UI errors must not kill the TCP session.
                        err = f"handler error (ignored): {type(exc).__name__}: {exc}"
                        if self.on_message:
                            try:
                                self.on_message({"type": "ERROR", "error": err})
                            except Exception:
                                pass
                        continue
        finally:
            self.connected = False
            if self.on_message and not self._stop.is_set():
                try:
                    self.on_message({"type": "ERROR", "error": f"TCP disconnected: {reason}"})
                except Exception:
                    pass
