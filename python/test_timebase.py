"""Timebase: Count(uSec) readback and measured packet fs."""

from __future__ import annotations

import unittest

import numpy as np

from timebase import (
    MeasuredSampleRate,
    RateFollow,
    commit_period_us,
    describe_period_readback,
    rate_is_near_nominal,
    vi_restart_for_period,
)


class _Reg:
    def __init__(self, stored=0, ignore_write=False, fail_read=False):
        self.stored = stored
        self.ignore_write = ignore_write
        self.fail_read = fail_read
        self.writes = []

    def write(self, value):
        self.writes.append(value)
        if not self.ignore_write:
            self.stored = value

    def read(self):
        if self.fail_read:
            raise OSError("register unreachable")
        return self.stored


class _Scorer:
    def __init__(self, fs):
        self.fs = fs


class _Runtime:
    def __init__(self, fs):
        self.fs = fs

    def retarget_fs(self, fs):
        self.fs = float(fs)


def _cycles_in_window(sample_hz: float, tone_hz: float, axis_fs: float, window_s: float = 1.0) -> float:
    """Cycles of a tone drawn the way the PC charts draw it.

    The window keeps about ``axis_fs * window_s`` samples and labels that span
    as ``window_s`` seconds (``t = arange(n) / axis_fs``).
    """
    n = max(int(round(axis_fs * window_s)), 40)
    return tone_hz * (n / sample_hz)


class PeriodReadbackTest(unittest.TestCase):
    def test_match(self):
        ok, detail = describe_period_readback(5000, 5000)
        self.assertTrue(ok)
        self.assertIn("5000", detail)
        self.assertIn("==", detail)

    def test_numpy_and_float_match(self):
        ok, _detail = describe_period_readback(5000, np.uint32(5000))
        self.assertTrue(ok)
        ok, _detail = describe_period_readback(5000, 5000.0)
        self.assertTrue(ok)

    def test_mismatch_names_both_values(self):
        ok, detail = describe_period_readback(5000, 0)
        self.assertFalse(ok)
        self.assertIn("5000", detail)
        self.assertIn("0", detail)

    def test_commit_reads_back_requested_period(self):
        reg = _Reg(stored=1)
        got, detail = commit_period_us(reg, 5000)
        self.assertEqual(reg.writes, [5000])
        self.assertEqual(got, 5000)
        self.assertIn("==", detail)

    def test_commit_fails_when_write_does_not_stick(self):
        reg = _Reg(stored=0, ignore_write=True)
        with self.assertRaises(RuntimeError) as caught:
            commit_period_us(reg, 5000)
        self.assertIn("5000", str(caught.exception))
        self.assertIn("0", str(caught.exception))

    def test_commit_fails_when_read_raises(self):
        reg = _Reg(fail_read=True)
        with self.assertRaises(RuntimeError) as caught:
            commit_period_us(reg, 5000)
        self.assertIn("5000", str(caught.exception))
        self.assertIn("OSError", str(caught.exception))


class MeasuredRateTest(unittest.TestCase):
    def test_nominal_until_one_second_then_delivered_rate(self):
        meter = MeasuredSampleRate(200.0)
        # 40 samples every 0.533 s → 75.05 Hz. First call only starts the clock.
        self.assertEqual(meter.observe(40, 0.0), 200.0)
        self.assertFalse(meter.settled)
        self.assertEqual(meter.observe(40, 0.533), 200.0)
        stamp = meter.observe(40, 1.066)
        self.assertTrue(meter.settled)
        self.assertAlmostEqual(stamp, 80 / 1.066, places=5)
        self.assertAlmostEqual(stamp, 75.05, delta=0.2)

    def test_published_fs_does_not_flicker_inside_one_percent(self):
        meter = MeasuredSampleRate(200.0)
        meter.observe(40, 0.0)
        meter.observe(40, 0.5)
        settled = meter.observe(40, 1.0)
        self.assertAlmostEqual(settled, 80.0)
        # 80 + 159 = 239 samples / 3 s = 79.67 Hz, 0.4% under the published 80 Hz.
        again = meter.observe(159, 3.0)
        self.assertEqual(again, settled)

    def test_published_fs_tracks_a_real_drift(self):
        meter = MeasuredSampleRate(200.0)
        meter.observe(40, 0.0)
        meter.observe(80, 1.0)
        self.assertTrue(meter.settled)
        updated = meter.observe(10, 1.05)
        self.assertNotEqual(updated, 80.0)
        self.assertAlmostEqual(updated, 90 / 1.05, places=5)

    def test_four_hertz_window_uses_measured_fs(self):
        sample_hz = 75.0
        stretched = _cycles_in_window(sample_hz, 4.0, axis_fs=200.0)
        corrected = _cycles_in_window(sample_hz, 4.0, axis_fs=sample_hz)
        self.assertAlmostEqual(stretched, 4.0 * 200.0 / 75.0, places=5)
        self.assertGreater(stretched, 10.0)
        self.assertAlmostEqual(corrected, 4.0, places=5)


