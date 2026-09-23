"""Sample-period readback and measured delivery rate.

``Count(uSec)`` is written as microseconds (5000 → nominal 200 Hz). Reading it
back only proves the host register holds that value. A matched readback with a
delivery rate near 75 Hz is not a host-side unit conversion:

- 5000 ticks of the 40 MHz FPGA clock would be 8 kHz, not ~75 Hz.
- An additive loop delay already longer than 5 ms cannot be removed by writing
  a smaller count.

Python does not rebuild the LabVIEW bitfile. When the register matches but the
FIFO does not, packets carry the measured rate so a 1 s plot window matches
wall-clock time. A true ``period_us`` clock has to be fixed in FPGA_DAQ.vi.
"""

from __future__ import annotations

from collections.abc import Callable


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
    ) -> None:
        self.nominal = float(nominal_fs)
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


def measured_rate_message(meter: MeasuredSampleRate) -> str:
    nominal = meter.nominal
    measured = meter.fs
    base = (
        f"實測取樣率 {measured:.2f} Hz（名義 {nominal:.1f} Hz，"
        f"{meter.samples} samples / {meter.elapsed:.2f}s）。"
        f" DataPacket.fs 使用實測值。"
        f" measured_fs={measured:.2f} nominal_fs={nominal:.1f}."
    )
    if nominal <= 0 or abs(measured - nominal) / nominal <= 0.02:
        return base + " 實測與名義一致。 Measured rate matches nominal."
    return (
        base
        + " 實測與 1e6/period_us 不符。若 Count(uSec) 讀回已等於要求值，"
        + " bitfile 迴圈並沒有以該微秒數取樣，需在 LabVIEW FPGA 修正 FPGA_DAQ.vi"
        + "（Python 無法重編 bitfile）。"
        + " Measured rate does not match nominal; fix the bitfile separately"
        + " if a true period_us clock is required."
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
    ) -> None:
        self.meter = MeasuredSampleRate(
            nominal_fs, min_elapsed=min_elapsed, min_samples=min_samples
        )
        self.epoch_sec = float(epoch_sec)
        self.epoch_n = int(epoch_n)
        self.processing_fs = float(nominal_fs)
        self.announced = False

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
                f"判斷時基改為實測 {self.processing_fs:.2f} Hz"
                f"（epoch={self.epoch_n} samples / {self.epoch_sec:g}s）。"
                f" judgment clock set to measured fs={self.processing_fs:.2f} Hz"
            )
        return stamp
