"""Stereo peak meters with peak-hold, and a latching clip indicator.

Level awareness is most of the reason this app has a Record tab rather than
telling you to use Audacity. The meter has to answer one question at a glance --
*is my input gain right?* -- so: a bar per channel, a peak-hold tick that lingers
so you can see a transient you would otherwise miss, a numeric max in dBFS, and a
clip light that **latches**. A clip light that clears itself is useless: the whole
point is to still be lit when you look up thirty seconds later.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.text_styles import apply_muted
from gui.level_scale import FLOOR_DBFS, GRIDLINES_DBFS, dbfs_fraction

#: Where the bar turns amber -- close enough to full scale to want a look.
WARN_DBFS = -6.0
#: How long a peak-hold tick lingers, in telemetry frames (~50 ms each).
HOLD_FRAMES = 30            # ~1.5 s

#: Bar height. Gain-setting is the highest-stakes judgment the app asks of
#: anyone -- get it wrong and the whole side is wrong, and you find out at the
#: end. So the meter is given real physical presence rather than the 16px hairline
#: it used to be. This is the one place where visual weight is function.
BAR_HEIGHT = 26

#: Where the clip zone starts. Deliberately the *same* number the hint text tells
#: people to keep peaks below, so the red band on the bar and the sentence under
#: it are saying one thing: past here you are out of margin.
CLIP_ZONE_DBFS = -3.0

#: Headroom bands. Below -3 dBFS you are safe; from -3 to -0.5 you are spending
#: the last of your margin; above -0.5 you are, for practical purposes, at the
#: ceiling. These are the numbers the hint text tells the user to aim for, so
#: they are the numbers the colour follows.
SAFE_DBFS = -3.0
DANGER_DBFS = -0.5

_GREEN = QColor("#3aa655")
_AMBER = QColor("#c07000")
_RED = QColor("#c0392b")
_TICK = QColor("#e8e8e8")
#: Landmark rules on the bar, matching the history lanes' gridlines.
_GRID_MARK = QColor(255, 255, 255, 40)
_TARGET_MARK = QColor("#c07000")
#: The clip zone's backing, painted on the empty track so the ceiling is visible
#: before anything reaches it.
_CLIP_ZONE = QColor(192, 57, 43, 60)
#: LED-style segmentation across the fill.
_SEGMENT_PITCH = 6
_SEGMENT_GAP = QColor(0, 0, 0, 45)

_MINUS = "−"           # a real minus sign; a hyphen reads as a dash


def headroom_colour(dbfs: float) -> QColor:
    """Green with room to spare, amber spending the last of it, red at the wall."""
    if dbfs is None or math.isnan(dbfs):
        return _GREEN
    if dbfs < SAFE_DBFS:
        return _GREEN
    if dbfs < DANGER_DBFS:
        return _AMBER
    return _RED


def format_headroom(dbfs: float) -> str:
    """``-4.2`` -> ``max -4.2 dBFS (4.2 dB headroom)``.

    Headroom is the number the gain ritual is actually aiming at, so it is stated
    as itself rather than left as an exercise in mental arithmetic.
    """
    if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
        return "max —"
    margin = max(0.0, -dbfs)
    sign = _MINUS if dbfs < 0 else ""
    return f"max {sign}{abs(dbfs):.1f} dBFS ({margin:.1f} dB headroom)"


def format_channel_peak(dbfs: float) -> str:
    """``-7.04`` -> ``-7.0``. One decimal, a real minus sign, em dash for silence."""
    if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
        return "—"
    sign = _MINUS if dbfs < 0 else ""
    return f"{sign}{abs(dbfs):.1f}"


def clip_zone_fraction() -> float:
    """Where the clip zone begins, as a 0..1 fraction of the bar's width.

    On the shared -60..0 mapping this is 0.95 -- the top 5% of the bar. It looks
    narrow written down and reads correctly on screen, because linear-in-dB puts
    everything above -20 dBFS in the top third anyway.
    """
    return dbfs_fraction(CLIP_ZONE_DBFS)


#: dBFS -> 0..1 along the bar. Shared with the history lanes so a level lands in
#: the same place in both views -- see :mod:`gui.level_scale`.
_fraction = dbfs_fraction


class PeakBar(QWidget):
    """One channel: a filled bar plus a peak-hold tick."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(BAR_HEIGHT)
        self.setMinimumWidth(160)
        self._level = 0.0        # current, 0..1
        self._hold = 0.0         # peak-hold, 0..1
        self._hold_age = 0

    def set_level(self, dbfs: float) -> None:
        self._level = _fraction(dbfs)
        if self._level >= self._hold:
            self._hold = self._level
            self._hold_age = 0
        else:
            self._hold_age += 1
            if self._hold_age > HOLD_FRAMES:
                self._hold = max(self._level, self._hold - 0.02)   # then decay
        self.update()

    def reset(self) -> None:
        self._level = 0.0
        self._hold = 0.0
        self._hold_age = 0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, self.palette().alternateBase())

        width = rect.width()
        height = rect.height()

        # The clip zone is painted on the *track*, not the fill, so it is there
        # to read against even when the bar is nowhere near it. A bar with
        # nothing behind it cannot be judged: -7 dBFS truthfully fills 88% of a
        # -60..0 scale and looks "nearly full" until the eye has a marked ceiling
        # to place it against.
        zone_x = width * clip_zone_fraction()
        painter.fillRect(QRectF(zone_x, 0, width - zone_x, height), _CLIP_ZONE)

        filled = width * self._level
        if filled > 0:
            # Colour by how hot it is, so "too hot" is visible without reading.
            peak_db = FLOOR_DBFS + self._level * (0.0 - FLOOR_DBFS)
            colour = _GREEN if peak_db < WARN_DBFS else (
                _AMBER if peak_db < -0.1 else _RED)
            # A gradient toward the ceiling: the fill darkens into its warning
            # colour as it climbs, so the bar reads hot before it reads full.
            gradient = QLinearGradient(0.0, 0.0, float(width), 0.0)
            gradient.setColorAt(0.0, colour.darker(125))
            gradient.setColorAt(max(0.0, dbfs_fraction(WARN_DBFS) - 0.08), colour)
            gradient.setColorAt(1.0, colour.lighter(115))
            painter.fillRect(QRectF(0, 0, filled, height), gradient)

            # Segmentation: fine vertical breaks across the fill give it the
            # look of a stack of LEDs rather than a progress bar, which is the
            # visual language people already read levels in.
            painter.setPen(Qt.NoPen)
            for x in range(0, int(filled), _SEGMENT_PITCH):
                painter.fillRect(QRectF(x + _SEGMENT_PITCH - 2, 0, 1, height),
                                 _SEGMENT_GAP)

        # The same landmarks the history lanes rule, at the same fractions. A bar
        # with no marks cannot be read: -7 dBFS truthfully fills 88% of a -60..0
        # scale, which looks "nearly full" until you can see it sitting below the
        # -6 and -3 marks. The marks are what make the position legible.
        for db in GRIDLINES_DBFS:
            if db >= 0.0:
                continue                      # 0 dBFS is the bar's own right edge
            gx = width * dbfs_fraction(db)
            painter.fillRect(QRectF(gx, 0, 1, height),
                             _TARGET_MARK if db == SAFE_DBFS else _GRID_MARK)

        if self._hold > 0:
            x = min(width - 2, width * self._hold)
            painter.fillRect(QRectF(x, 0, 2, height), _TICK)

        painter.setPen(self.palette().mid().color())
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()


