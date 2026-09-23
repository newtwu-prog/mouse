"""TTL channel labels for the Qt UI.

Display is CH0, CH1, … End-user strings never use DIO*.
Parsing still accepts DIO* and a bare integer so older settings text keeps working.
Same rules as ui_groups.parse_ttl_label / format_ttl_label (that module imports tkinter).
"""

from __future__ import annotations


def parse_ttl_label(text: str) -> int:
    """Accept CH0 / DIO0 / bare int."""
    raw = (text or "").strip().upper()
    for prefix in ("CH", "DIO"):
        if raw.startswith(prefix):
            return int(raw[len(prefix) :])
    return int(raw)


def format_ttl_label(dio: int) -> str:
    return f"CH{int(dio)}"
