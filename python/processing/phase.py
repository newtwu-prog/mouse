from __future__ import annotations

import numpy as np
from scipy.signal import hilbert


def instantaneous_phase_deg(x: np.ndarray) -> float:
    """Return the last Hilbert phase in degrees, matching RT_Phase_SubVI.vi."""
    if x.size < 8:
        return float("nan")
    analytic = hilbert(x - np.mean(x))
    return float(np.degrees(np.angle(analytic[-1])))