class MeterScale(QWidget):
    """The numbered dBFS rule under the bars.

    The bars are only readable *against* something. This is that something: the
    same landmarks the history lanes rule, at the same fractions, from the same
    :mod:`gui.level_scale` -- so a level sits at one place across every view in
    the app, and the number under it says which place that is.
    """

    #: Tick + label. Two lines of type is all it needs and all the budget allows.
    HEIGHT = 15

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(self.HEIGHT)
        self.setMinimumWidth(160)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        width = self.rect().width()

        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() - 2.0))
        painter.setFont(font)
        metrics = painter.fontMetrics()

        # The clip zone gets its own mark on the rule, so the red band above has
        # a number attached to it rather than being a vibe.
        painter.fillRect(QRectF(width * clip_zone_fraction(), 0,
                                width * (1.0 - clip_zone_fraction()), 3),
                         _CLIP_ZONE)

        for db in GRIDLINES_DBFS:
            x = width * dbfs_fraction(db)
            colour = _TARGET_MARK if db == CLIP_ZONE_DBFS else self.palette().mid().color()
            painter.fillRect(QRectF(min(x, width - 1), 0, 1, 4), colour)

            label = "0" if db == 0.0 else f"{_MINUS}{abs(int(db))}"
            text_w = metrics.horizontalAdvance(label)
            # 0 dBFS is the bar's right edge: pull its label fully inside rather
            # than letting it clip off the widget.
            left = min(max(0.0, x - text_w / 2.0), float(width - text_w))
            painter.setPen(colour)
            painter.drawText(QRectF(left, 3, text_w, self.HEIGHT - 3),
                             Qt.AlignHCenter | Qt.AlignTop, label)
        painter.end()


