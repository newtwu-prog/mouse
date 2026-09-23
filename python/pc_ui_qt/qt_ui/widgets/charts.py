"""Three stacked scope charts. TTL uses the right axis of the top plot only."""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import QSizePolicy, QSplitter, QVBoxLayout, QWidget

from qt_ui.theme import TRACE_COLOR

_SERIES = {
    "EEG": ("main", "eeg", "EEG"),
    "EMG": ("main", "emg", "EMG"),
    "TTL": ("ttl", "ttl", "TTL"),
    "delta": ("energy", "d", "delta energy"),
    "theta": ("energy", "t", "theta energy"),
}

# Light panel behind the in-plot legend. 140/255 is a soft veil: dark labels
# and solid swatches stay crisp, and the waveform still shows through.
LEGEND_BACKDROP_ALPHA = 140


class SpanAxis(pg.AxisItem):
    """About 5–8 major ticks; decimals follow the visible span."""

    def __init__(self, orientation: str = "left", nbins: int = 6, **kwargs) -> None:
        super().__init__(orientation, **kwargs)
        self._nbins = nbins
        self.enableAutoSIPrefix(False)

    def tickValues(self, minVal, maxVal, size):
        span = abs(float(maxVal) - float(minVal))
        if span <= 0 or not math.isfinite(span):
            return super().tickValues(minVal, maxVal, size)
        raw = span / max(self._nbins, 3)
        if raw <= 0:
            return super().tickValues(minVal, maxVal, size)
        exp = math.floor(math.log10(raw))
        base = 10.0 ** exp
        frac = raw / base
        if frac < 1.5:
            nice = 1.0
        elif frac < 3.0:
            nice = 2.0
        elif frac < 7.0:
            nice = 5.0
        else:
            nice = 10.0
        step = nice * base
        start = math.ceil((float(minVal) - step * 1e-6) / step) * step
        values = []
        value = start
        for _ in range(self._nbins + 6):
            if value > float(maxVal) + step * 1e-6:
                break
            if value >= float(minVal) - step * 1e-6:
                values.append(value)
            value += step
        if len(values) < 2:
            return super().tickValues(minVal, maxVal, size)
        return [(step, values)]

    def tickStrings(self, values, scale, spacing):
        if not values:
            return []
        view = self.linkedView()
        if view is not None:
            low, high = view.viewRange()[1]
            span = abs(high - low)
        elif len(values) > 1:
            span = abs(values[-1] - values[0])
        else:
            span = abs(spacing or 1.0)
        if span < 0.05:
            fmt = "{:.3f}"
        elif span < 1.0:
            fmt = "{:.2f}"
        else:
            fmt = "{:.1f}"
        return [fmt.format(float(v) * scale) for v in values]


class ColorSwatch(pg.GraphicsWidget):
    """Solid color chip for the legend. The panel behind it is translucent."""

    sigClicked = Signal(object)

    def __init__(self, item) -> None:
        super().__init__()
        self.item = item
        self.setFixedWidth(16)
        self.setFixedHeight(14)

    def boundingRect(self):
        return QRectF(0, 0, 16, 14)

    def paint(self, painter, *args):
        pen = self.item.opts.get("pen")
        color = pg.mkColor(pen.color() if pen is not None else "#334155")
        color.setAlpha(255)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pg.mkBrush(color))
        painter.drawRect(QRectF(1, 3, 13, 8))


