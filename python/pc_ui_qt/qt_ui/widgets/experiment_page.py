"""Experiment page: compact signal row, three charts, group status."""

from __future__ import annotations

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from processing.groups import GroupSetting, ai_label
from protocol.messages import DataPacket, GroupData
from qt_ui.theme import STATE_COLOR
from qt_ui.waves import WaveStore
from qt_ui.widgets.charts import ExperimentCharts
from qt_ui.widgets.live_params import LiveParamsPanel
from qt_ui.widgets.signal_bar import SignalToggleBar


class GroupCard(QFrame):
    def __init__(self, group: GroupSetting, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("groupCard")
        self.group_name = group.name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        title = QLabel(f"群組 {group.name}")
        title.setStyleSheet("font-weight: 600;")
        self.caption = QLabel("判斷狀態")
        self.caption.setObjectName("muted")
        self.state = QLabel("—")
        self.state.setObjectName("stateBig")
        self.meta = QLabel(
            f"EEG {ai_label(group.eeg_ai)}    EMG {ai_label(group.emg_ai)}\n目標 {group.target_state}"
        )
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        self.live = QLabel("TTL —     Move —     θ/δ —")
        self.live.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.caption)
        layout.addWidget(self.state)
        layout.addWidget(self.meta)
        layout.addWidget(self.live)
        self._apply_color("—")
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def update_data(self, group: GroupData) -> None:
        self.state.setText(group.state or "—")
        self._apply_color(group.state or "—")
        self.live.setText(
            f"TTL {'T' if group.ttl else 'F'}     "
            f"Move {group.movement:.4g}     "
            f"θ/δ {group.theta_delta:.3f}"
        )

    def _apply_color(self, state: str) -> None:
        color = STATE_COLOR.get(state, "#64748b")
        self.state.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: 700;")


class GroupStatusPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusPanel")
        self.setMinimumWidth(280)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)
        heading = QLabel("群組狀態")
        heading.setStyleSheet("font-weight: 600;")
        self._layout.addWidget(heading)
        self._cards: dict[str, GroupCard] = {}
        self._layout.addStretch(1)

    def set_groups(self, groups: list[GroupSetting]) -> None:
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        for index, group in enumerate(groups):
            card = GroupCard(group)
            self._layout.insertWidget(index + 1, card)
            self._cards[group.name] = card

    def highlight(self, name: str) -> None:
        for group_name, card in self._cards.items():
            card.set_selected(group_name == name)

    def apply_packet(self, packet: DataPacket) -> None:
        for group in packet.groups:
            card = self._cards.get(group.name)
            if card is not None:
                card.update_data(group)


class ExperimentPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.waves = WaveStore()
        self.autoscale = False

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(6)
        self.signals = SignalToggleBar()
        self.charts = ExperimentCharts()
        left.addWidget(self.signals, 0)
        left.addWidget(self.charts, 1)
        root.addLayout(left, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.live = LiveParamsPanel()
        self.live.setFixedWidth(280)
        self.status = GroupStatusPanel()
        self.status.setFixedWidth(280)
        side.addWidget(self.live, 0)
        side.addWidget(self.status, 1)
        root.addLayout(side, 0)

        self.signals.visibility_changed.connect(self.redraw)

    def set_groups(self, groups: list[GroupSetting]) -> None:
        self.signals.set_groups([g.name for g in groups])
        self.status.set_groups(groups)
        self.live.set_groups(groups)
        self.waves.reset([g.name for g in groups])
        self.redraw()

    def highlight(self, name: str) -> None:
        self.status.highlight(name)

    def set_span(self, seconds: float) -> None:
        self.waves.set_span(seconds)
        self.redraw()

    def set_autoscale(self, enabled: bool) -> None:
        self.autoscale = bool(enabled)
        self.redraw()

    def reset_waves(self, fs: float) -> None:
        self.waves.reset(list(self.signals.visible_map().keys()), fs=fs)
        self.redraw()

    def append_packet(self, packet: DataPacket) -> None:
        self.waves.append_packet(packet)
        self.status.apply_packet(packet)
        self.redraw()

    def redraw(self) -> None:
        self.charts.redraw(self.waves, self.signals.visible_map(), autoscale=self.autoscale)
