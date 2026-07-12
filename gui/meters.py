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

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: Bottom of the meter scale. Below this there is nothing worth showing.
FLOOR_DBFS = -60.0
#: Where the bar turns amber -- close enough to full scale to want a look.
WARN_DBFS = -6.0
#: How long a peak-hold tick lingers, in telemetry frames (~50 ms each).
HOLD_FRAMES = 30            # ~1.5 s

_GREEN = QColor("#3aa655")
_AMBER = QColor("#c07000")
_RED = QColor("#c0392b")
_TICK = QColor("#e8e8e8")


def _fraction(dbfs: float) -> float:
    """dBFS -> 0..1 along the meter scale."""
    if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
        return 0.0
    if dbfs <= FLOOR_DBFS:
        return 0.0
    if dbfs >= 0.0:
        return 1.0
    return (dbfs - FLOOR_DBFS) / (0.0 - FLOOR_DBFS)


class PeakBar(QWidget):
    """One channel: a filled bar plus a peak-hold tick."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(16)
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
        filled = width * self._level
        if filled > 0:
            # Colour by how hot it is, so "too hot" is visible without reading.
            peak_db = FLOOR_DBFS + self._level * (0.0 - FLOOR_DBFS)
            colour = _GREEN if peak_db < WARN_DBFS else (
                _AMBER if peak_db < -0.1 else _RED)
            painter.fillRect(QRectF(0, 0, filled, height), colour)

        if self._hold > 0:
            x = min(width - 2, width * self._hold)
            painter.fillRect(QRectF(x, 0, 2, height), _TICK)

        painter.setPen(self.palette().mid().color())
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()


class LevelMeters(QWidget):
    """Stereo bars + numeric max + a latching clip indicator with a count."""

    def __init__(self, channels: int = 2) -> None:
        super().__init__()
        self._bars: list[PeakBar] = []
        self._clip_runs = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(3)

        for i in range(channels):
            row = QHBoxLayout()
            row.addWidget(QLabel("LR"[i] if i < 2 else str(i + 1)))
            bar = PeakBar()
            self._bars.append(bar)
            row.addWidget(bar, 1)
            root.addLayout(row)

        readout = QHBoxLayout()
        self.max_label = QLabel("max —")
        readout.addWidget(self.max_label)
        readout.addStretch(1)

        # Same loud amber treatment as the no-cover state: this is a problem you
        # want to be told about, not a status you want to look for.
        self.clip_label = QLabel("no clipping")
        self.clip_label.setStyleSheet("QLabel { color: palette(mid); }")
        readout.addWidget(self.clip_label)

        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Clear the peak hold, max reading and clip latch.")
        self.reset_button.clicked.connect(self.reset)
        readout.addWidget(self.reset_button)
        root.addLayout(readout)

    # -- feed ---------------------------------------------------------------
    def update_from(self, telemetry) -> None:
        for i, bar in enumerate(self._bars):
            db = telemetry.peaks_dbfs[i] if i < len(telemetry.peaks_dbfs) else FLOOR_DBFS
            bar.set_level(db)

        peak = telemetry.max_peak_dbfs
        self.max_label.setText(
            "max —" if peak is None or math.isinf(peak) else f"max {peak:+.1f} dBFS")
        self.set_clip_runs(telemetry.clip_runs)

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
        for bar in self._bars:
            bar.reset()
        self.max_label.setText("max —")
        self.clip_label.setText("no clipping")
        self.clip_label.setStyleSheet("QLabel { color: palette(mid); }")
