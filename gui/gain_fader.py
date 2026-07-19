"""The input-gain fader, built as an instrument rather than a slider.

Setting gain is a *closed loop*: you drag the knob, you watch the bars, you drag
again. For as long as those were two widgets at opposite ends of a row, the
gesture spanned the gap between them. So the fader carries the feedback on its
own face.

**What is honest to draw here.** The fader's axis is the Windows capture level,
0..100. The meter's axis is dBFS. They are not the same quantity and cannot be:
which knob position yields -3 dBFS depends entirely on how hot the source is, so
a "-3 dBFS zone" printed at a fixed spot on the gain axis would be a decoration
that lies. What this widget does instead is what a mixing desk does -- it puts
the *meter* alongside the fader, sharing one horizontal span:

* the **track** is the gain axis, and the handle rides it;
* the **level ribbon** underneath is the live input peak, positioned with
  :func:`gui.level_scale.dbfs_fraction` -- the same authority as the bars and
  the history lanes, so a level sits at the same fraction in all three;
* the **-3 dBFS target mark and the clip zone** are drawn on the ribbon, where
  they mean something, not on the track where they would not.

Read as one control it says: drag until the ribbon reaches the mark. That is the
gain ritual, in a single instrument, with nothing on it that isn't true.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QGridLayout, QLabel, QSlider, QWidget

from gui.level_scale import GRIDLINES_DBFS, dbfs_fraction
from gui.meters import (
    CLIP_ZONE_DBFS,
    _CLIP_ZONE,
    _GRID_MARK,
    _MINUS,
    _TARGET_MARK,
    clip_zone_fraction,
    format_channel_peak,
    headroom_colour,
)

#: Height of the level ribbon under the fader. Enough to read as a bar rather
#: than a hairline, small enough that the fader stays the dominant element.
RIBBON_HEIGHT = 14

#: The fader's groove. "Substantial" is the brief: this is the highest-stakes
#: control in the app and it should feel like one under the cursor.
GROOVE_HEIGHT = 14
HANDLE_WIDTH = 18

_FADER_STYLE = f"""
QSlider::groove:horizontal {{
    height: {GROOVE_HEIGHT}px;
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    height: {GROOVE_HEIGHT}px;
    background: palette(mid);
    border: 1px solid palette(mid);
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: {HANDLE_WIDTH}px;
    margin: -5px 0;
    border-radius: 3px;
    background: palette(button);
    border: 2px solid palette(dark);
}}
QSlider::handle:horizontal:hover {{ border-color: palette(highlight); }}
"""


class LevelRibbon(QWidget):
    """The live input peak, drawn on the meter's scale, under the fader.

    Deliberately the *loudest of the channels*: when you are setting gain the
    question is whether anything is too hot, not which side it was.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(RIBBON_HEIGHT)
        self.setMinimumWidth(160)
        self._dbfs = float("-inf")

    def set_level(self, dbfs: float) -> None:
        self._dbfs = dbfs if dbfs is not None else float("-inf")
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        width, height = rect.width(), rect.height()
        painter.fillRect(rect, self.palette().alternateBase())

        # The clip zone, on the track itself, so the ceiling is visible before
        # anything reaches it.
        zone_x = width * clip_zone_fraction()
        painter.fillRect(QRectF(zone_x, 0, width - zone_x, height), _CLIP_ZONE)

        if not math.isinf(self._dbfs) and not math.isnan(self._dbfs):
            filled = width * dbfs_fraction(self._dbfs)
            if filled > 0:
                painter.fillRect(QRectF(0, 0, filled, height),
                                 headroom_colour(self._dbfs))

        # The same landmarks as the bars and the lanes, from the one authority.
        for db in GRIDLINES_DBFS:
            if db >= 0.0:
                continue
            gx = width * dbfs_fraction(db)
            painter.fillRect(QRectF(gx, 0, 1, height),
                             _TARGET_MARK if db == CLIP_ZONE_DBFS else _GRID_MARK)

        painter.setPen(self.palette().mid().color())
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()


class GainFader(QWidget):
    """Label, horizontal fader, live numeric, and the level ribbon beneath."""

    valueChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        # A grid, so the ribbon and the fader share a column and are aligned by
        # construction. Indenting the ribbon by the label's width instead only
        # aligns them when the label happens to be laid out at its size hint,
        # which is not something to rely on across fonts and DPI settings.
        root = QGridLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setHorizontalSpacing(6)
        root.setVerticalSpacing(2)

        self.name_label = QLabel("Input gain")
        self.name_label.setStyleSheet("QLabel { font-weight: bold; }")
        root.addWidget(self.name_label, 0, 0)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setStyleSheet(_FADER_STYLE)
        self.slider.setToolTip(
            "Adjusts the Windows input level for this device. Drag until the "
            "level bar below sits just under the −3 dBFS mark. If the signal "
            "distorts even at low settings, turn the source down instead.")
        self.slider.valueChanged.connect(self._on_slider_moved)
        root.addWidget(self.slider, 0, 1)
        root.setColumnStretch(1, 1)

        # Fixed width and monospaced: a number that changes as you drag must not
        # resize the control it is attached to.
        self.value_label = QLabel("—")
        self.value_label.setFixedWidth(34)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setStyleSheet("QLabel { font-family: monospace; }")
        root.addWidget(self.value_label, 0, 2)

        self.peak_label = QLabel("—")
        self.peak_label.setFixedWidth(52)
        self.peak_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.peak_label.setStyleSheet(
            "QLabel { font-family: monospace; font-weight: bold; }")
        self.peak_label.setToolTip("The loudest channel, right now.")
        root.addWidget(self.peak_label, 0, 3)

        # Directly under the fader, in the same column: "the level" sits beneath
        # the knob that moves it rather than beside it.
        self.ribbon = LevelRibbon()
        root.addWidget(self.ribbon, 1, 1)

    # -- gain axis ----------------------------------------------------------
    def _on_slider_moved(self, value: int) -> None:
        self.value_label.setText(str(int(value)))
        self.valueChanged.emit(int(value))

    def set_value(self, value: int, *, silent: bool = False) -> None:
        """Move the handle. ``silent`` suppresses the outgoing signal, for when
        the value came *from* the endpoint rather than from the user."""
        if silent:
            self.slider.blockSignals(True)
        self.slider.setValue(int(value))
        self.value_label.setText(str(int(value)))
        if silent:
            self.slider.blockSignals(False)

    def value(self) -> int:
        return int(self.slider.value())

    # -- level axis ---------------------------------------------------------
    def set_level(self, dbfs: float) -> None:
        """Feed the ribbon and the numeric from the meters' own telemetry."""
        self.ribbon.set_level(dbfs)
        self.peak_label.setText(format_channel_peak(dbfs))
        if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
            self.peak_label.setStyleSheet(
                "QLabel { font-family: monospace; font-weight: bold; }")
            return
        self.peak_label.setStyleSheet(
            "QLabel { font-family: monospace; font-weight: bold; "
            f"color: {headroom_colour(dbfs).name()}; }}")

    def clear_level(self) -> None:
        self.set_level(float("-inf"))
