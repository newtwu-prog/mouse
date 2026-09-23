from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

TARGET_STATES = ("WAKE", "NREM", "REM")
THRESHOLD_MODES = ("above", "below")
THRESHOLD_MODE_LABELS = {
    "above": "超過閾值",
    "below": "低於閾值",
}
AI_CHANNELS = 16
TTL_CHANNELS = 3


@dataclass(frozen=True)
class GroupSetting:
    name: str
    eeg_ai: int
    emg_ai: int
    movement_threshold: float
    theta_delta_threshold: float
    target_state: str
    ttl_dio: int
    threshold_v: float
    threshold_mode: str = "above"
    threshold_n: int = 4  # consecutive samples that must meet threshold (scope-like trigger)


def ai_label(ai: int) -> str:
    """LabVIEW Channel_To_ai.vi uses Ch1..Ch16 for AI0..AI15."""
    return f"Ch{ai + 1}"


def parse_ai_label(text: str) -> int:
    raw = str(text).strip().upper().replace("AI", "").replace("CH", "")
    value = int(raw)
    if 1 <= value <= AI_CHANNELS:
        return value - 1
    if 0 <= value < AI_CHANNELS:
        return value
    raise ValueError(f"channel out of range: {text}")


def group_to_dict(group: GroupSetting) -> dict:
    return asdict(group)


def normalize_threshold_mode(value: object) -> str:
    raw = str(value or "above").strip().lower()
    aliases = {
        "above": "above",
        "over": "above",
        "gt": "above",
        "peak": "above",
        "high": "above",
        "超過": "above",
        "超過閾值": "above",
        "below": "below",
        "under": "below",
        "lt": "below",
        "trough": "below",
        "low": "below",
        "低於": "below",
        "低於閾值": "below",
    }
    mode = aliases.get(raw, raw)
    if mode not in THRESHOLD_MODES:
        raise ValueError(f"threshold_mode must be above/below, got: {value}")
    return mode


def _num(item: dict, *keys: str, default: float) -> float:
    for key in keys:
        if key in item and item[key] is not None:
            return float(item[key])
    return float(default)


def groups_from_items(items: list[dict]) -> list[GroupSetting]:
    out: list[GroupSetting] = []
    for item in items:
        # Old Word W / Word R fall back for older JSON files.
        movement = _num(item, "movement_threshold", "w", default=0.05)
        ratio = _num(item, "theta_delta_threshold", "r", default=1.0)
        out.append(
            GroupSetting(
                name=str(item["name"]).strip(),
                eeg_ai=int(item["eeg_ai"]),
                emg_ai=int(item["emg_ai"]),
                movement_threshold=movement,
                theta_delta_threshold=ratio,
                target_state=str(item.get("target_state", "WAKE")).upper(),
                ttl_dio=int(item.get("ttl_dio", 0)),
                threshold_v=float(item.get("threshold_v", 0.02)),
                threshold_mode=normalize_threshold_mode(item.get("threshold_mode", "above")),
                threshold_n=max(int(item.get("threshold_n", 4)), 1),
            )
        )
    return out


def load_groups(path: Path) -> tuple[list[GroupSetting], dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    groups = groups_from_items(raw.get("groups", []))
    return groups, raw


def save_groups(path: Path, groups: list[GroupSetting], extra: dict | None = None) -> None:
    payload = dict(extra or {})
    payload["groups"] = [group_to_dict(g) for g in groups]
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate_groups(groups: list[GroupSetting]) -> list[str]:
    errors: list[str] = []
    if not groups:
        return ["請至少新增一個量測群組。"]

    names = [g.name for g in groups]
    if any(not name for name in names):
        errors.append("群組名稱不可空白。")
    if len(names) != len(set(names)):
        errors.append("群組名稱不可重複。")

    analog: list[tuple[str, int]] = []
    dios: list[int] = []
    for g in groups:
        if g.target_state not in TARGET_STATES:
            errors.append(f"「{g.name}」判斷狀態必須是 WAKE / NREM / REM。")
        if g.eeg_ai == g.emg_ai:
            errors.append(f"「{g.name}」EEG 與 EMG 不可使用同一通道。")
        if not (0 <= g.eeg_ai < AI_CHANNELS and 0 <= g.emg_ai < AI_CHANNELS):
            errors.append(f"「{g.name}」通道必須在 Ch1–Ch{AI_CHANNELS}。")
        if not (0 <= g.ttl_dio < TTL_CHANNELS):
            errors.append(f"「{g.name}」TTL 必須是 DIO0–DIO{TTL_CHANNELS - 1}。")
        try:
            normalize_threshold_mode(g.threshold_mode)
        except ValueError:
            errors.append(f"「{g.name}」閾值判斷方式必須是超過閾值或低於閾值。")
        if not (g.threshold_v == g.threshold_v):
            errors.append(f"「{g.name}」條件1閾值必須是數字。")
        if int(g.threshold_n) < 1:
            errors.append(f"「{g.name}」條件1連續點數必須 ≥ 1。")
        if g.movement_threshold < 0:
            errors.append(f"「{g.name}」Movement 閾值不可為負。")
        if g.theta_delta_threshold < 0:
            errors.append(f"「{g.name}」θ/δ 閾值不可為負。")
        analog.append((g.name, g.eeg_ai))
        analog.append((g.name, g.emg_ai))
        dios.append(g.ttl_dio)

    used: dict[int, str] = {}
    for name, ai in analog:
        prev = used.get(ai)
        if prev:
            errors.append(f"{ai_label(ai)} 同時被「{prev}」與「{name}」使用。")
        else:
            used[ai] = name
    if len(dios) != len(set(dios)):
        errors.append("每個群組的 TTL DIO 必須不同。")
    return errors


def with_updates(group: GroupSetting, **changes) -> GroupSetting:
    return replace(group, **changes)
