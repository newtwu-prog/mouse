from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


class LowpassBank:
    """Streaming Butterworth low-pass, matching LabVIEW LowPassFilter.vi."""

    def __init__(self, fs: float, cutoff_hz: float, n_channels: int, order: int = 4) -> None:
        nyquist = fs / 2.0
        cutoff = min(max(cutoff_hz, 0.1), nyquist * 0.95)
        self.sos = butter(order, cutoff / nyquist, btype="low", output="sos")
        zi0 = sosfilt_zi(self.sos)
        self._zi = np.repeat(zi0[:, :, None], n_channels, axis=2)

    def process(self, frames: np.ndarray) -> np.ndarray:
        if frames.size == 0:
            return frames
        filtered, self._zi = sosfilt(self.sos, frames, axis=0, zi=self._zi)
        return filtered
