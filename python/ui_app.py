"""Desktop UI for FPGA connect, group setup, then experiment.

    py -3.11 python/ui_app.py

Flow matches LabVIEW test2.vi:
  檢查連線 → 開啟 FPGA → 設定量測群組並確認 → 開始實驗
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEFAULT_DEVICE_KEY, DEVICES
from connect_rt import connection_report
from fpga_daq import default_bitfile, inspect_bitfile_text, resolve_device, rio_resource
from processing.groups import (
    GroupSetting,
    ai_label,
    load_groups,
    save_groups,
    validate_groups,
)
from rt_engine import RtConfig, RtEngine, RtUpdate
from ui_groups import GroupSettingsPanel

ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS = ROOT / "settings" / "default_groups.json"
STATE_COLOR = {"WAKE": "#b45309", "NREM": "#1d4ed8", "REM": "#7e22ce"}
UI_FONT = ("Microsoft JhengHei UI", 10)
UI_FONT_BIG = ("Microsoft JhengHei UI", 14, "bold")
UI_FONT_MONO = ("Consolas", 9)


class RtTestApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("EEG measurement for mice  /  Python")
        self.root.geometry("1320x860")
        self.root.minsize(1140, 740)

        self.updates: Queue[RtUpdate] = Queue(maxsize=12)
        self.engine = RtEngine(self.updates)
        loaded, self.settings = load_groups(DEFAULT_SETTINGS)
        self.groups: list[GroupSetting] = list(loaded)
        self.configured = False
        self.phase = "idle"

        self.device_key = tk.StringVar(value=DEFAULT_DEVICE_KEY)
        self.ip_var = tk.StringVar(value=DEVICES[DEFAULT_DEVICE_KEY].ip)
        self.period_var = tk.StringVar(value=str(int(self.settings.get("sample_period_us", 5000))))
        self.epoch_var = tk.StringVar(value=str(self.settings.get("epoch_sec", 12.0)))
        self.ttl_out_var = tk.StringVar(
            value=str(self.settings.get("ttl_output_ms", self.settings.get("ttl_pulse_ms", 10.0)))
        )
        self.ttl_ref_var = tk.StringVar(value=str(self.settings.get("ttl_refractory_ms", 100.0)))
        self.ttl_var = tk.BooleanVar(value=False)
        self.plot_group = tk.StringVar(value=self.groups[0].name if self.groups else "")
        self.status_var = tk.StringVar(value="尚未連線")
        self.fpga_var = tk.StringVar(value="FPGA：未開啟")

        self._build()
        self._set_phase("idle")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._poll)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="裝置", font=UI_FONT).pack(side=tk.LEFT)
        device_box = ttk.Combobox(
            top,
            textvariable=self.device_key,
            values=list(DEVICES),
            width=8,
            state="readonly",
        )
        device_box.pack(side=tk.LEFT, padx=(4, 10))
        device_box.bind("<<ComboboxSelected>>", self._on_device)

        ttk.Label(top, text="IP", font=UI_FONT).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.ip_var, width=16).pack(side=tk.LEFT, padx=(4, 10))

        self.btn_check = ttk.Button(top, text="檢查連線", command=self._check_connection)
        self.btn_open = ttk.Button(top, text="開啟 FPGA", command=self._open_fpga)
        self.btn_start = ttk.Button(top, text="開始實驗", command=self._start)
        self.btn_stop = ttk.Button(top, text="停止實驗", command=self._stop)
        self.btn_close = ttk.Button(top, text="關閉 FPGA", command=self._close_fpga)
        for btn in (self.btn_check, self.btn_open, self.btn_start, self.btn_stop, self.btn_close):
            btn.pack(side=tk.LEFT, padx=3)
        ttk.Label(top, textvariable=self.status_var, font=UI_FONT).pack(side=tk.RIGHT)

        opts = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        opts.pack(fill=tk.X)
        for label, var, width in (
            ("取樣週期 µs", self.period_var, 8),
            ("狀態窗 秒", self.epoch_var, 6),
        ):
            ttk.Label(opts, text=label, font=UI_FONT).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(opts, textvariable=var, width=width).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(opts, text="啟用 TTL 輸出", variable=self.ttl_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(opts, text="顯示群組", font=UI_FONT).pack(side=tk.LEFT)
        self.plot_box = ttk.Combobox(
            opts,
            textvariable=self.plot_group,
            values=[g.name for g in self.groups],
            width=10,
            state="readonly",
        )
        self.plot_box.pack(side=tk.LEFT, padx=4)
        ttk.Button(opts, text="Bitfile 介面", command=self._show_bitfile).pack(side=tk.LEFT, padx=8)
        ttk.Button(opts, text="TTL0", command=lambda: self._pulse(0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(opts, text="TTL1", command=lambda: self._pulse(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(opts, text="TTL2", command=lambda: self._pulse(2)).pack(side=tk.LEFT, padx=2)

        ttl_opts = ttk.LabelFrame(self.root, text="TTL 時間設定", padding=(8, 4))
        ttl_opts.pack(fill=tk.X, padx=8, pady=(0, 6))
        for label, var, width in (
            ("輸出時間 ms", self.ttl_out_var, 8),
            ("絕對不反應 ms", self.ttl_ref_var, 8),
        ):
            ttk.Label(ttl_opts, text=label, font=UI_FONT).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(ttl_opts, textvariable=var, width=width).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(
            ttl_opts,
            text="條件成立時輸出固定寬度；之後進入不反應區間。",
            font=UI_FONT,
        ).pack(side=tk.LEFT, padx=8)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        setup = ttk.Frame(self.notebook)
        run = ttk.Frame(self.notebook)
        self.notebook.add(setup, text="1. 量測群組設定")
        self.notebook.add(run, text="2. 實驗擷取")

        self.group_panel = GroupSettingsPanel(setup, self.groups, on_changed=self._on_groups_edited)
        self.group_panel.pack(fill=tk.BOTH, expand=True)
        self.group_panel.btn_load.configure(command=self._load_settings)
        self.group_panel.btn_save.configure(command=self._save_settings)
        self.group_panel.btn_confirm.configure(command=self._confirm_settings)

        run.columnconfigure(0, weight=3)
        run.columnconfigure(1, weight=2)
        run.rowconfigure(0, weight=3)
        run.rowconfigure(1, weight=2)

        plot_frame = ttk.LabelFrame(run, text="即時波形", padding=4)
        plot_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.fig = Figure(figsize=(7.2, 4.6), dpi=100)
        self.ax_eeg = self.fig.add_subplot(311)
        self.ax_emg = self.fig.add_subplot(312)
        self.ax_state = self.fig.add_subplot(313)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._clear_plot()

        self.side = ttk.LabelFrame(run, text="群組狀態", padding=8)
        self.side.grid(row=0, column=1, sticky="nsew")
        self.group_vars: dict[str, dict] = {}
        self._rebuild_status()
        ttk.Label(self.side, textvariable=self.fpga_var, font=UI_FONT).pack(anchor="w", pady=(12, 0))

        log_frame = ttk.LabelFrame(run, text="紀錄", padding=4)
        log_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        self.log = tk.Text(log_frame, height=9, font=UI_FONT_MONO, wrap=tk.WORD)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._log("請依序：檢查連線 → 開啟 FPGA → 在「量測群組設定」確認設定 → 開始實驗。")
        self._log("若剛用過 LabVIEW，請先停止 RT_main.vi / startup.rtexe，否則 FIFO 會被占用。")

    def _rebuild_status(self) -> None:
        for child in list(self.side.winfo_children()):
            child.destroy()
        self.group_vars = {}
        for g in self.groups:
            box = ttk.Frame(self.side)
            box.pack(fill=tk.X, pady=6)
            state_var = tk.StringVar(value="—")
            detail_var = tk.StringVar(
                value=(
                    f"EEG {ai_label(g.eeg_ai)}  EMG {ai_label(g.emg_ai)}  "
                    f"目標 {g.target_state}  "
                    f"Move {g.movement_threshold:g}  θ/δ {g.theta_delta_threshold:g}  "
                    f"{'超過' if g.threshold_mode == 'above' else '低於'} {g.threshold_v:g} V  "
                    f"DIO{g.ttl_dio}"
                )
            )
            ttk.Label(box, text=f"群組 {g.name}", font=UI_FONT).pack(anchor="w")
            state_lbl = ttk.Label(box, textvariable=state_var, font=UI_FONT_BIG, foreground="#64748b")
            state_lbl.pack(anchor="w")
            ttk.Label(box, textvariable=detail_var, font=UI_FONT).pack(anchor="w")
            self.group_vars[g.name] = {"state": state_var, "detail": detail_var, "label": state_lbl}
        ttk.Label(self.side, textvariable=self.fpga_var, font=UI_FONT).pack(anchor="w", pady=(12, 0))

    def _on_device(self, _event=None) -> None:
        self.ip_var.set(DEVICES[self.device_key.get()].ip)

    def _device(self):
        return resolve_device(self.device_key.get(), self.ip_var.get().strip() or None)

    def _log(self, text: str) -> None:
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        running = phase == "running"
        opened = self.engine.fpga_open and not running
        idle = not self.engine.fpga_open and not running
        self.btn_check.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.btn_open.configure(state=tk.NORMAL if idle else tk.DISABLED)
        start_ok = opened and self.configured
        self.btn_start.configure(state=tk.NORMAL if start_ok else tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.btn_close.configure(state=tk.NORMAL if opened or running else tk.DISABLED)
        self.group_panel.set_enabled(not running)
        if start_ok:
            self.status_var.set("已就緒，可開始實驗")

    def _on_groups_edited(self) -> None:
        self.configured = False
        self.groups = list(self.group_panel.groups)
        self.group_panel.status.set("設定已變更，請再按「確認設定」。")
        if self.phase != "running":
            self._set_phase(self.phase)

    def _confirm_settings(self) -> None:
        try:
            if self.group_panel._index >= 0 and self.group_panel.groups:
                self.group_panel._apply_form()
        except Exception:
            pass
        groups = list(self.group_panel.groups)
        errors = validate_groups(groups)
        if errors:
            self.configured = False
            self.group_panel.status.set(errors[0])
            messagebox.showerror("群組設定不完整", "\n".join(errors))
            self._set_phase(self.phase)
            return
        self.groups = groups
        self.plot_box.configure(values=[g.name for g in self.groups])
        if self.plot_group.get() not in {g.name for g in self.groups}:
            self.plot_group.set(self.groups[0].name)
        self._rebuild_status()
        extra = {
            "sample_period_us": int(float(self.period_var.get() or 5000)),
            "epoch_sec": float(self.epoch_var.get() or 12),
            "ttl_output_ms": float(self.ttl_out_var.get() or 10),
            "ttl_refractory_ms": float(self.ttl_ref_var.get() or 0),
            "ttl_pulse_ms": float(self.ttl_out_var.get() or 10),
        }
        self.settings.update(extra)
        save_groups(DEFAULT_SETTINGS, self.groups, extra)
        self.configured = True
        self.group_panel.status.set(f"已確認 {len(self.groups)} 個量測群組，可以開始實驗。")
        self._log("已確認量測群組：")
        for g in self.groups:
            self._log(
                f"  {g.name}: EEG {ai_label(g.eeg_ai)}  EMG {ai_label(g.emg_ai)}  "
                f"Move={g.movement_threshold:g}  θ/δ閾={g.theta_delta_threshold:g}  "
                f"目標 {g.target_state}  "
                f"{'超過' if g.threshold_mode == 'above' else '低於'}閾值 {g.threshold_v:g} V  "
                f"TTL DIO{g.ttl_dio}"
            )
        self._log(
            f"  TTL 輸出={self.ttl_out_var.get()} ms  不反應={self.ttl_ref_var.get()} ms"
        )
        self.notebook.select(1)
        self._set_phase(self.phase)

    def _load_settings(self) -> None:
        path = filedialog.askopenfilename(
            title="載入群組設定",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialdir=DEFAULT_SETTINGS.parent,
        )
        if not path:
            return
        try:
            groups, raw = load_groups(Path(path))
        except Exception as exc:
            messagebox.showerror("載入失敗", str(exc))
            return
        self.settings.update(raw)
        if "sample_period_us" in raw:
            self.period_var.set(str(int(raw["sample_period_us"])))
        if "epoch_sec" in raw:
            self.epoch_var.set(str(raw["epoch_sec"]))
        if "ttl_output_ms" in raw or "ttl_pulse_ms" in raw:
            self.ttl_out_var.set(str(raw.get("ttl_output_ms", raw.get("ttl_pulse_ms", 10.0))))
        if "ttl_refractory_ms" in raw:
            self.ttl_ref_var.set(str(raw["ttl_refractory_ms"]))
        self.group_panel.set_groups(groups)
        self._log(f"已載入設定  {path}")

    def _save_settings(self) -> None:
        path = filedialog.asksaveasfilename(
            title="儲存群組設定",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialdir=DEFAULT_SETTINGS.parent,
            initialfile="groups.json",
        )
        if not path:
            return
        extra = {
            "sample_period_us": int(float(self.period_var.get() or 5000)),
            "epoch_sec": float(self.epoch_var.get() or 12),
            "ttl_output_ms": float(self.ttl_out_var.get() or 10),
            "ttl_refractory_ms": float(self.ttl_ref_var.get() or 0),
            "ttl_pulse_ms": float(self.ttl_out_var.get() or 10),
        }
        save_groups(Path(path), list(self.group_panel.groups), extra)
        self._log(f"已儲存設定  {path}")

    def _check_connection(self) -> None:
        device = self._device()
        self.status_var.set("檢查連線中…")
        self._log(f"檢查 {device.alias} ({device.ip}) …")

        def work():
            try:
                ok, report = connection_report(device)
            except Exception as exc:
                ok, report = False, f"{type(exc).__name__}: {exc}"
            self.root.after(0, lambda: self._on_check_done(ok, report))

        threading.Thread(target=work, daemon=True).start()

    def _on_check_done(self, ok: bool, report: str) -> None:
        self._log(report)
        self.status_var.set("cRIO 在線" if ok else "連線失敗")
        if not ok:
            messagebox.showwarning("連線失敗", "找不到 cRIO，請檢查網線與 IP。")

    def _open_fpga(self) -> None:
        device = self._device()
        bitfile = default_bitfile()
        self.status_var.set("開啟 FPGA…")
        self._log(f"開啟 {rio_resource(device)}\n  {bitfile}")

        def work():
            try:
                msg = self.engine.open_fpga(device, bitfile)
                err = ""
            except Exception as exc:
                msg, err = "", f"{type(exc).__name__}: {exc}"
            self.root.after(0, lambda: self._on_fpga_opened(msg, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_fpga_opened(self, msg: str, err: str) -> None:
        if err:
            self.status_var.set("FPGA 開啟失敗")
            self._log(err)
            messagebox.showerror(
                "FPGA 開啟失敗",
                err + "\n\n請先停止 LabVIEW RT_main.vi / startup.rtexe。",
            )
            self._set_phase("idle")
            return
        self._log(msg)
        self.fpga_var.set("FPGA：已開啟")
        if self.configured:
            self.status_var.set("FPGA 已開啟，可開始實驗")
        else:
            self.status_var.set("FPGA 已開啟，請先確認量測群組")
            self.notebook.select(0)
        self._set_phase("opened")

    def _start(self) -> None:
        if not self.configured:
            messagebox.showinfo("尚未確認設定", "請先在「量測群組設定」按「確認設定」。")
            self.notebook.select(0)
            return
        try:
            cfg = RtConfig(
                period_us=int(float(self.period_var.get())),
                epoch_sec=float(self.epoch_var.get()),
                ttl_enabled=bool(self.ttl_var.get()),
                ttl_pulse_ms=float(self.ttl_out_var.get() or 10),
                ttl_output_ms=float(self.ttl_out_var.get() or 10),
                ttl_refractory_ms=float(self.ttl_ref_var.get() or 0),
                plot_group=self.plot_group.get(),
            )
        except ValueError:
            messagebox.showerror("參數錯誤", "取樣週期 / 狀態窗 / TTL 時間必須是數字。")
            return
        try:
            self.engine.start(cfg, self.groups)
        except Exception as exc:
            messagebox.showerror("無法開始實驗", str(exc))
            return
        self.status_var.set("實驗進行中")
        self._set_phase("running")
        self.notebook.select(1)
        self._log(
            f"開始實驗  {1_000_000 / cfg.period_us:.1f} Hz  "
            f"狀態窗={cfg.epoch_sec:g}s  TTL={'ON' if cfg.ttl_enabled else 'OFF'}  "
            f"輸出={cfg.ttl_output_ms:g}ms 不反應={cfg.ttl_refractory_ms:g}ms  "
            f"群組={len(self.groups)}"
        )

    def _stop(self) -> None:
        self.engine.stop()
        self.status_var.set("已停止實驗")
        self._set_phase("opened" if self.engine.fpga_open else "idle")

    def _close_fpga(self) -> None:
        self.engine.close_fpga()
        self.fpga_var.set("FPGA：未開啟")
        self.status_var.set("FPGA 已關閉")
        self._set_phase("idle")
        self._log("FPGA session 已關閉")

    def _pulse(self, channel: int) -> None:
        if not self.engine.fpga_open:
            messagebox.showinfo("TTL", "請先開啟 FPGA。")
            return

        def work():
            try:
                self.engine.pulse_dio(channel)
                err = ""
            except Exception as exc:
                err = str(exc)
            self.root.after(0, lambda: self._log(err or f"手動脈衝 TTL DIO{channel}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_bitfile(self) -> None:
        try:
            text = inspect_bitfile_text(default_bitfile())
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
        self._log(text)

    def _clear_plot(self) -> None:
        for ax, ylabel in (
            (self.ax_eeg, "EEG (V)"),
            (self.ax_emg, "EMG (V)"),
            (self.ax_state, "state"),
        ):
            ax.clear()
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3)
        self.ax_state.set_yticks([0, 1, 2])
        self.ax_state.set_yticklabels(["WAKE", "NREM", "REM"])
        self.ax_state.set_xlabel("12 s epoch")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _poll(self) -> None:
        try:
            while True:
                self._apply(self.updates.get_nowait())
        except Empty:
            pass
        self.root.after(50, self._poll)

    def _apply(self, update: RtUpdate) -> None:
        if update.message:
            self._log(update.message)
        if update.error:
            self._log(update.error)
            self.status_var.set("擷取錯誤")
            self._set_phase("opened" if self.engine.fpga_open else "idle")
            messagebox.showerror("擷取錯誤", update.error)
            return
        if update.kind != "data":
            if update.snapshot:
                self.fpga_var.set(self._fmt_snapshot(update.snapshot))
            return

        self.status_var.set(f"實驗中  t={update.elapsed:.1f}s  FIFO leftover={update.remaining}")
        if update.snapshot:
            self.fpga_var.set(self._fmt_snapshot(update.snapshot))

        for name, score in update.scores.items():
            vars_ = self.group_vars.get(name)
            if not vars_:
                continue
            state = score.get("state", "—")
            vars_["state"].set(state)
            vars_["label"].configure(foreground=STATE_COLOR.get(state, "#334155"))
            vars_["detail"].set(
                f"條件1={'T' if score.get('over_threshold') else 'F'}  "
                f"條件2={'T' if score.get('state_match') else 'F'}  "
                f"TTL={'T' if score.get('ttl') else 'F'}  "
                f"θ/δ={score.get('theta_delta', 0):.2f}  "
                f"Move={score.get('movement', score.get('emg_rms', 0)):.4g}"
            )

        if update.eeg is not None and update.t is not None:
            self.ax_eeg.clear()
            self.ax_emg.clear()
            self.ax_eeg.plot(update.t, update.eeg, color="#2563eb", linewidth=0.8)
            self.ax_eeg.set_ylabel("EEG (V)")
            self.ax_eeg.grid(True, alpha=0.3)
            if update.emg is not None:
                self.ax_emg.plot(update.t, update.emg, color="#ea580c", linewidth=0.8)
            self.ax_emg.set_ylabel("EMG (V)")
            self.ax_emg.grid(True, alpha=0.3)
            self.ax_state.clear()
            hist = update.history.get(self.plot_group.get(), [])
            if hist:
                self.ax_state.plot(hist, drawstyle="steps-post", color="#0f766e")
            self.ax_state.set_yticks([0, 1, 2])
            self.ax_state.set_yticklabels(["WAKE", "NREM", "REM"])
            self.ax_state.set_ylabel("state")
            self.ax_state.set_xlabel("12 s epoch")
            self.ax_state.grid(True, alpha=0.3)
            self.fig.tight_layout()
            self.canvas.draw_idle()

    @staticmethod
    def _fmt_snapshot(snap: dict) -> str:
        run = snap.get("Run")
        period = snap.get("Count(uSec)")
        ai0 = snap.get("Mod1/AI0")
        try:
            ai0_txt = f"{float(ai0):.4f} V" if ai0 is not None else "-"
        except (TypeError, ValueError):
            ai0_txt = str(ai0)
        return f"FPGA：Run={run}  Count={period}  AI0={ai0_txt}"

    def _on_close(self) -> None:
        try:
            self.engine.close_fpga()
        except Exception:
            pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    RtTestApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
