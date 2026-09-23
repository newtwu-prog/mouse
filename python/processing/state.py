from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt


def _bandpass_sum_sq(x: np.ndarray, fs: float, low: float, high: float) -> float:
    """Bandpass then sum of squares (Σ y²)."""
    nyquist = fs / 2.0
    high = min(high, nyquist * 0.95)
    low = min(max(low, 0.05), high * 0.95)
    if low >= high:
        return 0.0
    sos = butter(4, [low / nyquist, high / nyquist], btype="band", output="sos")
    y = sosfilt(sos, np.asarray(x, dtype=float))
    return float(np.sum(y * y))


def _movement_sum_sq_diff(emg: np.ndarray) -> float:
    """Σ (x[n] - x[n-1])² over the 12 s EMG window."""
    x = np.asarray(emg, dtype=float)
    if x.size < 2:
        return 0.0
    diff = np.diff(x)
    return float(np.sum(diff * diff))


# Previous epoch must already be sleep for REM (blocks WAKE → REM jumps).
REM_PREV_STATES = frozenset({"NREM", "REM"})


class StateScorer:
    """12 s WAKE / NREM / REM from raw EEG + EMG.

    EEG:
      delta = Σ (bandpass 0.5–4 Hz)²
      theta = Σ (bandpass 4–8 Hz)²
      ratio = theta / delta  vs  theta/delta threshold

    EMG / Movement:
      movement = Σ (x[n] - x[n-1])²  vs  movement threshold

    Rule:
      Movement high → WAKE
      else if (θ/δ high and previous state in {NREM, REM}) → REM
      else → NREM
    """

    def __init__(self, fs: float) -> None:
        self.fs = fs

    def score(
        self,
        eeg: np.ndarray,
        emg: np.ndarray,
        movement_threshold: float,
        theta_delta_threshold: float,
        previous_state: str | None = None,
    ) -> dict:
        delta = _bandpass_sum_sq(eeg, self.fs, 0.5, 4.0)
        theta = _bandpass_sum_sq(eeg, self.fs, 4.0, 8.0)
        ratio = theta / (delta + 1e-18)
        movement = _movement_sum_sq_diff(emg)

        movement_high = movement > float(movement_threshold)
        ratio_high = ratio > float(theta_delta_threshold)
        prev_ok = (previous_state or "") in REM_PREV_STATES

        if movement_high:
            state = "WAKE"
        elif ratio_high and prev_ok:
            state = "REM"
        else:
            state = "NREM"

        return {
            "state": state,
            "delta": delta,
            "theta": theta,
            "theta_delta": ratio,
            "movement": movement,
            "emg_rms": movement,  # UI/log alias: now sum-of-squared-diffs
            "eeg_rms": float(np.sqrt(np.mean(np.asarray(eeg, dtype=float) ** 2) + 1e-18)),
            "movement_high": movement_high,
            "ratio_high": ratio_high,
            "prev_ok_for_rem": prev_ok,
        }
