from __future__ import annotations

import numpy as np

from processing.groups import GroupSetting
from processing.phase import instantaneous_phase_deg
from processing.state import StateScorer


def state_code(name: str) -> int:
    return {"WAKE": 0, "NREM": 1, "REM": 2}.get(name, 0)


def sample_meets_threshold(value: float, threshold_v: float, mode: str) -> bool:
    """Single sample vs threshold (above or below)."""
    if mode == "below":
        return value < threshold_v
    return value > threshold_v


class GroupRuntime:
    """Per-group RT logic: 12 s state gate + sample-rate threshold TTL.

    Condition 1: EEG must meet threshold for ``threshold_n`` consecutive
    samples (oscilloscope-style trigger), then T until the streak breaks.
    Condition 2: sleep state vs PC target; latched until the next 12 s epoch.

    TTL: on rising edge of (cond1 AND cond2), output high for ttl_output_ms,
    then ignore new triggers for ttl_refractory_ms (absolute refractory).
    """

    def __init__(
        self,
        group: GroupSetting,
        fs: float,
        ttl_output_ms: float = 10.0,
        ttl_refractory_ms: float = 0.0,
    ) -> None:
        self.group = group
        self.fs = float(fs)
        self.pulse_n = max(int(round(fs * ttl_output_ms / 1000.0)), 1)
        self.refractory_n = max(int(round(fs * ttl_refractory_ms / 1000.0)), 0)
        self._pulse_left = 0
        self._refrac_left = 0
        self._prev_both = False
        self._streak = 0
        self.history: list[int] = []
        self.previous_state: str | None = None
        self.score: dict = {
            "state": "—",
            "state_match": False,
            "over_threshold": False,
            "ttl": False,
            "delta": 0.0,
            "theta": 0.0,
            "theta_delta": 0.0,
            "emg_rms": 0.0,
            "eeg_rms": 0.0,
            "movement_high": False,
            "ratio_high": False,
            "phase": float("nan"),
        }

    def on_samples(self, eeg: np.ndarray, ttl_enabled: bool) -> list[bool]:
        mode = self.group.threshold_mode
        thr = float(self.group.threshold_v)
        need_n = max(int(getattr(self.group, "threshold_n", 1) or 1), 1)
        state_ok = bool(self.score["state_match"])
        outs: list[bool] = []
        last_ok = False
        for value in eeg.astype(float, copy=False):
            if sample_meets_threshold(float(value), thr, mode):
                self._streak += 1
            else:
                self._streak = 0
            last_ok = self._streak >= need_n
            both = bool(ttl_enabled and state_ok and last_ok)
            rising = both and not self._prev_both
            self._prev_both = both

            if self._pulse_left > 0:
                outs.append(True)
                self._pulse_left -= 1
                if self._pulse_left == 0 and self.refractory_n > 0:
                    self._refrac_left = self.refractory_n
            elif self._refrac_left > 0:
                outs.append(False)
                self._refrac_left -= 1
                if self._refrac_left == 0:
                    # Allow another pulse if conditions are still true.
                    self._prev_both = False
            elif rising:
                self._pulse_left = self.pulse_n - 1
                outs.append(True)
                if self._pulse_left == 0 and self.refractory_n > 0:
                    self._refrac_left = self.refractory_n
            else:
                outs.append(False)

        self.score["over_threshold"] = last_ok
        self.score["ttl"] = outs[-1] if outs else False
        return outs

    def on_epoch(self, eeg: np.ndarray, emg: np.ndarray, scorer: StateScorer) -> dict:
        scored = scorer.score(
            eeg,
            emg,
            self.group.movement_threshold,
            self.group.theta_delta_threshold,
            previous_state=self.previous_state,
        )
        scored["phase"] = instantaneous_phase_deg(eeg)
        scored["state_match"] = scored["state"] == self.group.target_state
        scored["over_threshold"] = self.score["over_threshold"]
        scored["ttl"] = self.score["ttl"]
        self.previous_state = scored["state"]
        self.score = scored
        self.history.append(state_code(scored["state"]))
        self.history = self.history[-60:]
        return scored