class RateFollowTest(unittest.TestCase):
    def test_retargets_judgment_when_delivery_is_not_nominal(self):
        follow = RateFollow(200.0, epoch_sec=12.0, epoch_n=2400)
        scorer = _Scorer(200.0)
        runtimes = {"g": _Runtime(200.0)}
        logs: list[str] = []
        stamp = follow.observe(40, 0.0, scorer, runtimes, logs.append)
        self.assertEqual(stamp, 200.0)
        self.assertEqual(scorer.fs, 200.0)
        self.assertEqual(logs, [])
        follow.observe(75, 1.0, scorer, runtimes, logs.append)
        self.assertAlmostEqual(follow.meter.fs, 75.0, places=5)
        self.assertAlmostEqual(scorer.fs, 75.0, places=5)
        self.assertAlmostEqual(runtimes["g"].fs, 75.0, places=5)
        self.assertEqual(follow.epoch_n, int(round(75.0 * 12.0)))
        self.assertTrue(any("measured_fs=" in line for line in logs))
        self.assertTrue(any("ASSERTION FAILED" in line for line in logs))
        self.assertTrue(any("FPGA_DAQ.vi" in line for line in logs))
        self.assertFalse(follow.rate_ok)
        # Further packets at the same rate do not log again.
        n_logs = len(logs)
        follow.observe(75, 2.0, scorer, runtimes, logs.append)
        self.assertEqual(len(logs), n_logs)

    def test_near_nominal_does_not_rebuild_epochs(self):
        follow = RateFollow(200.0, epoch_sec=12.0, epoch_n=2400)
        scorer = _Scorer(200.0)
        runtimes = {"g": _Runtime(200.0)}
        # 202 samples over the second after t0 → 202 Hz, 1% high.
        follow.observe(1, 0.0, scorer, runtimes, lambda _msg: None)
        stamp = follow.observe(202, 1.0, scorer, runtimes, lambda _msg: None)
        self.assertAlmostEqual(stamp, 202.0)
        self.assertEqual(scorer.fs, 200.0)
        self.assertEqual(follow.epoch_n, 2400)
        self.assertTrue(follow.rate_ok)


class NominalAssertTest(unittest.TestCase):
    def test_75_hz_is_not_near_200(self):
        self.assertFalse(rate_is_near_nominal(75.0, 200.0))
        self.assertTrue(rate_is_near_nominal(198.0, 200.0))
        self.assertTrue(rate_is_near_nominal(220.0, 200.0))
        self.assertFalse(rate_is_near_nominal(221.0, 200.0))

    def test_restart_only_a_running_local_vi(self):
        restart, note = vi_restart_for_period("RIO0", False)
        self.assertFalse(restart)
        self.assertEqual(note, "")
        restart, note = vi_restart_for_period("RIO0", True)
        self.assertTrue(restart)
        self.assertEqual(note, "")
        restart, note = vi_restart_for_period("rio://192.168.0.110/RIO0", True)
        self.assertFalse(restart)
        self.assertIn("rio://", note)


if __name__ == "__main__":
    unittest.main()
