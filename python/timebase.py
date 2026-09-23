"""FPGA sample period and a wall-clock check that it really is that rate.

Normal use keeps the default ``Count(uSec)=5000`` (200 Hz). The same path
writes whatever period the existing setting carries: the host does not lock
the register at 5000, and it does not invent a second "change sample rate"
flow. The value is microseconds and is read back. It is not rewritten as
ticks or as some other count that would only look like the requested rate:

- 5000 ticks of the 40 MHz FPGA clock would be 8 kHz, not 200 Hz.
- A delivery rate near 75 Hz is not an integer rescaling of 5000 µs.
- If the readback is 5000 and the wall clock is still far from 200 Hz, the
  bitstream is not waiting ``Count(uSec)`` microseconds. Python cannot
  rebuild the bitfile; ``FPGA_DAQ.vi`` has to be fixed in LabVIEW FPGA.

Measured ``DataPacket.fs`` is a diagnostic and keeps the plot from stretching
a slow stream a second time. It does not create 200 samples per second.
"""

from __future__ import annotations

from collections.abc import Callable

# Wall-clock rate must land inside this fraction of 1e6/period_us.
# 75 Hz vs 200 Hz fails; a few percent of scheduler jitter does not.
NOMINAL_RATE_TOLERANCE = 0.10


def _as_period_int(value: object) -> int:
    if isinstance(value, bool) or isinstance(value, str) or value is None:
        raise TypeError(type(value).__name__)
    number = float(value)  # type: ignore[arg-type]
    if number != number or abs(number) == float("inf"):
        raise ValueError(repr(value))
    rounded = round(number)
    if abs(number - rounded) > 1e-6:
        raise ValueError(repr(value))
    return int(rounded)


def describe_period_readback(requested: int, readback: object) -> tuple[bool, str]:
    """Return whether ``readback`` is the requested Count(uSec), plus a log line."""
    requested = int(requested)
    try:
        got = _as_period_int(readback)
    except (TypeError, ValueError):
        return False, (
            f"Count(uSec) 讀回值無法解讀 ({readback!r})，要求 {requested}。"
            f" Cannot interpret Count(uSec) readback; requested {requested}."
        )
    if got != requested:
        return False, (
            f"Count(uSec) 讀回 {got}，與要求的 {requested} 不符，FPGA 沒有接受這個取樣週期。"
            f" Count(uSec) readback {got} != requested {requested}."
        )
    return True, f"Count(uSec) readback {got} == requested {requested}"


def commit_period_us(register, period_us: int) -> tuple[int, str]:
    """Write Count(uSec), read it back, and fail if it did not stick."""
    requested = int(period_us)
    register.write(requested)
    try:
        readback = register.read()
    except Exception as exc:
        raise RuntimeError(
            f"Count(uSec) 寫入 {requested} 後無法讀回：{type(exc).__name__}: {exc}. "
            f"Could not read back Count(uSec) after writing {requested}."
        ) from exc
    ok, detail = describe_period_readback(requested, readback)
    if not ok:
        raise RuntimeError(detail)
    return _as_period_int(readback), detail


class MeasuredSampleRate:
    """Wall-clock rate of delivered frames, stable enough to stamp on packets.

    The first observation only starts the clock (those samples arrived before
    ``t0``). After ``min_elapsed`` and ``min_samples``, ``fs`` becomes the
    cumulative count / elapsed time. Later updates move ``fs`` only when that
    average drifts by more than 1%, so the plot timebase does not flicker.
    """

    def __init__(
        self,
        nominal_fs: float,
        min_elapsed: float = 1.0,
        min_samples: int = 40,
        period_us: int | None = None,
    ) -> None:
        self.nominal = float(nominal_fs)
        self.period_us = _requested_period_us(self.nominal, period_us)
        self.fs = float(nominal_fs)
        self.min_elapsed = float(min_elapsed)
        self.min_samples = int(min_samples)
        self.elapsed = 0.0
        self.samples = 0
        self.settled = False
        self._t0: float | None = None
        self._samples = 0

    def observe(self, n_frames: int, now: float) -> float:
        if n_frames <= 0:
            return self.fs
        if self._t0 is None:
            self._t0 = float(now)
            return self.fs
        if now <= self._t0:
            return self.fs
        self._samples += int(n_frames)
        self.samples = self._samples
        self.elapsed = float(now) - self._t0
        if self.elapsed < self.min_elapsed or self._samples < self.min_samples:
            return self.fs
        measured = self._samples / self.elapsed
        if not (1.0 <= measured <= 100_000.0):
            return self.fs
        if not self.settled or abs(measured - self.fs) / self.fs > 0.01:
            self.fs = measured
            self.settled = True
        return self.fs


def _requested_period_us(nominal_fs: float, period_us: int | None) -> int:
    """Microseconds actually requested. Default 5000 only when none was given."""
    if period_us is not None and int(period_us) > 0:
        return int(period_us)
    if nominal_fs <= 0:
        return 5000
    return max(int(round(1_000_000.0 / float(nominal_fs))), 1)