class ChannelRow:
    """One channel: label, bar, live dBFS, and that channel's own max-hold.

    The numerics live *beside* the bar rather than under the pair of them,
    because the question being asked is per-channel -- a single shared "max"
    line cannot tell you which side is hot, which is the thing you need to know
    to do anything about it.

    Deliberately **not** a QWidget: its four widgets are placed directly into
    :class:`LevelMeters`' grid so that every bar and the scale beneath them
    share one column. A row that owned its own layout could only be lined up
    with the scale by guessing at the label's laid-out width, which was wrong by
    6px and left the calibrated ticks not quite under the bars they annotate.
    """

    def __init__(self, name: str) -> None:
        self._max_hold = float("-inf")

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("QLabel { font-weight: bold; }")
        self.name_label.setFixedWidth(12)

        self.bar = PeakBar()

        # Monospaced and fixed-width so the numbers do not jitter the layout as
        # they change -- a readout that shifts while you watch it is unreadable.
        self.peak_label = QLabel("—")
        self.peak_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.peak_label.setFixedWidth(46)
        self.peak_label.setStyleSheet("QLabel { font-family: monospace; }")
        self.peak_label.setToolTip("This channel's level right now, in dBFS.")

        self.hold_label = QLabel("—")
        self.hold_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.hold_label.setFixedWidth(52)
        self.hold_label.setStyleSheet(
            "QLabel { font-family: monospace; font-weight: bold; }")
        self.hold_label.setToolTip(
            "The loudest this channel has been since the last reset.")

    def widgets(self):
        """The four widgets, in column order, for the host grid to place."""
        return (self.name_label, self.bar, self.peak_label, self.hold_label)

    def set_level(self, dbfs: float) -> None:
        self.bar.set_level(dbfs)
        self.peak_label.setText(format_channel_peak(dbfs))
        if dbfs is not None and not math.isnan(dbfs) and dbfs > self._max_hold:
            self._max_hold = dbfs
            self.hold_label.setText(format_channel_peak(dbfs))
            self.hold_label.setStyleSheet(
                "QLabel { font-family: monospace; font-weight: bold; "
                f"color: {headroom_colour(dbfs).name()}; }}")

    @property
    def max_hold(self) -> float:
        return self._max_hold

    def reset(self) -> None:
        self._max_hold = float("-inf")
        self.bar.reset()
        self.peak_label.setText("—")
        self.hold_label.setText("—")
        self.hold_label.setStyleSheet(
            "QLabel { font-family: monospace; font-weight: bold; }")


class LevelMeters(QWidget):
    """Stereo bars + numeric max + a latching clip indicator with a count."""

    #: Reset was pressed. The host clears the *source* statistics too -- clearing
    #: only the label would let the next telemetry frame put the old max straight
    #: back, 50 ms later, which is not a reset at all.
    resetRequested = Signal()

    def __init__(self, channels: int = 2) -> None:
        super().__init__()
        self._bars: list[PeakBar] = []
        self._clip_runs = 0

        root = QGridLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setHorizontalSpacing(6)
        root.setVerticalSpacing(2)
        root.setColumnStretch(1, 1)              # the bars take the slack

        self.rows: list[ChannelRow] = []
        for i in range(channels):
            row = ChannelRow("LR"[i] if i < 2 else str(i + 1))
            self.rows.append(row)
            self._bars.append(row.bar)
            for column, widget in enumerate(row.widgets()):
                root.addWidget(widget, i, column)

        # The rule sits in the bars' own column, so it is aligned with them by
        # construction rather than by arithmetic.
        self.scale = MeterScale()
        root.addWidget(self.scale, channels, 1)

        readout = QHBoxLayout()
        # Kept for the overall session max across both channels: the per-channel
        # holds say which side is hot, this says whether anything was.
        self.max_label = QLabel("max —")
        self.max_label.setStyleSheet("QLabel { font-weight: bold; }")
        readout.addWidget(self.max_label)
        readout.addStretch(1)

        # Same loud amber treatment as the no-cover state: this is a problem you
        # want to be told about, not a status you want to look for.
        self.clip_label = QLabel("no clipping")
        apply_muted(self.clip_label)
        readout.addWidget(self.clip_label)

        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Clear the peak hold, max reading and clip latch.")
        self.reset_button.clicked.connect(self._on_reset_clicked)
        readout.addWidget(self.reset_button)
        root.addLayout(readout, channels + 1, 0, 1, 4)

    def _on_reset_clicked(self) -> None:
        self.reset()
        self.resetRequested.emit()

    # -- feed ---------------------------------------------------------------
    def update_from(self, telemetry) -> None:
        for i, row in enumerate(self.rows):
            db = telemetry.peaks_dbfs[i] if i < len(telemetry.peaks_dbfs) else FLOOR_DBFS
            row.set_level(db)

        self.set_max_peak(telemetry.max_peak_dbfs)
        self.set_clip_runs(telemetry.clip_runs)

    def set_max_peak(self, dbfs: float) -> None:
        """The max, and the margin left under full scale, coloured by how much."""
        self.max_label.setText(format_headroom(dbfs))
        if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
            self.max_label.setStyleSheet("QLabel { font-weight: bold; }")
            return
        self.max_label.setStyleSheet(
            f"QLabel {{ color: {headroom_colour(dbfs).name()}; font-weight: bold; }}")

    def set_clip_runs(self, runs: int) -> None:
        """Latch red once clipping has been seen; never clear it on our own."""
        if runs <= self._clip_runs:
            return
        self._clip_runs = runs
        self.clip_label.setText(f"CLIPPING — {runs} run(s)")
        self.clip_label.setStyleSheet(
            "QLabel { color: #c0392b; font-weight: bold; }")

    @property
    def clip_runs(self) -> int:
        return self._clip_runs

    def reset(self) -> None:
        self._clip_runs = 0
        for row in self.rows:
            row.reset()
        self.set_max_peak(float("-inf"))
        self.clip_label.setText("no clipping")
        apply_muted(self.clip_label)