class ExperimentCharts(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # tk gridspec uses height_ratios [3.2, 1.1, 1.1]. Minimums stay small so a
        # short tab cannot force the top plot to paint over the ones below it.
        self.main = self._plot(nbins=6, min_height=140)
        self.energy = self._plot(nbins=5, min_height=72)
        self.emg = self._plot(nbins=5, min_height=72)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setObjectName("chartSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(14)
        self.splitter.addWidget(self.main)
        self.splitter.addWidget(self.energy)
        self.splitter.addWidget(self.emg)
        self.splitter.setStretchFactor(0, 32)
        self.splitter.setStretchFactor(1, 11)
        self.splitter.setStretchFactor(2, 11)
        layout.addWidget(self.splitter, 1)
        self._ratio_applied = False
        self._applying_ratio = False

        self.main.setLabel("left", "EEG / EMG (V)")
        self.energy.setLabel("left", "energy")
        self.emg.setLabel("left", "EMG (V)")
        self.emg.setLabel("bottom", "t (s)")
        for plot in (self.main, self.energy):
            plot.getAxis("bottom").setStyle(showValues=False)

        self.energy.setXLink(self.main)
        self.emg.setXLink(self.main)
        self._install_ttl_axis()
        self._align_right_axis()

        self._items: dict[str, pg.PlotDataItem] = {}
        self._legend_labels: list[str] = []
        self._legend_key: tuple[str, ...] = ()
        self._legends = {
            "main": self._add_legend(self.main),
            "energy": self._add_legend(self.energy),
            "emg": self._add_legend(self.emg),
        }
        self._limits = {
            "main": (-0.5, 0.5),
            "energy": (0.0, 1.0),
            "emg": (-0.5, 0.5),
        }
        self._locked = {"main": False, "energy": False, "emg": False}
        self._span = 1.0
        self._locking_x = False
        for view in self._x_views():
            view.setDefaultPadding(0.0)
            view.enableAutoRange(x=False, y=False)
            view.sigXRangeChanged.connect(self._guard_x)
        self.set_span(1.0)

    def legend_labels(self) -> list[str]:
        return list(self._legend_labels)

    def point_count(self, group: str, signal: str) -> int:
        item = self._items.get(f"{group}|{signal}")
        if item is None:
            return 0
        _x, y = item.getData()
        return 0 if y is None else int(len(y))

    def set_span(self, seconds: float) -> None:
        span = max(float(seconds), 0.2)
        self._span = span
        self._lock_x(span)

    def _x_views(self):
        return (
            self.main.getViewBox(),
            self.energy.getViewBox(),
            self.emg.getViewBox(),
            self._ttl_vb,
        )

    def _lock_x(self, span: float) -> None:
        """Match tkinter ax.set_xlim(0, Display_s) on every plot, including TTL."""
        if self._locking_x:
            return
        self._locking_x = True
        try:
            for view in self._x_views():
                view.enableAutoRange(x=False)
                view.setLimits(xMin=0.0, xMax=float(span))
                view.setXRange(0.0, float(span), padding=0.0)
        finally:
            self._locking_x = False
        self._sync_ttl_view()

    def _guard_x(self, *_args) -> None:
        if self._locking_x:
            return
        span = self._span
        for view in self._x_views():
            low, high = view.viewRange()[0]
            if abs(low) > 1e-4 or abs(high - span) > 1e-3:
                self._lock_x(span)
                return

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.apply_ratio()
        self._sync_ttl_view()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._applying_ratio:
            return
        self.apply_ratio()

    def apply_ratio(self) -> None:
        """Top trace gets 3.2 parts; energy and EMG get 1.1 each."""
        if self._applying_ratio:
            return
        height = self.splitter.height()
        handles = self.splitter.handleWidth() * 2
        usable = height - handles
        if usable < 160:
            return
        top = int(usable * 32 / 54)
        mid = int(usable * 11 / 54)
        bottom = max(usable - top - mid, 1)
        self._applying_ratio = True
        try:
            self.splitter.setSizes([top, mid, bottom])
        finally:
            self._applying_ratio = False
        self._ratio_applied = True

    def redraw(self, store, visible: dict[str, dict[str, bool]], *, autoscale: bool) -> None:
        fs = max(float(store.fs or 200.0), 1e-6)
        self.set_span(store.span_sec)
        names = [name for name, waves in store.waves.items() if len(waves.get("eeg", [])) or name in visible]
        if not names:
            names = list(visible.keys())
        n_groups = max(len(visible), 1)
        buckets: dict[str, list[np.ndarray]] = {"main": [], "energy": [], "emg": []}
        legend_rows: dict[str, list[tuple[pg.PlotDataItem, str]]] = {
            "main": [],
            "energy": [],
            "emg": [],
        }

        for name in names:
            waves = store.waves.get(name) or {}
            flags = visible.get(name, {})
            eeg = np.asarray(waves.get("eeg", np.zeros(0)), dtype=float)
            t = np.arange(eeg.size) / fs if eeg.size else np.zeros(0)
            for signal, (target, key, base_label) in _SERIES.items():
                shown = bool(flags.get(signal, False))
                values = np.asarray(waves.get(key, np.zeros(0)), dtype=float)
                item = self._curve(name, signal, target)
                if shown and values.size and t.size:
                    count = min(values.size, t.size)
                    item.setData(t[:count], values[:count])
                    item.setVisible(True)
                    if target == "main":
                        buckets["main"].append(values[:count])
                    elif target == "energy":
                        buckets["energy"].append(values[:count])
                elif not shown:
                    item.setVisible(False)
                if shown:
                    label = base_label if n_groups == 1 else f"{name} {base_label}"
                    legend_rows["main" if target == "ttl" else target].append((item, label))
            emg_vals = np.asarray(waves.get("emg", np.zeros(0)), dtype=float)
            emg_item = self._curve(name, "EMG-bottom", "emg")
            if flags.get("EMG"):
                if emg_vals.size and t.size:
                    count = min(emg_vals.size, t.size)
                    emg_item.setData(t[:count], emg_vals[:count])
                    emg_item.setVisible(True)
                    buckets["emg"].append(emg_vals[:count])
                label = "EMG" if n_groups == 1 else f"{name} EMG"
                legend_rows["emg"].append((emg_item, label))
            else:
                emg_item.setVisible(False)

        self._apply_y(buckets, autoscale)
        self._ttl_vb.setYRange(-0.1, 1.2, padding=0)
        self._lock_x(self._span)
        self._refresh_legends(legend_rows)
        self._sync_ttl_view()

    def _plot(self, nbins: int, min_height: int) -> pg.PlotWidget:
        plot = pg.PlotWidget(axisItems={"left": SpanAxis("left", nbins=nbins)})
        plot.setBackground("w")
        plot.showGrid(x=True, y=True, alpha=0.28)
        plot.setMouseEnabled(x=False, y=False)
        plot.setMenuEnabled(False)
        plot.hideButtons()
        plot.getAxis("left").setWidth(62)
        plot.setMinimumHeight(min_height)
        plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        view = plot.getViewBox()
        view.setDefaultPadding(0.0)
        view.enableAutoRange(x=False, y=False)
        return plot

    def _add_legend(self, plot: pg.PlotWidget) -> pg.LegendItem:
        legend = plot.addLegend(
            offset=(-8, 6),
            brush=pg.mkBrush(255, 255, 255, LEGEND_BACKDROP_ALPHA),
            pen=pg.mkPen(148, 163, 184, 120),
            labelTextColor="#0f172a",
            labelTextSize="9pt",
            sampleType=ColorSwatch,
        )
        return legend

    def _install_ttl_axis(self) -> None:
        plot_item = self.main.getPlotItem()
        plot_item.showAxis("right")
        axis = plot_item.getAxis("right")
        axis.setLabel("TTL", color=TRACE_COLOR["TTL"])
        axis.setPen(pg.mkPen(TRACE_COLOR["TTL"]))
        axis.enableAutoSIPrefix(False)
        self._ttl_vb = pg.ViewBox()
        self._ttl_vb.setBackgroundColor(None)
        self._ttl_vb.setMouseEnabled(x=False, y=False)
        self._ttl_vb.setMenuEnabled(False)
        self._ttl_vb.setZValue(10)
        plot_item.scene().addItem(self._ttl_vb)
        axis.linkToView(self._ttl_vb)
        self._ttl_vb.setXLink(plot_item)
        self._ttl_vb.enableAutoRange(x=False, y=False)
        self._ttl_vb.setDefaultPadding(0.0)
        self._ttl_vb.setYRange(-0.1, 1.2, padding=0)

        def update() -> None:
            self._ttl_vb.setGeometry(plot_item.vb.sceneBoundingRect())
            self._ttl_vb.linkedViewChanged(plot_item.vb, self._ttl_vb.XAxis)

        self._sync_ttl_view = update
        plot_item.vb.sigResized.connect(update)

    def _align_right_axis(self) -> None:
        self.main.getAxis("right").setWidth(46)
        for plot in (self.energy, self.emg):
            plot.showAxis("right")
            axis = plot.getAxis("right")
            axis.setStyle(showValues=False)
            axis.setWidth(46)
            axis.setPen(pg.mkPen("#ffffff"))

    def _curve(self, group: str, signal: str, target: str) -> pg.PlotDataItem:
        key = f"{group}|{signal}"
        item = self._items.get(key)
        if item is not None:
            return item
        color_key = "EMG" if signal.startswith("EMG") else signal
        pen = pg.mkPen(TRACE_COLOR.get(color_key, "#334155"), width=1.6 if signal == "TTL" else 1.2)
        opts = {"pen": pen, "antialias": True}
        if signal == "TTL":
            opts["stepMode"] = "left"
        item = pg.PlotDataItem(**opts)
        if target == "ttl":
            self._ttl_vb.addItem(item)
        elif target == "energy":
            self.energy.addItem(item)
        elif target == "emg":
            self.emg.addItem(item)
        else:
            self.main.addItem(item)
        self._items[key] = item
        return item

    def _apply_y(self, buckets: dict[str, list[np.ndarray]], autoscale: bool) -> None:
        plots = {"main": self.main, "energy": self.energy, "emg": self.emg}
        for key, plot in plots.items():
            samples = buckets.get(key) or []
            if samples and (autoscale or not self._locked[key]):
                joined = np.concatenate(samples)
                finite = joined[np.isfinite(joined)]
                if finite.size:
                    lo = float(np.min(finite))
                    hi = float(np.max(finite))
                    if lo == hi:
                        lo -= 0.5
                        hi += 0.5
                    pad = 0.08 * (hi - lo)
                    self._limits[key] = (lo - pad, hi + pad)
                    self._locked[key] = True
            low, high = self._limits[key]
            plot.setYRange(low, high, padding=0)

    def _refresh_legends(self, rows: dict[str, list[tuple[pg.PlotDataItem, str]]]) -> None:
        labels = []
        for key in ("main", "energy", "emg"):
            labels.extend(label for _item, label in rows[key])
        signature = tuple(labels)
        if signature == self._legend_key:
            return
        self._legend_key = signature
        self._legend_labels = labels
        for key, legend in self._legends.items():
            legend.clear()
            for item, label in rows[key]:
                legend.addItem(item, label)
