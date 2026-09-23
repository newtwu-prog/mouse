"""Streaming bandpass → square (energy) for PC display."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


class BandEnergy:
    def __init__(self, fs: float, low: float, high: float, order: int = 4) -> None:
        nyquist = fs / 2.0
        high = min(high, nyquist * 0.95)
        low = min(max(low, 0.05), high * 0.95)
        self.sos = butter(order, [low / nyquist, high / nyquist], btype="band", output="sos")
        self._zi = sosfilt_zi(self.sos) * 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float).reshape(-1)
        if x.size == 0:
            return x
        y, self._zi = sosfilt(self.sos, x, zi=self._zi)
        return y * y
