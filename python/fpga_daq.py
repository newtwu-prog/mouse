"""Host-side wrapper around the compiled LabVIEW FPGA bitfile.

The .lvbitx is already the FPGA bitstream (compiled by LabVIEW FPGA for the
Kintex-7 on cRIO-9043). Python does not execute that bitstream on the CPU.
It uses nifpga to download it to the FPGA and talk to registers / DMA FIFOs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from nifpga import Session
from nifpga.bitfile import Bitfile

from config import DEFAULT_BITFILE, DEFAULT_DEVICE_KEY, DEVICES, RTDevice
from timebase import commit_period_us

FIFO_NAME = "FIFO_AIO"
RUN_REG = "Run"
PERIOD_REG = "Count(uSec)"
TIMEOUT_REG = "Timed Out?"
AI0_REG = "Mod1/AI0"
DIO_REGS = ("DIO0 tirg", "DIO1 tirg", "DIO2 tirg")

# NI 9220 on slot 1 has 16 analog channels. FIFO_AIO is a packed FXP stream
# (signed 26-bit, 5 integer bits, range +/-16 V). Host reads volts.
DEFAULT_CHANNELS = 16
DEFAULT_FIFO_DEPTH = 100_000


def default_bitfile() -> Path:
    return (Path(__file__).resolve().parent / DEFAULT_BITFILE).resolve()


def rio_resource(device: RTDevice) -> str:
    return f"rio://{device.ip}/{device.rio_resource}"


def inspect_bitfile_text(bitfile: Path) -> str:
    parsed = Bitfile(str(bitfile))
    lines = [
        f"bitfile   : {bitfile}",
        f"signature : {parsed.signature}",
        "registers :",
    ]
    for name, reg in parsed.registers.items():
        if reg.is_internal():
            continue
        lines.append(f"  {name:24}  {reg.datatype}")
    lines.append("fifos :")
    for name, fifo in parsed.fifos.items():
        extra = ""
        fxp = fifo.type
        if fifo.is_fxp():
            extra = (
                f"  signed={getattr(fxp, '_signed', '?')}"
                f"  word={getattr(fxp, '_word_length', '?')}"
                f"  integer={getattr(fxp, '_integer_word_length', '?')}"
                f"  delta={getattr(fxp, '_delta', '?')}"
            )
        lines.append(f"  {name}  number={fifo.number}  {fifo.datatype}{extra}")
    return "\n".join(lines)


def inspect_bitfile(bitfile: Path) -> None:
    print(inspect_bitfile_text(bitfile))


def _as_float_list(data: Iterable) -> list[float]:
    return [float(v) for v in data]


def _err_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _is_fifo_reserved(exc: BaseException) -> bool:
    return "FifoReserved" in _err_text(exc)


def _is_rpc_error(exc: BaseException) -> bool:
    text = _err_text(exc)
    return "RpcConnection" in text or "-63040" in text or "-63041" in text


FIFO_RESERVED_HELP = (
    "FIFO_AIO 仍被占用（FifoReservedError）。"
    "這幾乎一定是先前 LabVIEW 留下的："
    "cRIO 上的 RT_main.vi 或開機啟動的 startup.rtexe 還握著 FPGA DMA。"
    "請先在 LabVIEW 專案停止 RT 程式，並在 NI MAX 取消「啟動時執行」；"
    "若仍失敗請重開 cRIO，再回到本程式按「關閉 FPGA」後重新「開啟 FPGA」。"
)

RPC_HELP = (
    "與 cRIO 的 FPGA RPC 連線失敗（RpcConnectionError -63040）。"
    "常見原因："
    "1) 先前 LabVIEW RT_main.vi / startup.rtexe 仍在跑或剛停但 NI-RIO Server 尚未恢復；"
    "2) 網線／IP 不穩；"
    "3) 開啟 FPGA 後連線已斷，但本機還握著舊 session。"
    "請先停止 LabVIEW RT、確認能 ping 通 cRIO，必要時重開 cRIO，"
    "再按「關閉 FPGA」→「開啟 FPGA」→「開始實驗」。"
)


def _ignore(exc_call) -> None:
    try:
        exc_call()
    except Exception:
        pass


def _raise_mapped(exc: BaseException) -> None:
    if _is_fifo_reserved(exc):
        raise RuntimeError(FIFO_RESERVED_HELP) from exc
    if _is_rpc_error(exc):
        raise RuntimeError(RPC_HELP) from exc
    raise exc


class FpgaDaq:
    """Download FPGA_DAQ.vi bitfile and stream FIFO_AIO."""

    def __init__(
        self,
        device: RTDevice | None = None,
        bitfile: Path | None = None,
        no_run: bool = False,
        resource: str | None = None,
    ) -> None:
        self.device = device
        self.bitfile = Path(bitfile) if bitfile else default_bitfile()
        if not self.bitfile.is_file():
            raise FileNotFoundError(f"bitfile not found: {self.bitfile}")
        # Local on cRIO: resource="RIO0". Remote from PC: rio://ip/RIO0.
        if resource:
            self.resource = resource
        elif device is not None:
            self.resource = rio_resource(device)
        else:
            self.resource = "RIO0"
        # Open stopped. Do NOT abort/reset/download here: those calls on a
        # remote rio:// session often drop the NI-RIO RPC link, and the next
        # StartFifo then fails with RpcConnectionError (-63040).
        try:
            self.session = Session(str(self.bitfile), self.resource, no_run=True)
        except Exception as exc:
            _raise_mapped(exc)
        self._leftover: list[float] = []
        self._fifo_configured = False
        self._fifo_started = False
        self._fpga_running = False
        self._fifo_depth = DEFAULT_FIFO_DEPTH
        self._want_run = not no_run
        self.period_readback: int | None = None
        self.period_detail = ""
        self._prepare_idle()

    def _prepare_idle(self) -> None:
        _ignore(lambda: self.session.registers[RUN_REG].write(False))
        fifo = self.session.fifos[FIFO_NAME]
        _ignore(fifo.stop)
        # Soft unreserve only; avoid reset/abort/download on open.
        _ignore(fifo.unreserve)
        self._fifo_configured = False
        self._fifo_started = False

    def _ensure_fpga_running(self) -> None:
        if self._fpga_running or not self._want_run:
            return
        try:
            self.session.run()
            self._fpga_running = True
        except Exception as exc:
            # Already running is fine; anything else map to a clear error.
            text = _err_text(exc)
            if "already" in text.lower() or "Running" in text:
                self._fpga_running = True
                return
            _raise_mapped(exc)

    def _configure_fifo(self, fifo_depth: int = DEFAULT_FIFO_DEPTH) -> int:
        if self._fifo_configured:
            return self._fifo_depth
        fifo = self.session.fifos[FIFO_NAME]
        try:
            self._fifo_depth = int(fifo.configure(requested_depth=fifo_depth))
        except Exception as exc:
            if not _is_fifo_reserved(exc):
                _raise_mapped(exc)
            _ignore(fifo.stop)
            _ignore(fifo.unreserve)
            try:
                self._fifo_depth = int(fifo.configure(requested_depth=fifo_depth))
            except Exception as exc2:
                _raise_mapped(exc2)
        self._fifo_configured = True
        return self._fifo_depth

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
        fifo = self.session.fifos[FIFO_NAME]
        _ignore(fifo.unreserve)
        try:
            self.session.close(reset_if_last_session=False)
        except Exception:
            _ignore(self.session.close)

    def __enter__(self) -> "FpgaDaq":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self, period_us: int = 5000, fifo_depth: int = DEFAULT_FIFO_DEPTH) -> int:
        fifo = self.session.fifos[FIFO_NAME]
        try:
            # LabVIEW-style host order for remote sessions:
            # idle Run=False → configure host FIFO once → run VI → StartFifo
            # → set period → Run=True.
            _ignore(lambda: self.session.registers[RUN_REG].write(False))
            self._configure_fifo(fifo_depth)
            self._ensure_fpga_running()
            if self._fifo_started:
                _ignore(fifo.stop)
                self._fifo_started = False
            try:
                fifo.start()
            except Exception as exc:
                if _is_rpc_error(exc):
                    _raise_mapped(exc)
                if not _is_fifo_reserved(exc):
                    raise
                _ignore(fifo.stop)
                _ignore(fifo.unreserve)
                self._fifo_configured = False
                self._configure_fifo(fifo_depth)
                try:
                    fifo.start()
                except Exception as exc2:
                    _raise_mapped(exc2)
            self._fifo_started = True
            self._leftover = []
            # Host writes microseconds. Do not rescale: 5000 ticks at 40 MHz
            # would be 8 kHz, and a ~75 Hz delivery with a matched readback is
            # not an integer tick conversion. See timebase.py.
            try:
                self.period_readback, self.period_detail = commit_period_us(
                    self.session.registers[PERIOD_REG], int(period_us)
                )
            except Exception:
                _ignore(fifo.stop)
                self._fifo_started = False
                raise
            self.session.registers[RUN_REG].write(True)
            return self._fifo_depth
        except Exception as exc:
            if isinstance(exc, RuntimeError) and (
                "RpcConnection" in str(exc) or "FifoReserved" in str(exc) or "FIFO_AIO" in str(exc)
            ):
                raise
            _raise_mapped(exc)

    def stop(self) -> None:
        try:
            self.session.registers[RUN_REG].write(False)
        except Exception:
            pass
        try:
            self.session.fifos[FIFO_NAME].stop()
        except Exception:
            pass
        self._fifo_started = False

    def read_volts(self, count: int, timeout_ms: int = 2000) -> tuple[list[float], int]:
        result = self.session.fifos[FIFO_NAME].read(count, timeout_ms=timeout_ms)
        remaining = getattr(result, "elements_remaining", 0)
        return _as_float_list(result.data), int(remaining)

    def read_frames(
        self, count: int, channels: int = DEFAULT_CHANNELS, timeout_ms: int = 2000
    ):
        """Read FIFO samples and reshape to (n_frames, channels)."""
        import numpy as np

        if not hasattr(self, "_leftover"):
            self._leftover = []
        volts, remaining = self.read_volts(count, timeout_ms=timeout_ms)
        combined = self._leftover + volts
        n = (len(combined) // channels) * channels
        frames = (
            np.asarray(combined[:n], dtype=float).reshape(-1, channels)
            if n
            else np.zeros((0, channels))
        )
        self._leftover = combined[n:]
        return frames, remaining

    def write_dio(self, channel: int, value: bool) -> None:
        if channel < 0 or channel >= len(DIO_REGS):
            raise ValueError(f"TTL channel must be 0..{len(DIO_REGS) - 1}, got {channel}")
        name = DIO_REGS[channel]
        if name not in self.session.registers:
            raise KeyError(f"FPGA register not found: {name}")
        self.session.registers[name].write(bool(value))

    def snapshot(self) -> dict[str, object]:
        regs = self.session.registers
        data: dict[str, object] = {}
        for name in (RUN_REG, PERIOD_REG, TIMEOUT_REG, AI0_REG):
            if name in regs:
                data[name] = regs[name].read()
        return data


def resolve_device(key: str = DEFAULT_DEVICE_KEY, ip: str | None = None) -> RTDevice:
    device = DEVICES[key]
    if ip:
        device = RTDevice(
            alias=device.alias,
            ip=ip,
            model=device.model,
            rio_resource=device.rio_resource,
        )
    return device
