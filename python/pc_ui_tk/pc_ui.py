"""PC UI: configure experiment, connect to cRIO RT over TCP, display & record.

Also supports offline TDMS replay (no cRIO / no TCP) via 「載入 TDMS…」.

    py -3.11 python/pc_ui_tk/pc_ui.py
    python/pc_ui_tk/pc_ui.bat

RT agent (on cRIO or local simulate):
    py -3.11 python/rt_target/main.py --simulate
    py -3.11 python/rt_target/main.py --resource RIO0 --bitfile /path/to.lvbitx
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Full, Queue
from tkinter import filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FormatStrFormatter, MaxNLocator

PYTHON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PYTHON))

from config import DEFAULT_DEVICE_KEY, DEVICES
from offline_tdms import OfflineTdmsRunner
from pc_client import RtTcpClient
from processing.energy import BandEnergy
from processing.groups import (
    GroupSetting,
    THRESHOLD_MODE_LABELS,
    THRESHOLD_MODES,
    ai_label,
    group_to_dict,
    load_groups,
    normalize_threshold_mode,
    save_groups,
    validate_groups,
)
from protocol.messages import DEFAULT_PORT, DataPacket
from recorder import SessionRecorder
from tdms_replay import inspect_tdms
from ui_groups import GroupSettingsPanel

ROOT = PYTHON
DEFAULT_SETTINGS = ROOT / "settings" / "default_groups.json"
RECORD_ROOT = ROOT / "recordings"
TESTDATA = ROOT / "testdata"
UI_FONT = ("Microsoft JhengHei UI", 10)
UI_FONT_BIG = ("Microsoft JhengHei UI", 13, "bold")

# Matplotlib needs an explicit CJK-capable font for Chinese UI labels.
import matplotlib as _mpl
_mpl.rcParams["font.sans-serif"] = ["Microsoft JhengHei UI", "Microsoft JhengHei", "SimHei", "DejaVu Sans"]
_mpl.rcParams["axes.unicode_minus"] = False
STATE_COLOR = {"WAKE": "#b45309", "NREM": "#1d4ed8", "REM": "#7e22ce", "—": "#64748b"}


class PcApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("EEG mice  /  PC（線上 RT 或離線 TDMS 測試）")
        self.root.geometry("1380x920")
        self.root.minsize(1180, 780)

        loaded, self.settings = load_groups(DEFAULT_SETTINGS)
        self.groups: list[GroupSetting] = list(loaded)
        self.configured = False
        self.client = RtTcpClient(DEVICES[DEFAULT_DEVICE_KEY].ip, DEFAULT_PORT)
        self.client.on_packet = self._on_packet
        self.client.on_message = self._on_message
        self.offline = OfflineTdmsRunner(on_data=self._on_packet, on_log=self._on_offline_log)
        self.updates: Queue = Queue(maxsize=32)
        self.recorder: SessionRecorder | None = None
        # Per-group filters / wave buffers (group × signal display).
        self._energy_d: dict[str, BandEnergy] = {}
        self._energy_t: dict[str, BandEnergy] = {}
        self._waves: dict[str, dict[str, np.ndarray]] = {}
        self._trace_vars: dict[str, tk.BooleanVar] = {}
        self._wave_n = 200  # default ~1 s at 200 Hz
        self._plot_fs = 200.0
        self._tdms_channel_names: list[str] = []

        self.ip_var = tk.StringVar(value=DEVICES[DEFAULT_DEVICE_KEY].ip)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        _period_us = int(self.settings.get("sample_period_us", 5000))
        self.period_var = tk.StringVar(value=str(max(_period_us // 1000, 1)))  # UI: ms; FPGA gets us
        self.epoch_var = tk.StringVar(value=str(self.settings.get("epoch_sec", 12.0)))
        self.ttl_out_var = tk.StringVar(
            value=str(self.settings.get("ttl_output_ms", self.settings.get("ttl_pulse_ms", 10.0)))
        )
        self.ttl_ref_var = tk.StringVar(value=str(self.settings.get("ttl_refractory_ms", 0.0)))
        self.ttl_var = tk.BooleanVar(value=True)
        self.record_var = tk.BooleanVar(value=True)
        self.record_root_var = tk.StringVar(
            value=str(self.settings.get("record_root", str(RECORD_ROOT)))
        )
        self.plot_group = tk.StringVar(value=self.groups[0].name if self.groups else "")
        self.span_sec_var = tk.StringVar(value="1")
        self.y_autoscale_var = tk.BooleanVar(value=False)
        self._y_limits: dict[str, tuple[float, float]] = {}
        self.status_var = tk.StringVar(value="未連線到 RT")
        self.conn_var = tk.StringVar(value="RT：未連線")
        self.tdms_path_var = tk.StringVar(value="")
        self.tdms_ch_var = tk.StringVar(value="")  # comma-separated optional
        self._last_applied_live = {
            g.name: {
                "movement_threshold": g.movement_threshold,
                "theta_delta_threshold": g.theta_delta_threshold,
                "threshold_mode": g.threshold_mode,
                "threshold_v": g.threshold_v,
            }
            for g in self.groups
        }

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(40, self._poll)


    def _sync_mid_to_top_width(self, _event=None) -> None:
        """Keep mid rows aligned; never narrower than top or content (avoid clipping)."""
        if getattr(self, "_panel_width_syncing", False):
            return
        top = getattr(self, "_top_controls", None)
        mids = getattr(self, "_synced_mids", None)
        if top is None or not mids:
            return
        try:
            top.update_idletasks()
            w = int(top.winfo_width())
        except tk.TclError:
            return
        if w < 20:
            return
        self._panel_width_syncing = True
        try:
            # Measure natural content width so TDMS path is not clipped.
            for mid in mids:
                if mid is None:
                    continue
                mid.grid_propagate(True)
                mid.update_idletasks()
                w = max(w, int(mid.winfo_reqwidth()))
            for mid in mids:
                if mid is None:
                    continue
                mid.configure(width=w)
                mid.grid_propagate(False)
                mid.update_idletasks()
                h = 1
                for child in mid.winfo_children():
                    child.update_idletasks()
                    h = max(h, int(child.winfo_reqheight()))
                mid.configure(height=h + 4)
            # Browse button right edge aligns to TTL frame right; entry fills the rest.
            ttl = getattr(self, "_ttl_panel", None)
            align = getattr(self, "_save_align", None)
            if ttl is not None and align is not None:
                ttl.update_idletasks()
                align.update_idletasks()
                left = int(align.winfo_rootx())
                right = int(ttl.winfo_rootx()) + int(ttl.winfo_width())
                tw = max(right - left, 200)
                btn = getattr(self, "btn_record_browse", None)
                bh = 22
                if btn is not None:
                    btn.update_idletasks()
                    bh = max(bh, int(btn.winfo_reqheight()))
                align.configure(width=tw, height=bh)
        finally:
            self._panel_width_syncing = False


    def _build(self) -> None:
        # Reference width = RT IP ... 停止實驗 (status stays outside on the right).
        # 取樣設定 + 顯示與存檔 together match that same width.
        header = ttk.Frame(self.root, padding=(8, 8, 8, 6))
        header.pack(fill=tk.X)

        ttk.Label(header, textvariable=self.status_var, font=UI_FONT).pack(side=tk.RIGHT, anchor="n")

        ref = ttk.Frame(header)
        ref.pack(side=tk.LEFT, anchor="nw")

        top = ttk.Frame(ref)
        top.pack(anchor="w")
        ttk.Label(top, text="RT IP", font=UI_FONT).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.ip_var, width=16).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="Port", font=UI_FONT).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.port_var, width=6).pack(side=tk.LEFT, padx=4)
        self.btn_conn = ttk.Button(top, text="連線 RT", command=self._connect)
        self.btn_disc = ttk.Button(top, text="斷線", command=self._disconnect)
        self.btn_start = ttk.Button(top, text="開始實驗", command=self._start)
        self.btn_stop = ttk.Button(top, text="停止實驗", command=self._stop)
        for b in (self.btn_conn, self.btn_disc, self.btn_start, self.btn_stop):
            b.pack(side=tk.LEFT, padx=3)

        self._top_controls = top
        mid = ttk.Frame(ref)
        mid.pack(anchor="w", pady=(6, 0))
        # 取樣 / 顯示 content-sized; TTL uses the free space to the right of 顯示與存檔.
        mid.columnconfigure(0, weight=0)
        mid.columnconfigure(1, weight=0)
        mid.columnconfigure(2, weight=1)
        self._mid_panels = mid

        left = ttk.LabelFrame(mid, text="取樣設定", padding=8)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 4))
        ttk.Label(left, text="取樣週期 ms", font=UI_FONT).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(left, textvariable=self.period_var, width=8).grid(
            row=0, column=1, sticky="w", pady=2, padx=(6, 0)
        )
        ttk.Label(left, text="狀態窗 s", font=UI_FONT).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(left, textvariable=self.epoch_var, width=8).grid(
            row=1, column=1, sticky="w", pady=2, padx=(6, 0)
        )

        right = ttk.LabelFrame(mid, text="顯示與存檔", padding=8)
        right.grid(row=0, column=1, sticky="nsw", padx=(4, 4))
        checks = ttk.Frame(right)
        checks.pack(side=tk.LEFT, anchor="nw")
        ttk.Checkbutton(checks, text="啟用 TTL（由 RT 輸出）", variable=self.ttl_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(checks, text="PC 存檔", variable=self.record_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(checks, text="Y軸自動縮放", variable=self.y_autoscale_var).pack(anchor="w", pady=1)
        fields = ttk.Frame(right)
        fields.pack(side=tk.LEFT, anchor="nw", padx=(16, 0))
        ttk.Label(fields, text="Display s", font=UI_FONT).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(fields, textvariable=self.span_sec_var, width=5).grid(
            row=0, column=1, sticky="w", pady=2, padx=(6, 0)
        )
        ttk.Label(fields, text="顯示群組", font=UI_FONT).grid(row=1, column=0, sticky="w", pady=2)
        self.plot_box = ttk.Combobox(
            fields, textvariable=self.plot_group, values=[g.name for g in self.groups], width=10, state="readonly"
        )
        self.plot_box.grid(row=1, column=1, sticky="w", pady=2, padx=(6, 0))
        self.plot_box.bind("<<ComboboxSelected>>", self._on_plot_group_selected)

        ttl = ttk.LabelFrame(mid, text="TTL 時間設定", padding=8)
        ttl.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        # Nested fields so the long help line cannot stretch label→entry gap
        # (match 取樣設定: labels left, entries padx=(6, 0)).
        ttl_fields = ttk.Frame(ttl)
        ttl_fields.pack(anchor="w")
        # sticky=e so shorter labels hug the entries (padx=6), matching 取樣設定 gap.
        ttk.Label(ttl_fields, text="輸出時間 ms", font=UI_FONT).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(ttl_fields, textvariable=self.ttl_out_var, width=8).grid(
            row=0, column=1, sticky="w", pady=2, padx=(6, 0)
        )
        ttk.Label(ttl_fields, text="不應期時間 ms", font=UI_FONT).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(ttl_fields, textvariable=self.ttl_ref_var, width=8).grid(
            row=1, column=1, sticky="w", pady=2, padx=(6, 0)
        )
        ttk.Label(
            ttl,
            text="條件成立時輸出固定寬度；之後進入不應期時段。",
            font=UI_FONT,
        ).pack(anchor="w", pady=(4, 0))

        self._left_panel = left
        self._right_panel = right
        self._ttl_panel = ttl

        # Row 2: TDMS alone, full width; path entry expands so it is not clipped.
        mid2 = ttk.Frame(ref)
        mid2.pack(anchor="w", pady=(6, 0))
        mid2.columnconfigure(0, weight=1)
        offline = ttk.LabelFrame(mid2, text="離線 TDMS 測試（不需連 cRIO）", padding=8)
        offline.grid(row=0, column=0, sticky="nsew")
        offline.columnconfigure(0, weight=1)

        # One parallel row: path uses ~half width; channel sits in the freed space.
        tdms_row = ttk.Frame(offline)
        tdms_row.pack(fill=tk.X, pady=2)
        tdms_row.columnconfigure(0, weight=1, uniform="tdms_halves")
        tdms_row.columnconfigure(1, weight=1, uniform="tdms_halves")

        path_half = ttk.Frame(tdms_row)
        path_half.grid(row=0, column=0, sticky="ew")
        path_half.columnconfigure(1, weight=1)
        ttk.Label(path_half, text="TDMS", font=UI_FONT).grid(row=0, column=0, sticky="w")
        self.tdms_path_entry = ttk.Entry(path_half, textvariable=self.tdms_path_var)
        self.tdms_path_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.btn_tdms_browse = ttk.Button(
            path_half, text="載入 TDMS…", command=self._browse_tdms
        )
        self.btn_tdms_browse.grid(row=0, column=2, sticky="w", padx=(6, 0))

        ch_half = ttk.Frame(tdms_row)
        ch_half.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        ttk.Label(ch_half, text="通道(可選)", font=UI_FONT).pack(side=tk.LEFT)
        self.tdms_ch_entry = ttk.Entry(ch_half, textvariable=self.tdms_ch_var, width=18)
        self.tdms_ch_entry.pack(side=tk.LEFT, padx=(6, 12))
        self.btn_offline_start = ttk.Button(
            ch_half, text="開始離線實驗", command=self._start_offline
        )
        self.btn_offline_stop = ttk.Button(
            ch_half, text="停止離線", command=self._stop_offline
        )
        self.btn_offline_start.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_offline_stop.pack(side=tk.LEFT)

        self._offline_panel = offline
        self._synced_mids = [mid, mid2]
        self._panel_width_syncing = False
        top.bind("<Configure>", self._sync_mid_to_top_width)
        self.root.after_idle(self._sync_mid_to_top_width)

        # Save path in the middle: separates settings above from charts below.
        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X, padx=8, pady=(8, 0))
        save_opts = ttk.Frame(self.root, padding=(8, 10, 8, 10))
        save_opts.pack(fill=tk.X)
        # Block ends at TTL frame right edge; entry fills between label and browse button.
        self._save_align = ttk.Frame(save_opts)
        self._save_align.pack(side=tk.LEFT, anchor="w")
        self._save_align.pack_propagate(False)
        ttk.Label(self._save_align, text="存檔路徑", font=UI_FONT).pack(side=tk.LEFT, padx=(0, 4))
        self.btn_record_browse = ttk.Button(
            self._save_align, text="選擇資料夾…", command=self._browse_record_root
        )
        self.btn_record_browse.pack(side=tk.RIGHT)
        self.save_path_entry = ttk.Entry(self._save_align, textvariable=self.record_root_var)
        self.save_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X, padx=8, pady=(0, 4))

        self.notebook = ttk.Notebook(self.root)

        # Log: default hidden; toggle button stays at the very bottom.
        self._log_bar = ttk.Frame(self.root)
        self._log_bar.pack(fill=tk.X, padx=8, pady=(0, 8), side=tk.BOTTOM)
        self._log_visible = False
        self.btn_log_toggle = ttk.Button(
            self._log_bar, text="顯示 Log", command=self._toggle_log
        )
        self.btn_log_toggle.pack(side=tk.LEFT)
        self._log_frame = ttk.LabelFrame(self.root, text="訊息 / Log", padding=4)
        self.log = tk.Text(self._log_frame, height=5, font=("Consolas", 9))
        self.log.pack(fill=tk.X)

        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._log("Log 預設隱藏，按「顯示 Log」可開啟；斷線原因也會寫入 pc_ui_debug.log")

        setup = ttk.Frame(self.notebook)
        self.notebook.add(setup, text="1. 量測群組設定")
        self.group_panel = GroupSettingsPanel(setup, self.groups, on_changed=self._on_groups_edited)
        self.group_panel.pack(fill=tk.BOTH, expand=True)
        self.group_panel.btn_confirm.configure(
            text="套用設定", command=self._confirm_settings
        )
        self.group_panel.btn_load.configure(command=self._load_settings)
        self.group_panel.btn_save.configure(command=self._save_settings)

        run = ttk.Frame(self.notebook)
        self.notebook.add(run, text="2. 實驗顯示")
        body = ttk.Frame(run)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        plot_col = ttk.Frame(body)
        plot_col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        plot_col.columnconfigure(0, weight=1)
        plot_col.rowconfigure(1, weight=1)
        self._build_plot_toggles(plot_col)

        fig = Figure(figsize=(8, 8), dpi=100)
        gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 1.1, 1.1], hspace=0.28)
        # Large: EEG + EMG (left) + TTL (right twin axis only)
        self.ax_main = fig.add_subplot(gs[0])
        self.ax_main_ttl = self.ax_main.twinx()
        self.ax_rg = fig.add_subplot(gs[1], sharex=self.ax_main)
        self.ax_emg2 = fig.add_subplot(gs[2], sharex=self.ax_main)
        self.ax_main.set_ylabel("EEG / EMG (V)")
        # TTL label only on right twin axis (same side as TTL ticks)
        self.ax_main_ttl.set_ylabel("TTL")
        self.ax_main_ttl.yaxis.set_label_position("right")
        self.ax_main_ttl.yaxis.tick_right()
        self.ax_main_ttl.set_ylim(-0.1, 1.2)
        self.ax_rg.set_ylabel("energy")
        self.ax_emg2.set_ylabel("EMG raw (V)")
        self.ax_emg2.set_xlabel("t (s)")
        for ax in (self.ax_main, self.ax_rg, self.ax_emg2):
            ax.grid(True, alpha=0.3)
        self.ax_eeg = self.ax_main
        self.ax_emg = self.ax_emg2
        self.ax_d = self.ax_rg
        self.ax_t = self.ax_rg
        self.ax_ttl = self.ax_main_ttl
        # twinx is incompatible with tight_layout
        fig.subplots_adjust(left=0.10, right=0.90, top=0.97, bottom=0.08, hspace=0.35)
        self.canvas = FigureCanvasTkAgg(fig, master=plot_col)
        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")

        self.side = ttk.Frame(body)
        self.side.grid(row=0, column=1, sticky="nsew")
        self._build_live_panel(self.side)
        self._status_host = ttk.Frame(self.side)
        self._status_host.pack(fill=tk.BOTH, expand=True)
        self.group_vars: dict = {}
        self._rebuild_status()
        self._sync_live_panel_from_groups()

        self._set_buttons()



    def _on_plot_group_selected(self, _event=None) -> None:
        if hasattr(self, "live_group_var"):
            self.live_group_var.set(self.plot_group.get())
            self._sync_live_panel_from_groups(selected_name=self.plot_group.get())

    def _build_live_panel(self, parent: ttk.Frame) -> None:
        """Compact live-tunable thresholds for tab 2 (waveform view)."""
        box = ttk.LabelFrame(parent, text="即時參數（看波形時調整）", padding=8)
        box.pack(fill=tk.X, pady=(0, 8))
        self.live_group_var = tk.StringVar(value=self.plot_group.get() if hasattr(self, "plot_group") else "")
        self.live_move_var = tk.StringVar(value="0.05")
        self.live_td_var = tk.StringVar(value="1.0")
        self.live_mode_var = tk.StringVar(value=THRESHOLD_MODE_LABELS["above"])
        self.live_thr_var = tk.StringVar(value="0.02")
        mode_choices = [THRESHOLD_MODE_LABELS[m] for m in THRESHOLD_MODES]

        rows = [
            ("群組", self.live_group_var, [g.name for g in self.groups], True),
            ("Movement 閾值", self.live_move_var, None, False),
            ("θ/δ 閾值", self.live_td_var, None, False),
            ("判斷方式", self.live_mode_var, mode_choices, True),
            ("判斷閾值 (V)", self.live_thr_var, None, False),
        ]
        for row, (label, var, choices, readonly) in enumerate(rows):
            ttk.Label(box, text=label, font=UI_FONT).grid(row=row, column=0, sticky="w", pady=2)
            if choices is None:
                ttk.Entry(box, textvariable=var, width=12).grid(
                    row=row, column=1, sticky="w", pady=2, padx=(6, 0)
                )
            else:
                state = "readonly" if readonly else "normal"
                cb = ttk.Combobox(box, textvariable=var, values=choices, width=10, state=state)
                cb.grid(row=row, column=1, sticky="w", pady=2, padx=(6, 0))
                if label == "群組":
                    self.live_group_box = cb
                    cb.bind("<<ComboboxSelected>>", self._on_live_group_selected)
        ttk.Button(box, text="即時更新", command=self._live_update_settings).grid(
            row=len(rows), column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )
        self._live_panel = box

    def _on_live_group_selected(self, _event=None) -> None:
        name = self.live_group_var.get()
        if name and name != self.plot_group.get():
            self.plot_group.set(name)
        self._sync_live_panel_from_groups(selected_name=name)

    def _sync_live_panel_from_groups(self, selected_name: str | None = None) -> None:
        if not hasattr(self, "live_group_var"):
            return
        names = [g.name for g in self.groups]
        if hasattr(self, "live_group_box"):
            self.live_group_box.configure(values=names)
        name = selected_name or self.live_group_var.get() or self.plot_group.get()
        if name not in names:
            name = names[0] if names else ""
        self.live_group_var.set(name)
        g = next((x for x in self.groups if x.name == name), None)
        if g is None:
            return
        self.live_move_var.set(f"{g.movement_threshold:g}")
        self.live_td_var.set(f"{g.theta_delta_threshold:g}")
        self.live_mode_var.set(
            THRESHOLD_MODE_LABELS.get(g.threshold_mode, THRESHOLD_MODE_LABELS["above"])
        )
        self.live_thr_var.set(f"{g.threshold_v:g}")

    def _live_update_settings(self) -> None:
        """Apply tab-2 live thresholds to one group; push RT / log parameter_changes."""
        if not self.groups:
            messagebox.showinfo("尚無群組", "請先在第一分頁建立並套用設定。")
            return
        if not self.configured:
            messagebox.showinfo("尚未套用設定", "請先在第一分頁按「套用設定」。")
            return
        name = self.live_group_var.get().strip()
        idx = next((i for i, g in enumerate(self.groups) if g.name == name), -1)
        if idx < 0:
            messagebox.showerror("群組不存在", f"找不到群組：{name}")
            return
        old_live = {n: v.copy() for n, v in self._last_applied_live.items()}
        try:
            move = float(self.live_move_var.get())
            td = float(self.live_td_var.get())
            thr = float(self.live_thr_var.get())
            mode = normalize_threshold_mode(self.live_mode_var.get())
        except ValueError as exc:
            messagebox.showerror("參數錯誤", str(exc))
            return
        g = self.groups[idx]
        updated = GroupSetting(
            name=g.name,
            eeg_ai=g.eeg_ai,
            emg_ai=g.emg_ai,
            movement_threshold=move,
            theta_delta_threshold=td,
            target_state=g.target_state,
            ttl_dio=g.ttl_dio,
            threshold_v=thr,
            threshold_mode=mode,
            threshold_n=g.threshold_n,
        )
        self.groups[idx] = updated
        # Keep tab-1 panel in sync without clearing configured.
        self.group_panel.groups = list(self.groups)
        self.group_panel._refresh_table()
        if self.group_panel._index == idx:
            self.group_panel._fill_form(updated)
        elif 0 <= self.group_panel._index < len(self.group_panel.groups):
            pass
        self.plot_group.set(name)
        self._rebuild_status()
        extra = {
            "sample_period_us": self._period_us(),
            "epoch_sec": float(self.epoch_var.get() or 12),
            "ttl_enabled": bool(self.ttl_var.get()),
            "ttl_output_ms": float(self.ttl_out_var.get() or 10),
            "ttl_refractory_ms": float(self.ttl_ref_var.get() or 0),
            "record_root": self.record_root_var.get().strip(),
        }
        self.settings.update(extra)
        save_groups(DEFAULT_SETTINGS, self.groups, extra)
        if self.client.connected:
            self._push_config()
            self._log(f"即時更新已傳送至 RT（{name}）；從下一批資料開始生效")
        if self.offline.running:
            self.offline.update_groups(self.groups)
            self._log(f"離線即時參數已套用（{name}）；從下一批資料開始生效")
        if not self.client.connected and not self.offline.running:
            self._log(f"已更新即時參數（{name}）；連線或離線實驗開始後會生效")
        new_live = self._live_param_snapshot()
        if self.recorder is not None:
            self.recorder.append_parameter_changes(
                old_live,
                new_live,
                source="live_apply",
            )
        self._last_applied_live = {n: v.copy() for n, v in new_live.items()}
        self._sync_live_panel_from_groups(selected_name=name)
        self.group_panel.status.set(f"已即時更新群組 {name} 的閾值參數。")

    def _toggle_log(self) -> None:
        if self._log_visible:
            self._log_frame.pack_forget()
            self.btn_log_toggle.configure(text="顯示 Log")
            self._log_visible = False
        else:
            # Pack above the bottom toggle bar so the button stays visible.
            self._log_frame.pack(
                fill=tk.X, padx=8, pady=(0, 4), side=tk.BOTTOM, before=self._log_bar
            )
            self.btn_log_toggle.configure(text="隱藏 Log")
            self._log_visible = True

    def _log(self, text: str) -> None:
        line = text.rstrip() + "\n"
        self.log.insert(tk.END, line)
        self.log.see(tk.END)
        try:
            from datetime import datetime
            with open(ROOT / "pc_ui_debug.log", "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%H:%M:%S ") + line)
        except Exception:
            pass

    def _rebuild_status(self) -> None:
        host = getattr(self, "_status_host", self.side)
        for child in list(host.winfo_children()):
            child.destroy()
        self.group_vars = {}
        for g in self.groups:
            box = ttk.Frame(host)
            box.pack(fill=tk.X, pady=6)
            state_var = tk.StringVar(value="—")
            detail_var = tk.StringVar(
                value=(
                    f"EEG {ai_label(g.eeg_ai)} EMG {ai_label(g.emg_ai)}  "
                    f"目標 {g.target_state}  Move {g.movement_threshold:g}  θ/δ {g.theta_delta_threshold:g}"
                )
            )
            ttk.Label(box, text=f"群組 {g.name}", font=UI_FONT).pack(anchor="w")
            lbl = ttk.Label(box, textvariable=state_var, font=UI_FONT_BIG, foreground="#64748b")
            lbl.pack(anchor="w")
            ttk.Label(box, textvariable=detail_var, font=UI_FONT).pack(anchor="w")
            self.group_vars[g.name] = {"state": state_var, "detail": detail_var, "label": lbl}

    def _set_buttons(self) -> None:
        connected = self.client.connected
        offline = self.offline.running
        busy = connected or offline
        self.btn_conn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_disc.configure(state=tk.NORMAL if connected else tk.DISABLED)
        self.btn_start.configure(state=tk.NORMAL if connected and self.configured else tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL if connected else tk.DISABLED)
        self.btn_tdms_browse.configure(state=tk.DISABLED if offline else tk.NORMAL)
        self.btn_record_browse.configure(
            state=tk.DISABLED if self.recorder is not None else tk.NORMAL
        )
        self.btn_offline_start.configure(
            state=tk.NORMAL if self.configured and not busy else tk.DISABLED
        )
        self.btn_offline_stop.configure(state=tk.NORMAL if offline else tk.DISABLED)

    def _on_groups_edited(self) -> None:
        self.configured = False
        self.groups = list(self.group_panel.groups)
        self.group_panel.status.set("設定已變更，請再按「套用設定」。")
        self._set_buttons()

    def _confirm_settings(self) -> None:
        old_live = {name: values.copy() for name, values in self._last_applied_live.items()}
        try:
            if self.group_panel._index >= 0 and self.group_panel.groups:
                self.group_panel._apply_form()
        except Exception:
            pass
        groups = list(self.group_panel.groups)
        errors = validate_groups(groups)
        if errors:
            messagebox.showerror("群組設定不完整", "\n".join(errors))
            return
        self.groups = groups
        self.plot_box.configure(values=[g.name for g in groups])
        if self.plot_group.get() not in {g.name for g in groups}:
            self.plot_group.set(groups[0].name)
        self._rebuild_status()
        extra = {
            "sample_period_us": self._period_us(),
            "epoch_sec": float(self.epoch_var.get() or 12),
            "ttl_enabled": bool(self.ttl_var.get()),
            "ttl_output_ms": float(self.ttl_out_var.get() or 10),
            "ttl_refractory_ms": float(self.ttl_ref_var.get() or 0),
            "record_root": self.record_root_var.get().strip(),
        }
        self.settings.update(extra)
        save_groups(DEFAULT_SETTINGS, self.groups, extra)
        self.configured = True
        self.group_panel.status.set(
            f"已確認 {len(groups)} 個群組。可連線 RT，或用下方「開始離線測試」。"
        )
        self._log("已確認量測群組")
        self._set_buttons()
        if self.client.connected:
            self._push_config()
            self._log("即時參數已傳送至 RT；從下一批資料開始生效")
        if self.offline.running:
            self.offline.update_groups(self.groups)
            self._log("離線即時參數已套用；從下一批資料開始生效")
        new_live = self._live_param_snapshot()
        if self.recorder is not None:
            self.recorder.append_parameter_changes(
                old_live,
                new_live,
                source="live_apply",
            )
        self._last_applied_live = {name: values.copy() for name, values in new_live.items()}
        self._sync_live_panel_from_groups()
        self._rebuild_plot_toggles()

    def _period_ms(self) -> float:
        try:
            return max(float(self.period_var.get() or 5), 0.001)
        except ValueError:
            return 5.0

    def _period_us(self) -> int:
        # UI shows ms; FPGA / RT still use microseconds.
        return int(round(self._period_ms() * 1000.0))

    def _settings_dict(self) -> dict:
        return {
            "sample_period_us": self._period_us(),
            "epoch_sec": float(self.epoch_var.get() or 12),
            "ttl_enabled": bool(self.ttl_var.get()),
            "ttl_output_ms": float(self.ttl_out_var.get() or 10),
            "ttl_refractory_ms": float(self.ttl_ref_var.get() or 0),
            "record_root": self.record_root_var.get().strip(),
            "groups": [group_to_dict(g) for g in self.groups],
        }

    def _push_config(self) -> None:
        try:
            self.client.send_config(self._settings_dict())
            self._log("已下發設定到 RT")
        except Exception as exc:
            messagebox.showerror("下發失敗", str(exc))

    def _load_settings(self) -> None:
        path = filedialog.askopenfilename(
            title="載入群組設定",
            filetypes=[("JSON", "*.json")],
            initialdir=DEFAULT_SETTINGS.parent,
        )
        if not path:
            return
        groups, raw = load_groups(Path(path))
        self.settings.update(raw)
        if "sample_period_us" in raw:
            self.period_var.set(str(max(int(raw["sample_period_us"]) // 1000, 1)))
        if "epoch_sec" in raw:
            self.epoch_var.set(str(raw["epoch_sec"]))
        if "ttl_output_ms" in raw or "ttl_pulse_ms" in raw:
            self.ttl_out_var.set(str(raw.get("ttl_output_ms", raw.get("ttl_pulse_ms", 10.0))))
        if "ttl_refractory_ms" in raw:
            self.ttl_ref_var.set(str(raw["ttl_refractory_ms"]))
        if "ttl_enabled" in raw:
            self.ttl_var.set(bool(raw["ttl_enabled"]))
        self.group_panel.set_groups(groups)
        self._log(f"已載入 {path}")

    def _save_settings(self) -> None:
        path = filedialog.asksaveasfilename(
            title="儲存群組設定",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialdir=DEFAULT_SETTINGS.parent,
        )
        if not path:
            return
        save_groups(Path(path), list(self.group_panel.groups), self._settings_dict())
        self._log(f"已儲存 {path}")

    def _connect(self) -> None:
        if self.offline.running:
            messagebox.showinfo("離線測試中", "請先停止離線 TDMS 測試，再連線 RT。")
            return
        host = self.ip_var.get().strip()
        port = int(self.port_var.get())
        self.status_var.set("連線中…")

        def work():
            try:
                self.client.host = host
                self.client.port = port
                self.client.connect()
                err = ""
            except Exception as exc:
                err = str(exc)
            self.root.after(0, lambda: self._on_connected(err))

        threading.Thread(target=work, daemon=True).start()

    def _record_root(self) -> Path:
        raw = self.record_root_var.get().strip()
        path = Path(raw) if raw else RECORD_ROOT
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _browse_record_root(self) -> None:
        initial = self.record_root_var.get().strip() or str(RECORD_ROOT)
        path = filedialog.askdirectory(title="選擇存檔資料夾", initialdir=initial)
        if not path:
            return
        self.record_root_var.set(path)
        self.settings["record_root"] = path
        save_groups(DEFAULT_SETTINGS, self.groups, self.settings)
        self._log(f"存檔路徑：{path}")

    def _live_param_snapshot(self) -> dict[str, dict]:
        return {
            g.name: {
                "movement_threshold": g.movement_threshold,
                "theta_delta_threshold": g.theta_delta_threshold,
                "threshold_mode": g.threshold_mode,
                "threshold_v": g.threshold_v,
            }
            for g in self.groups
        }

    def _browse_tdms(self) -> None:
        initial = TESTDATA if TESTDATA.is_dir() else ROOT
        path = filedialog.askopenfilename(
            title="載入 TDMS 檔案",
            filetypes=[("TDMS", "*.tdms"), ("All", "*.*")],
            initialdir=str(initial),
        )
        if not path:
            return
        self.tdms_path_var.set(path)
        try:
            info = inspect_tdms(Path(path))
            names: list[str] = []
            for g in info.get("groups", []):
                for ch in g.get("channels", []):
                    names.append(str(ch.get("name", "")))
            self._tdms_channel_names = [n for n in names if n]
            preview = ", ".join(self._tdms_channel_names[:8])
            more = "…" if len(self._tdms_channel_names) > 8 else ""
            self._log(f"已載入 TDMS: {path}")
            self._log(f"通道 ({len(self._tdms_channel_names)}): {preview}{more}")
            if not self.tdms_ch_var.get().strip() and len(self._tdms_channel_names) >= 2:
                # hint: first two channels often EEG, EMG — user can edit
                self.tdms_ch_var.set(",".join(self._tdms_channel_names[:2]))
        except Exception as exc:
            messagebox.showerror("讀取 TDMS 失敗", str(exc))
            self._log(f"TDMS 檢查失敗: {exc}")

    def _parse_tdms_channels(self) -> list[str] | None:
        raw = self.tdms_ch_var.get().strip()
        if not raw:
            return None
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _start_offline(self) -> None:
        if self.client.connected:
            messagebox.showinfo("已連線 RT", "請先斷線 RT，再做離線 TDMS 測試。")
            return
        if not self.configured:
            messagebox.showinfo("尚未確認", "請先確認量測群組設定。")
            self.notebook.select(0)
            return
        path = Path(self.tdms_path_var.get().strip())
        if not path.is_file():
            messagebox.showerror("缺少檔案", "請先按「載入 TDMS…」選擇檔案。")
            return
        try:
            speed = 1.0  # fixed realtime; UI speed control removed
            epoch_sec = float(self.epoch_var.get() or 12)
            ttl_out = float(self.ttl_out_var.get() or 10)
            ttl_ref = float(self.ttl_ref_var.get() or 0)
        except ValueError as exc:
            messagebox.showerror("參數錯誤", str(exc))
            return

        # Reset plot buffers; fs will come from TDMS packet
        self._reset_wave_buffers()
        if self.record_var.get():
            record_root = self._record_root()
            self.recorder = SessionRecorder(record_root)
            meta = self._settings_dict()
            meta["mode"] = "offline_tdms"
            meta["tdms_path"] = str(path)
            meta["tdms_speed"] = speed
            self.recorder.write_meta(meta)
            self._log(f"開始存檔  {self.recorder.dir}")
            self._set_buttons()

        self.offline.start(
            path,
            self.groups,
            ttl_enabled=bool(self.ttl_var.get()),
            ttl_output_ms=ttl_out,
            ttl_refractory_ms=ttl_ref,
            epoch_sec=epoch_sec,
            speed=speed,
            loop=True,
            channel_names=self._parse_tdms_channels(),
        )
        self.conn_var.set("模式：離線 TDMS")
        self.status_var.set("離線測試進行中")
        self.notebook.select(1)
        self._set_buttons()
        self._log(f"離線 TDMS 開始  {path.name}")

    def _stop_offline(self) -> None:
        if self.offline.running:
            self.offline.stop()
        self._stop_recording()
        self.conn_var.set("RT：未連線")
        self.status_var.set("離線測試已停止")
        self._set_buttons()
        self._log("離線測試已停止")

    def _on_offline_log(self, text: str) -> None:
        self.root.after(0, lambda: self._log(text))
        if "已停止" in text or "結束" in text or "失敗" in text:
            self.root.after(0, self._set_buttons)

    def _on_connected(self, err: str) -> None:
        if err:
            self.status_var.set("連線失敗")
            messagebox.showerror("連線 RT 失敗", err)
            self._set_buttons()
            return
        self.conn_var.set(f"RT：已連線 {self.client.host}:{self.client.port}")
        self.status_var.set("已連線")
        self._log(f"已連線 {self.client.host}:{self.client.port}")
        self._set_buttons()
        if self.configured:
            self._push_config()

    def _disconnect(self) -> None:
        self._stop_recording()
        self.client.close()
        self.conn_var.set("RT：未連線")
        self.status_var.set("已斷線")
        self._set_buttons()

    def _start(self) -> None:
        if not self.configured:
            messagebox.showinfo("尚未確認", "請先確認量測群組設定。")
            self.notebook.select(0)
            return
        self._push_config()
        fs = 1_000_000.0 / float(self._period_us())
        self._plot_fs = fs
        self._reset_wave_buffers(fs=fs)
        self._refresh_wave_n(fs)
        if self.record_var.get():
            record_root = self._record_root()
            self.recorder = SessionRecorder(record_root)
            self.recorder.write_meta(self._settings_dict())
            self._log(f"開始存檔  {self.recorder.dir}")
            self._set_buttons()
        try:
            self.client.send_start()
        except Exception as exc:
            messagebox.showerror("開始失敗", str(exc))
            return
        self.status_var.set("實驗進行中（判斷在 RT）")
        self.notebook.select(1)
        self._log("START 已送到 RT")

    def _stop(self) -> None:
        try:
            if self.client.connected:
                self.client.send_stop()
        except Exception:
            pass
        if self.offline.running:
            self.offline.stop()
        self._stop_recording()
        self.status_var.set("已停止")
        self._set_buttons()
        self._log("STOP")

    def _stop_recording(self) -> None:
        if self.recorder is not None:
            path = self.recorder.dir
            self.recorder.close()
            self.recorder = None
            self._log(f"存檔結束  {path}")
            self._set_buttons()

    def _on_packet(self, packet: DataPacket) -> None:
        try:
            self.updates.put_nowait(("data", packet))
        except Full:
            try:
                self.updates.get_nowait()
            except Empty:
                pass
            try:
                self.updates.put_nowait(("data", packet))
            except Full:
                pass

    def _on_message(self, msg: dict) -> None:
        try:
            self.updates.put_nowait(("msg", msg))
        except Full:
            pass

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.updates.get_nowait()
                if kind == "msg":
                    self._handle_msg(payload)
                else:
                    self._handle_data(payload)
        except Empty:
            pass
        if not self.client.connected and "已連線" in self.conn_var.get():
            self.conn_var.set("RT：連線中斷")
            self.status_var.set("連線中斷")
            self._log("偵測到 TCP 連線中斷（詳見上一則 ERROR）")
            self._set_buttons()
        self.root.after(40, self._poll)

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "LOG":
            self._log(str(msg.get("message", "")))
        elif t == "ERROR":
            self._log("ERROR: " + str(msg.get("error", msg)))
        elif t == "STATUS":
            self._log(f"STATUS: {msg}")
        elif t == "HELLO_ACK":
            self._log(f"RT hello ok  simulate={msg.get('simulate')}")


    def _display_span_sec(self) -> float:
        try:
            sec = float(self.span_sec_var.get() or 1.0)
        except ValueError:
            sec = 1.0
        return max(sec, 0.2)

    def _refresh_wave_n(self, fs: float | None = None) -> None:
        rate = float(fs if fs is not None else self._plot_fs or 200.0)
        self._plot_fs = rate
        self._wave_n = max(int(rate * self._display_span_sec()), 40)

    def _fine_yticks(self, ax, *, nbins: int = 6) -> None:
        """Sparse major ticks (約 5～8) with span-based decimal places."""
        lo, hi = ax.get_ylim()
        span = abs(float(hi) - float(lo))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=nbins, min_n_ticks=3))
        if span < 0.05:
            fmt = "%.3f"
        elif span < 1.0:
            fmt = "%.2f"
        else:
            fmt = "%.1f"
        ax.yaxis.set_major_formatter(FormatStrFormatter(fmt))

    def _apply_axis_scales(self) -> None:
        axes = {
            "main": (self.ax_main, 6),
            "rg": (self.ax_rg, 5),
            "emg2": (self.ax_emg2, 5),  # bottom EMG: own sparser ticks
        }
        span = self._display_span_sec()
        for ax, _nbins in axes.values():
            ax.set_xlim(0.0, span)
        self.ax_main_ttl.set_xlim(0.0, span)
        self.ax_main_ttl.set_ylim(-0.1, 1.2)
        self.ax_main_ttl.set_ylabel("TTL")
        if self.y_autoscale_var.get():
            for key, (ax, nbins) in axes.items():
                ax.relim()
                ax.autoscale(axis="y", tight=False)
                self._y_limits[key] = ax.get_ylim()
                self._fine_yticks(ax, nbins=nbins)
        else:
            for key, (ax, nbins) in axes.items():
                if key in self._y_limits:
                    ax.set_ylim(*self._y_limits[key])
                else:
                    ax.relim()
                    ax.autoscale(axis="y", tight=False)
                    self._y_limits[key] = ax.get_ylim()
                self._fine_yticks(ax, nbins=nbins)


    def _handle_data(self, packet: DataPacket) -> None:
        if self.recorder is not None:
            self.recorder.append_packet(packet)
        name = self.plot_group.get()
        g = next((x for x in packet.groups if x.name == name), packet.groups[0] if packet.groups else None)
        for gd in packet.groups:
            vars_ = self.group_vars.get(gd.name)
            if not vars_:
                continue
            vars_["state"].set(gd.state)
            vars_["label"].configure(foreground=STATE_COLOR.get(gd.state, "#334155"))
            vars_["detail"].set(
                f"C1={'T' if gd.cond1 else 'F'}  C2={'T' if gd.cond2 else 'F'}  "
                f"TTL={'T' if gd.ttl else 'F'}  "
                f"Move={gd.movement:.4g}  θ/δ={gd.theta_delta:.3f}"
            )
        if g is None and not packet.groups:
            return
        new_fs = float(packet.fs) if packet.fs and packet.fs > 0 else float(self._plot_fs or 200.0)
        old_fs = float(self._plot_fs or 0.0)
        if self._energy_d and old_fs > 0 and abs(new_fs - old_fs) / old_fs > 0.01:
            self._energy_d.clear()
            self._energy_t.clear()
        self._plot_fs = new_fs
        self._refresh_wave_n(new_fs)
        for gd in packet.groups:
            self._append_group_waves(gd, new_fs)
        self._redraw_waves()
        npts = 0
        if self._waves:
            any_w = next(iter(self._waves.values()))
            npts = len(any_w.get("eeg", []))
        self.status_var.set(
            f"實驗中  t={packet.elapsed_s:.1f}s  fs={packet.fs:.1f}Hz  "
            f"窗={self._display_span_sec():g}s/{npts}點  seq={packet.seq}"
        )


    def _trace_key(self, group_name: str, signal: str) -> str:
        return f"{group_name}_{signal}"

    def _reset_wave_buffers(self, fs: float | None = None) -> None:
        self._energy_d = {}
        self._energy_t = {}
        self._waves = {}
        rate = float(fs if fs is not None else self._plot_fs or 200.0)
        self._plot_fs = rate
        for g in self.groups:
            self._ensure_group_wave(g.name, rate)

    def _ensure_group_wave(self, name: str, fs: float | None = None) -> None:
        rate = float(fs if fs is not None else self._plot_fs or 200.0)
        if name not in self._energy_d:
            self._energy_d[name] = BandEnergy(rate, 0.5, 4.0)
            self._energy_t[name] = BandEnergy(rate, 4.0, 8.0)
        if name not in self._waves:
            self._waves[name] = {
                "eeg": np.zeros(0),
                "emg": np.zeros(0),
                "d": np.zeros(0),
                "t": np.zeros(0),
                "ttl": np.zeros(0),
            }

    def _append_group_waves(self, gd, fs: float) -> None:
        self._ensure_group_wave(gd.name, fs)
        d_e = self._energy_d[gd.name].process(gd.eeg)
        t_e = self._energy_t[gd.name].process(gd.eeg)
        if gd.ttl_trace is not None and len(gd.ttl_trace) == len(gd.eeg):
            ttl_e = np.asarray(gd.ttl_trace, dtype=float)
        else:
            ttl_e = np.full(len(gd.eeg), 1.0 if gd.ttl else 0.0)
        w = self._waves[gd.name]
        w["eeg"] = np.concatenate([w["eeg"], gd.eeg])[-self._wave_n :]
        w["emg"] = np.concatenate([w["emg"], gd.emg])[-self._wave_n :]
        w["d"] = np.concatenate([w["d"], d_e])[-self._wave_n :]
        w["t"] = np.concatenate([w["t"], t_e])[-self._wave_n :]
        w["ttl"] = np.concatenate([w["ttl"], ttl_e])[-self._wave_n :]

    def _build_plot_toggles(self, parent: ttk.Frame) -> None:
        """Compact one-row signal toggles: group dropdown + checkboxes for that group.

        UI edits the *currently selected* group's visibility only. Drawing still
        honors every group's remembered flags (dropdown ≠ sole visible group).
        """
        self._plot_toggle_parent = parent
        self._trace_vars: dict[str, tk.BooleanVar] = {}
        outer = ttk.LabelFrame(parent, text="顯示訊號", padding=4)
        outer.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        row = ttk.Frame(outer)
        row.pack(fill=tk.X)
        ttk.Label(row, text="群組", font=UI_FONT).pack(side=tk.LEFT, padx=(0, 4))
        self._plot_signal_group_var = tk.StringVar(value="")
        self._plot_signal_group_combo = ttk.Combobox(
            row,
            textvariable=self._plot_signal_group_var,
            state="readonly",
            width=14,
            font=UI_FONT,
        )
        self._plot_signal_group_combo.pack(side=tk.LEFT, padx=(0, 10))
        self._plot_signal_group_combo.bind(
            "<<ComboboxSelected>>", self._on_plot_signal_group_selected
        )
        self._plot_toggle_checks = ttk.Frame(row)
        self._plot_toggle_checks.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._plot_toggle_outer = outer
        self._rebuild_plot_toggles()

    def _signal_specs(self) -> tuple[tuple[str, str], ...]:
        # EMG visibility is shared by large + bottom charts
        return (
            ("EEG", "main"),
            ("EMG", "main"),
            ("TTL", "main"),
            ("delta", "mid"),
            ("theta", "mid"),
        )

    def _rebuild_plot_toggles(self) -> None:
        if not hasattr(self, "_plot_toggle_checks"):
            return
        prev = {k: bool(v.get()) for k, v in self._trace_vars.items()}
        self._trace_vars = {}
        names = [g.name for g in self.groups]
        for gi, g in enumerate(self.groups):
            for sig, _subplot in self._signal_specs():
                key = self._trace_key(g.name, sig)
                default = True if gi == 0 else False
                self._trace_vars[key] = tk.BooleanVar(value=prev.get(key, default))
        combo = getattr(self, "_plot_signal_group_combo", None)
        if combo is not None:
            combo["values"] = names
            cur = self._plot_signal_group_var.get()
            if names:
                if cur not in names:
                    self._plot_signal_group_var.set(names[0])
            else:
                self._plot_signal_group_var.set("")
        self._ensure_min_traces()
        self._refresh_plot_toggle_checkboxes()

    def _on_plot_signal_group_selected(self, _event=None) -> None:
        """Dropdown only switches which group's flags the checkboxes edit."""
        self._refresh_plot_toggle_checkboxes()

    def _refresh_plot_toggle_checkboxes(self) -> None:
        bar = getattr(self, "_plot_toggle_checks", None)
        if bar is None:
            return
        for child in list(bar.winfo_children()):
            child.destroy()
        name = self._plot_signal_group_var.get() if hasattr(self, "_plot_signal_group_var") else ""
        if not name or name not in {g.name for g in self.groups}:
            ttk.Label(bar, text="（尚無群組）", font=UI_FONT).pack(side=tk.LEFT)
            return
        for sig, subplot in self._signal_specs():
            key = self._trace_key(name, sig)
            var = self._trace_vars.get(key)
            if var is None:
                var = tk.BooleanVar(value=True)
                self._trace_vars[key] = var
            sps = ("main", "bot") if sig == "EMG" else (subplot,)
            ttk.Checkbutton(
                bar,
                text=sig,
                variable=var,
                command=lambda v=var, sps=sps: self._on_trace_toggle_multi(v, sps),
            ).pack(side=tk.LEFT, padx=3)

    def _subplot_keys(self, subplot: str) -> list[str]:
        keys = []
        for k in self._trace_vars:
            if subplot == "main" and (k.endswith("_EEG") or k.endswith("_EMG") or k.endswith("_TTL")):
                keys.append(k)
            elif subplot == "mid" and (k.endswith("_delta") or k.endswith("_theta")):
                keys.append(k)
            elif subplot == "bot" and k.endswith("_EMG"):
                keys.append(k)
        return keys

    def _ensure_min_traces(self) -> None:
        for sp in ("main", "mid", "bot"):
            keys = self._subplot_keys(sp)
            if keys and not any(self._trace_vars[k].get() for k in keys):
                self._trace_vars[keys[0]].set(True)

    def _on_trace_toggle(self, var: tk.BooleanVar, subplot: str) -> None:
        self._on_trace_toggle_multi(var, (subplot,))

    def _on_trace_toggle_multi(self, var: tk.BooleanVar, subplots: tuple[str, ...] | list[str]) -> None:
        if not var.get():
            for sp in subplots:
                keys = self._subplot_keys(sp)
                if keys and not any(self._trace_vars[k].get() for k in keys):
                    var.set(True)
                    return
        self._redraw_waves()

    def _trace_on(self, group_name: str, signal: str) -> bool:
        key = self._trace_key(group_name, signal)
        var = self._trace_vars.get(key)
        return bool(var.get()) if var is not None else False

    def _redraw_waves(self) -> None:
        """Redraw using ALL groups' visibility flags (not only the dropdown selection)."""
        if not hasattr(self, "ax_main"):
            return
        if not self._waves or all(len(w.get("eeg", [])) == 0 for w in self._waves.values()):
            return
        self.ax_main.clear()
        self.ax_main_ttl.clear()
        self.ax_rg.clear()
        self.ax_emg2.clear()
        fs = max(float(self._plot_fs or 200.0), 1e-6)

        for name, w in self._waves.items():
            eeg = w.get("eeg", np.zeros(0))
            if len(eeg) == 0:
                continue
            t = np.arange(len(eeg)) / fs
            if self._trace_on(name, "EEG"):
                self.ax_main.plot(t, eeg, color="#2563eb", lw=0.8, label=f"{name}_EEG")
            if self._trace_on(name, "EMG"):
                emg = w.get("emg", np.zeros(0))
                self.ax_main.plot(t, emg, color="#ea580c", lw=0.8, label=f"{name}_EMG")
                self.ax_emg2.plot(t, emg, color="#ea580c", lw=0.8, label=f"{name}_EMG")
            if self._trace_on(name, "TTL"):
                ttl = w.get("ttl", np.zeros(0))
                self.ax_main_ttl.step(
                    t, ttl, where="post", color="#b91c1c", lw=1.4, label=f"{name}_TTL"
                )
            if self._trace_on(name, "delta"):
                self.ax_rg.plot(
                    t, w.get("d", np.zeros(0)), color="#dc2626", lw=0.8,
                    label=f"{name} delta energy",
                )
            if self._trace_on(name, "theta"):
                self.ax_rg.plot(
                    t, w.get("t", np.zeros(0)), color="#16a34a", lw=0.8,
                    label=f"{name} theta energy",
                )

        self.ax_main.set_ylabel("EEG / EMG (V)")
        self.ax_main_ttl.set_ylabel("TTL")
        self.ax_main_ttl.set_ylim(-0.1, 1.2)
        h1, l1 = self.ax_main.get_legend_handles_labels()
        h2, l2 = self.ax_main_ttl.get_legend_handles_labels()
        if h1 or h2:
            self.ax_main.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=7)
        self.ax_rg.set_ylabel("energy")
        if self.ax_rg.get_legend_handles_labels()[0]:
            self.ax_rg.legend(loc="upper right", fontsize=7)
        self.ax_emg2.set_ylabel("EMG (V)")
        self.ax_emg2.set_xlabel("t (s)")
        if self.ax_emg2.get_legend_handles_labels()[0]:
            self.ax_emg2.legend(loc="upper right", fontsize=7)
        for ax in (self.ax_main, self.ax_rg, self.ax_emg2):
            ax.grid(True, alpha=0.3)
        self._apply_axis_scales()
        self.canvas.draw_idle()

    def _on_close(self) -> None:

        self._stop()
        if self.offline.running:
            self.offline.stop()
        self.client.close()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    PcApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
