"""TCP server that runs on the cRIO (or PC simulate) and streams DATA to the PC."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fpga_daq import default_bitfile
from processing.groups import groups_from_items, validate_groups
from protocol.framing import FrameReader
from protocol.messages import (
    DEFAULT_PORT,
    DataPacket,
    error_payload,
    heartbeat_payload,
    hello_ack_payload,
    log_payload,
    pack_data,
    parse_control,
    status_payload,
)
from rt_target.engine import RtRunConfig, RtTargetEngine


class ClientSession:
    def __init__(self, sock: socket.socket, addr) -> None:
        self.sock = sock
        self.addr = addr
        self.reader = FrameReader()
        self.lock = threading.Lock()
        self.alive = True

    def send(self, frame: bytes) -> None:
        with self.lock:
            if not self.alive:
                return
            try:
                self.sock.sendall(frame)
            except OSError:
                self.alive = False

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class RtTcpServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        bitfile: Path | None = None,
        resource: str = "RIO0",
        simulate: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.engine = RtTargetEngine(
            bitfile=bitfile,
            resource=resource,
            simulate=simulate,
            on_data=self._broadcast_data,
            on_log=self._broadcast_log,
        )
        self._clients: list[ClientSession] = []
        self._clients_lock = threading.Lock()
        self._settings: dict = {
            "sample_period_us": 5000,
            "epoch_sec": 12.0,
            "ttl_enabled": True,
            "ttl_output_ms": 10.0,
            "ttl_refractory_ms": 0.0,
            "tdms_path": "",
            "tdms_speed": 1.0,
            "tdms_loop": True,
            "tdms_channels": [],
            "groups": [],
        }
        self._cli_tdms = ""
        self._cli_tdms_speed = 1.0
        self._cli_tdms_loop = True
        self._cli_tdms_channels: list[str] = []
        self._stop = threading.Event()

    def _broadcast(self, frame: bytes) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        dead: list[ClientSession] = []
        for c in clients:
            c.send(frame)
            if not c.alive:
                dead.append(c)
        if dead:
            with self._clients_lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
                    c.close()

    def _broadcast_data(self, packet: DataPacket) -> None:
        try:
            self._broadcast(pack_data(packet))
        except Exception as exc:
            self._broadcast(error_payload(f"pack_data failed: {exc}"))

    def _broadcast_log(self, message: str) -> None:
        self._broadcast(log_payload(message))

    def _handle_client(self, client: ClientSession) -> None:
        client.send(hello_ack_payload(role="rt_target", simulate=self.engine.simulate))
        client.send(status_payload({"running": self.engine.running, "settings": self._settings}))
        try:
            while not self._stop.is_set() and client.alive:
                try:
                    chunk = client.sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                for msg_type, payload in client.reader.feed(chunk):
                    self._on_message(client, msg_type, payload)
        finally:
            with self._clients_lock:
                if client in self._clients:
                    self._clients.remove(client)
            client.close()

    def _on_message(self, client: ClientSession, msg_type: int, payload: bytes) -> None:
        msg = parse_control(msg_type, payload)
        kind = msg.get("type")
        if kind == "HELLO":
            client.send(hello_ack_payload(role="rt_target"))
            return
        if kind == "CONFIG":
            settings = {k: v for k, v in msg.items() if k != "type"}
            try:
                groups = groups_from_items(settings.get("groups", []))
                errors = validate_groups(groups)
                if errors:
                    client.send(error_payload("; ".join(errors)))
                    return
                self._settings.update(settings)
                self._settings["groups"] = [
                    {
                        "name": g.name,
                        "eeg_ai": g.eeg_ai,
                        "emg_ai": g.emg_ai,
                        "movement_threshold": g.movement_threshold,
                        "theta_delta_threshold": g.theta_delta_threshold,
                        "target_state": g.target_state,
                        "ttl_dio": g.ttl_dio,
                        "threshold_v": g.threshold_v,
                        "threshold_mode": g.threshold_mode,
                        "threshold_n": g.threshold_n,
                    }
                    for g in groups
                ]
                client.send(status_payload({"ok": True, "configured": True, "n_groups": len(groups)}))
                if self.engine.running:
                    self.engine.update_groups(groups)
                    self._broadcast_log(f"live config applied: {len(groups)} groups")
                else:
                    self._broadcast_log(f"config accepted: {len(groups)} groups")
            except Exception as exc:
                client.send(error_payload(str(exc)))
            return
        if kind == "START":
            try:
                groups = groups_from_items(self._settings.get("groups", []))
                errors = validate_groups(groups)
                if errors:
                    client.send(error_payload("; ".join(errors)))
                    return
                tdms_path = str(self._settings.get("tdms_path") or self._cli_tdms or "")
                chans = self._settings.get("tdms_channels") or self._cli_tdms_channels or []
                if isinstance(chans, str):
                    chans = [c.strip() for c in chans.split(",") if c.strip()]
                cfg = RtRunConfig(
                    period_us=int(self._settings.get("sample_period_us", 5000)),
                    epoch_sec=float(self._settings.get("epoch_sec", 12.0)),
                    ttl_enabled=bool(self._settings.get("ttl_enabled", True)),
                    ttl_output_ms=float(self._settings.get("ttl_output_ms", self._settings.get("ttl_pulse_ms", 10.0))),
                    ttl_refractory_ms=float(self._settings.get("ttl_refractory_ms", 0.0)),
                    tdms_path=tdms_path,
                    tdms_speed=float(self._settings.get("tdms_speed", self._cli_tdms_speed)),
                    tdms_loop=bool(self._settings.get("tdms_loop", self._cli_tdms_loop)),
                    tdms_channels=tuple(chans),
                )
                self.engine.stop()
                self.engine.start(groups, cfg)
                client.send(status_payload({"running": True, "tdms": bool(tdms_path)}))
                self._broadcast_log("START" + (f" tdms={tdms_path}" if tdms_path else ""))
            except Exception as exc:
                client.send(error_payload(f"START failed: {exc}"))
            return
        if kind == "STOP":
            self.engine.stop()
            client.send(status_payload({"running": False}))
            self._broadcast_log("STOP")
            return
        if kind == "HEARTBEAT":
            client.send(heartbeat_payload(time.time()))
            return

    def serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        srv.settimeout(1.0)
        mode = "SIMULATE" if self.engine.simulate else f"FPGA {self.engine.resource}"
        print(f"RT TCP server on {self.host}:{self.port}  [{mode}]", flush=True)
        try:
            while not self._stop.is_set():
                try:
                    sock, addr = srv.accept()
                except socket.timeout:
                    continue
                print(f"PC connected: {addr}", flush=True)
                client = ClientSession(sock, addr)
                with self._clients_lock:
                    self._clients.append(client)
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True,
                    name=f"pc-{addr[1]}",
                ).start()
        finally:
            self._stop.set()
            self.engine.stop()
            srv.close()
            with self._clients_lock:
                for c in list(self._clients):
                    c.close()
                self._clients.clear()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="cRIO RT agent (TCP server)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--bitfile", default="", help="path to .lvbitx on the cRIO")
    p.add_argument("--resource", default="RIO0", help="local RIO0 on cRIO")
    p.add_argument("--simulate", action="store_true", help="no FPGA; generate test signals")
    p.add_argument("--tdms", default="", help="replay a real mouse TDMS file instead of FPGA")
    p.add_argument("--tdms-speed", type=float, default=1.0, help="replay speed (e.g. 10 = 10x)")
    p.add_argument("--tdms-once", action="store_true", help="stop at end of TDMS (default: loop)")
    p.add_argument(
        "--tdms-channels",
        default="",
        help="comma-separated channel names in AI order (EEG,EMG,...)",
    )
    args = p.parse_args(argv)

    bitfile = Path(args.bitfile) if args.bitfile else default_bitfile()
    using_tdms = bool(args.tdms)
    if using_tdms and not Path(args.tdms).is_file():
        print(f"TDMS not found: {args.tdms}")
        return 1
    if not args.simulate and not using_tdms and not bitfile.is_file():
        print(f"bitfile not found: {bitfile}")
        print("Copy the .lvbitx, or pass --bitfile, or use --simulate / --tdms")
        return 1

    server = RtTcpServer(
        host=args.host,
        port=args.port,
        bitfile=bitfile,
        resource=args.resource,
        simulate=args.simulate and not using_tdms,
    )
    server._cli_tdms = args.tdms
    server._cli_tdms_speed = args.tdms_speed
    server._cli_tdms_loop = not args.tdms_once
    server._cli_tdms_channels = [c.strip() for c in args.tdms_channels.split(",") if c.strip()]
    if using_tdms:
        server._settings["tdms_path"] = args.tdms
        server._settings["tdms_speed"] = args.tdms_speed
        server._settings["tdms_loop"] = not args.tdms_once
        server._settings["tdms_channels"] = server._cli_tdms_channels
        print(f"TDMS replay mode: {args.tdms}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
