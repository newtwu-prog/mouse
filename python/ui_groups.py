"""Measurement-group editor, matching LabVIEW test2.vi / groupSettingPanel.vi."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from processing.groups import (
    AI_CHANNELS,
    TARGET_STATES,
    THRESHOLD_MODE_LABELS,
    THRESHOLD_MODES,
    TTL_CHANNELS,
    GroupSetting,
    ai_label,
    normalize_threshold_mode,
    parse_ai_label,
)

UI_FONT = ("Microsoft JhengHei UI", 10)
AI_CHOICES = [ai_label(i) for i in range(AI_CHANNELS)]
TTL_CHOICES = [f"CH{i}" for i in range(TTL_CHANNELS)]
MODE_CHOICES = [THRESHOLD_MODE_LABELS[m] for m in THRESHOLD_MODES]
COLUMNS = ("name", "eeg", "emg", "move", "td", "state", "mode", "thr", "n", "ttl")
HEADERS = {
    "name": "群組名稱",
    "eeg": "EEG",
    "emg": "EMG",
    "move": "Movement",
    "td": "θ/δ",
    "state": "判斷狀態",
    "mode": "判斷方式",
    "thr": "判斷閾值",
    "n": "連續判斷點數",
    "ttl": "TTL",
}

def parse_ttl_label(text: str) -> int:
    """Accept CH0 / DIO0 / bare int for backward compatibility."""
    raw = (text or "").strip().upper()
    for prefix in ("CH", "DIO"):
        if raw.startswith(prefix):
            return int(raw[len(prefix) :])
    return int(raw)

def format_ttl_label(dio: int) -> str:
    return f"CH{int(dio)}"



class GroupSettingsPanel(ttk.Frame):
    def __init__(self, master, groups: list[GroupSetting], on_changed) -> None:
        super().__init__(master)
        self.on_changed = on_changed
        self.groups: list[GroupSetting] = list(groups)
        self._index = 0 if self.groups else -1
        self._build()
        self._refresh_table()
        if self.groups:
            self._select_row(0)
        else:
            self._clear_form()

    def _build(self) -> None:
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        table_box = ttk.LabelFrame(body, text="量測群組", padding=6)
        table_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.tree = ttk.Treeview(table_box, columns=COLUMNS, show="headings", height=10, selectmode="browse")
        widths = {
            "name": 80,
            "eeg": 55,
            "emg": 55,
            "move": 80,
            "td": 70,
            "state": 70,
            "mode": 80,
            "thr": 70,
            "n": 55,
            "ttl": 55,
        }
        for key in COLUMNS:
            self.tree.heading(key, text=HEADERS[key])
            self.tree.column(key, width=widths[key], anchor=tk.CENTER)
        scroll = ttk.Scrollbar(table_box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.LabelFrame(body, text="編輯選取的群組", padding=8)
        form.grid(row=0, column=1, sticky="nsew")

        self.name_var = tk.StringVar()
        self.eeg_var = tk.StringVar(value=AI_CHOICES[0])
        self.emg_var = tk.StringVar(value=AI_CHOICES[1])
        self.move_var = tk.StringVar(value="0.05")
        self.td_var = tk.StringVar(value="1.0")
        self.state_var = tk.StringVar(value="NREM")
        self.mode_var = tk.StringVar(value=THRESHOLD_MODE_LABELS["above"])
        self.thr_var = tk.StringVar(value="0.02")
        self.n_var = tk.StringVar(value="4")
        self.ttl_var = tk.StringVar(value=TTL_CHOICES[0])

        rows = [
            ("群組名稱", self.name_var, None),
            ("EEG 通道", self.eeg_var, AI_CHOICES),
            ("EMG 通道", self.emg_var, AI_CHOICES),
            ("Movement 閾值", self.move_var, None),
            ("θ/δ 閾值", self.td_var, None),
            ("判斷狀態", self.state_var, list(TARGET_STATES)),
            ("判斷方式", self.mode_var, MODE_CHOICES),
            ("判斷閾值 (V)", self.thr_var, None),
            ("連續判斷點數", self.n_var, None),
            ("TTL 輸出", self.ttl_var, TTL_CHOICES),
        ]
        for row, (label, var, choices) in enumerate(rows):
            ttk.Label(form, text=label, font=UI_FONT).grid(row=row, column=0, sticky="w", pady=3)
            if choices is None:
                ttk.Entry(form, textvariable=var, width=16).grid(row=row, column=1, sticky="ew", pady=3)
            else:
                ttk.Combobox(
                    form, textvariable=var, values=choices, width=14, state="readonly"
                ).grid(row=row, column=1, sticky="ew", pady=3)
        form.columnconfigure(1, weight=1)
        ttk.Button(form, text="套用到選取列", command=self._apply_form).grid(
            row=len(rows), column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )

        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, padx=8, pady=8)
        self.btn_add = ttk.Button(buttons, text="新增群組", command=self._add)
        self.btn_delete = ttk.Button(buttons, text="刪除群組", command=self._delete)
        self.btn_load = ttk.Button(buttons, text="載入設定")
        self.btn_save = ttk.Button(buttons, text="儲存設定")
        self.btn_confirm = ttk.Button(buttons, text="確認設定")
        for btn in (self.btn_add, self.btn_delete, self.btn_load, self.btn_save, self.btn_confirm):
            btn.pack(side=tk.LEFT, padx=4)

        self.status = tk.StringVar(value="設定尚未確認，無法開始實驗。")
        ttk.Label(self, textvariable=self.status, font=UI_FONT).pack(anchor="w", padx=12, pady=(0, 8))

    def set_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in (self.btn_add, self.btn_delete, self.btn_load, self.btn_save, self.btn_confirm):
            btn.configure(state=state)
        select = "browse" if enabled else "none"
        self.tree.configure(selectmode=select)

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, g in enumerate(self.groups):
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    g.name,
                    ai_label(g.eeg_ai),
                    ai_label(g.emg_ai),
                    f"{g.movement_threshold:g}",
                    f"{g.theta_delta_threshold:g}",
                    g.target_state,
                    THRESHOLD_MODE_LABELS.get(g.threshold_mode, g.threshold_mode),
                    f"{g.threshold_v:g}",
                    str(int(g.threshold_n)),
                    format_ttl_label(g.ttl_dio),
                ),
            )

    def _select_row(self, index: int) -> None:
        if not self.groups:
            self._index = -1
            self._clear_form()
            return
        self._index = max(0, min(index, len(self.groups) - 1))
        self.tree.selection_set(str(self._index))
        self.tree.see(str(self._index))
        self._fill_form(self.groups[self._index])

    def _on_select(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._index = int(selected[0])
        self._fill_form(self.groups[self._index])

    def _fill_form(self, group: GroupSetting) -> None:
        self.name_var.set(group.name)
        self.eeg_var.set(ai_label(group.eeg_ai))
        self.emg_var.set(ai_label(group.emg_ai))
        self.move_var.set(f"{group.movement_threshold:g}")
        self.td_var.set(f"{group.theta_delta_threshold:g}")
        self.state_var.set(group.target_state)
        self.mode_var.set(THRESHOLD_MODE_LABELS.get(group.threshold_mode, THRESHOLD_MODE_LABELS["above"]))
        self.thr_var.set(f"{group.threshold_v:g}")
        self.n_var.set(str(int(group.threshold_n)))
        self.ttl_var.set(format_ttl_label(group.ttl_dio))

    def _clear_form(self) -> None:
        self.name_var.set("")
        self.eeg_var.set(AI_CHOICES[0])
        self.emg_var.set(AI_CHOICES[1] if len(AI_CHOICES) > 1 else AI_CHOICES[0])
        self.move_var.set("0.05")
        self.td_var.set("1.0")
        self.state_var.set("NREM")
        self.mode_var.set(THRESHOLD_MODE_LABELS["above"])
        self.thr_var.set("0.02")
        self.n_var.set("4")
        self.ttl_var.set(TTL_CHOICES[0])

    def form_group(self, fallback_name: str = "") -> GroupSetting:
        name = self.name_var.get().strip() or fallback_name
        ttl = parse_ttl_label(self.ttl_var.get())
        return GroupSetting(
            name=name,
            eeg_ai=parse_ai_label(self.eeg_var.get()),
            emg_ai=parse_ai_label(self.emg_var.get()),
            movement_threshold=float(self.move_var.get()),
            theta_delta_threshold=float(self.td_var.get()),
            target_state=self.state_var.get().upper(),
            ttl_dio=ttl,
            threshold_v=float(self.thr_var.get()),
            threshold_mode=normalize_threshold_mode(self.mode_var.get()),
            threshold_n=max(int(float(self.n_var.get())), 1),
        )

    def _apply_form(self) -> None:
        if self._index < 0 or self._index >= len(self.groups):
            return
        try:
            group = self.form_group(fallback_name=self.groups[self._index].name)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.groups[self._index] = group
        self._refresh_table()
        self._select_row(self._index)
        self.on_changed()

    def _next_defaults(self) -> GroupSetting:
        used_ai = {g.eeg_ai for g in self.groups} | {g.emg_ai for g in self.groups}
        used_dio = {g.ttl_dio for g in self.groups}
        eeg = next((i for i in range(AI_CHANNELS) if i not in used_ai), 0)
        emg = next((i for i in range(AI_CHANNELS) if i not in used_ai and i != eeg), min(eeg + 1, AI_CHANNELS - 1))
        dio = next((i for i in range(TTL_CHANNELS) if i not in used_dio), 0)
        return GroupSetting(
            name=f"group_{len(self.groups) + 1}",
            eeg_ai=eeg,
            emg_ai=emg,
            movement_threshold=0.05,
            theta_delta_threshold=1.0,
            target_state="NREM",
            ttl_dio=dio,
            threshold_v=0.02,
            threshold_mode="above",
            threshold_n=4,
        )

    def _add(self) -> None:
        self.groups.append(self._next_defaults())
        self._refresh_table()
        self._select_row(len(self.groups) - 1)
        self.on_changed()

    def _delete(self) -> None:
        if self._index < 0 or not self.groups:
            return
        del self.groups[self._index]
        self._refresh_table()
        if self.groups:
            self._select_row(min(self._index, len(self.groups) - 1))
        else:
            self._index = -1
            self._clear_form()
        self.on_changed()

    def set_groups(self, groups: list[GroupSetting]) -> None:
        self.groups = list(groups)
        self._refresh_table()
        if self.groups:
            self._select_row(0)
        else:
            self._index = -1
            self._clear_form()
        self.on_changed()