def rate_is_near_nominal(
    measured_hz: float,
    nominal_hz: float,
    tolerance: float = NOMINAL_RATE_TOLERANCE,
) -> bool:
    """True when the delivered rate is close to 1e6/period_us (200 Hz at 5000 µs)."""
    if nominal_hz <= 0 or measured_hz <= 0:
        return False
    return abs(measured_hz - nominal_hz) / nominal_hz <= tolerance


def vi_restart_for_period(resource: str, vi_running: bool) -> tuple[bool, str]:
    """Whether to abort the VI so the next run() re-reads Count(uSec).

    Abort keeps host-written controls. reset() and download() restore the
    bitfile default and would wipe the period. Remote rio:// abort often
    drops the NI-RIO RPC link, so that path is left running and reported.
    """
    if not vi_running:
        return False, ""
    if str(resource).startswith("rio://"):
        return False, (
            " remote rio:// session left running; VI not aborted"
            " (avoids RpcConnectionError). A bitstream that latches"
            " Count(uSec) only at VI start keeps the old period until the"
            " FPGA session is opened locally on the cRIO (RIO0)."
        )
    return True, ""


def measured_rate_message(meter: MeasuredSampleRate) -> str:
    nominal = meter.nominal
    measured = meter.fs
    period = meter.period_us
    stats = (
        f"measured_fs={measured:.2f} nominal_fs={nominal:.1f} "
        f"Count(uSec)={period} "
        f"samples={meter.samples} elapsed_s={meter.elapsed:.2f}."
    )
    if rate_is_near_nominal(measured, nominal):
        return (
            f"取樣率確認：實測 {measured:.2f} Hz，接近要求的 {nominal:.1f} Hz"
            f"（Count(uSec)={period} µs）。 Sample rate OK. {stats}"
        )
    return (
        f"取樣率斷言失敗：FPGA 應以約 {nominal:.1f} Hz 取樣"
        f"（Count(uSec)={period} µs），實測只有 {measured:.2f} Hz。"
        f" 若讀回已等於 {period}，bitfile 並沒有用該微秒數計時；"
        f"Python 不能改寫另一個 count 來假造這個速率，必須在 LabVIEW FPGA 修改"
        f" FPGA_DAQ.vi。DataPacket.fs 暫時填實測值，只讓圖對齊真實間隔，"
        f"硬體仍然是 {measured:.2f} Hz。"
        f" ASSERTION FAILED: hardware is not sampling near the requested period. {stats}"
        f" Bitfile is not honoring Count(uSec) as microseconds."
    )


def apply_processing_fs(fs: float, scorer, runtimes, epoch_sec: float) -> int:
    """Point sleep-staging and TTL pulse lengths at the delivered rate."""
    scorer.fs = float(fs)
    for runtime in runtimes.values():
        runtime.retarget_fs(fs)
    return max(int(round(float(fs) * float(epoch_sec))), 8)


class RateFollow:
    """Stamp packets with the measured rate and retarget judgment when it sticks."""

    def __init__(
        self,
        nominal_fs: float,
        epoch_sec: float,
        epoch_n: int,
        *,
        min_elapsed: float = 1.0,
        min_samples: int = 40,
        period_us: int | None = None,
    ) -> None:
        self.meter = MeasuredSampleRate(
            nominal_fs,
            min_elapsed=min_elapsed,
            min_samples=min_samples,
            period_us=period_us,
        )
        self.epoch_sec = float(epoch_sec)
        self.epoch_n = int(epoch_n)
        self.processing_fs = float(nominal_fs)
        self.announced = False
        self.rate_ok: bool | None = None

    def observe(
        self,
        n_frames: int,
        now: float,
        scorer,
        runtimes,
        log: Callable[[str], None],
    ) -> float:
        stamp = self.meter.observe(n_frames, now)
        if self.meter.settled and not self.announced:
            self.announced = True
            self.rate_ok = rate_is_near_nominal(self.meter.fs, self.meter.nominal)
            log(measured_rate_message(self.meter))
        # Ignore sub-2% jitter so a matched nominal clock does not rebuild epochs.
        if (
            self.meter.settled
            and abs(self.meter.fs - self.processing_fs) / self.processing_fs > 0.02
        ):
            self.processing_fs = float(self.meter.fs)
            self.epoch_n = apply_processing_fs(
                self.processing_fs, scorer, runtimes, self.epoch_sec
            )
            log(
                f"判斷暫用實測 {self.processing_fs:.2f} Hz"
                f"（名義 {self.meter.nominal:.1f} Hz，"
                f"epoch={self.epoch_n} samples / {self.epoch_sec:g}s）。"
                f" Judgment is using the delivered rate; this does not make"
                f" the FPGA sample at nominal."
            )
        return stamp
