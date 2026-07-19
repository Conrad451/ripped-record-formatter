"""The one-line status strip: what the app is doing, in plain language.

The log pane was a developer's console living at the bottom of a consumer app.
It was always open, it scrolled machine-voice lines past at speed, and the one
thing a person actually wants from it -- *what is happening right now* -- had to
be reconstructed by reading the last few lines fast enough.

So the console collapses and this takes its place: a single sentence, present on
every tab, saying the current state. The history is not gone -- nothing is
removed from logging, and one click opens the full pane. This is a change of
**presence**, not of content.

One component, used everywhere. Per-tab variants are exactly how an app ends up
with five voices, and the whole point of the pipeline framing is that it is one
experience rather than five tools sharing a window.

The vocabulary is deliberately small and each line answers "what is happening,
to what, and how far in":

    Ready
    Recording Side C — 2:14, peaks −8.1
    Encoding Side A — 3 of 5 tracks
    Analyzing Side B
    Exporting to MP3 — 7 of 12 tracks
    Saved SideC.wav — 19:42

A warning or an error takes the same line and colours it, so a problem cannot
scroll past unseen. It stays until something else replaces it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

#: The resting state. Named because several places need to return to it.
READY = "Ready"

_INFO_STYLE = "QLabel { color: palette(text); }"
_WARN_STYLE = "QLabel { color: #8a5a00; font-weight: bold; }"
_ERROR_STYLE = "QLabel { color: #c0392b; font-weight: bold; }"

#: Severity levels, in the order they escalate.
INFO = "info"
WARN = "warn"
ERROR = "error"

_STYLES = {INFO: _INFO_STYLE, WARN: _WARN_STYLE, ERROR: _ERROR_STYLE}


class StatusStrip(QWidget):
    """A live one-line state display, with a toggle for the full history."""

    #: The user asked to see (or hide) the log pane.
    historyToggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)

        self.message_label = QLabel(READY)
        self.message_label.setStyleSheet(_INFO_STYLE)
        self.message_label.setWordWrap(False)
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.message_label, 1)

        # A link rather than a button: showing the log is an escape hatch, not a
        # step, and it should not compete with the sentence beside it.
        self.history_button = QPushButton("Show details")
        self.history_button.setFlat(True)
        self.history_button.setCheckable(True)
        self.history_button.setCursor(Qt.PointingHandCursor)
        self.history_button.setStyleSheet(
            "QPushButton { border: none; color: palette(link); "
            "text-decoration: underline; padding: 0 4px; }")
        self.history_button.setToolTip(
            "Show the full log. Everything is still recorded either way.")
        self.history_button.toggled.connect(self._on_history_toggled)
        row.addWidget(self.history_button)

    # -- state --------------------------------------------------------------
    def set_status(self, message: str, level: str = INFO) -> None:
        """Say what is happening. ``level`` colours the line."""
        self.message_label.setText(message or READY)
        self.message_label.setStyleSheet(_STYLES.get(level, _INFO_STYLE))

    def set_ready(self) -> None:
        self.set_status(READY, INFO)

    def status(self) -> str:
        return self.message_label.text()

    def level(self) -> str:
        """Which severity the line is currently showing."""
        style = self.message_label.styleSheet()
        for name, sheet in _STYLES.items():
            if sheet == style:
                return name
        return INFO

    # -- history ------------------------------------------------------------
    def set_history_visible(self, visible: bool) -> None:
        """Reflect the log pane's state without re-emitting the signal."""
        self.history_button.blockSignals(True)
        self.history_button.setChecked(visible)
        self.history_button.setText("Hide details" if visible else "Show details")
        self.history_button.blockSignals(False)

    def _on_history_toggled(self, checked: bool) -> None:
        self.history_button.setText("Hide details" if checked else "Show details")
        self.historyToggled.emit(checked)
