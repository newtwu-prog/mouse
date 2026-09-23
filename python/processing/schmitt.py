from __future__ import annotations


class SchmittTrigger:
    """Two-threshold detector, matching RT schmitt trigger.vi."""

    def __init__(self, high: float, low: float | None = None) -> None:
        self.high = high
        self.low = high * 0.5 if low is None else low
        self._armed = True

    def update(self, value: float) -> bool:
        if self._armed and value >= self.high:
            self._armed = False
            return True
        if not self._armed and value <= self.low:
            self._armed = True
        return False

    def feed(self, values) -> tuple[bool, bool]:
        """Process a sample block. Returns (currently_high, any_rising_edge)."""
        rose = False
        for value in values:
            if self.update(float(value)):
                rose = True
        return (not self._armed), rose
