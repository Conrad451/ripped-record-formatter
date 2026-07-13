"""Level history: the last N seconds of input peaks, scrolling.

An instantaneous meter answers *am I clipping right now?* -- which is the wrong
question when you are setting gain, because "right now" is over before you have
looked up. The question the gain ritual actually asks is *how did the loudest
passage of this record sit against full scale?*, and that is a question about the
recent past. So: a strip chart, WaveRepair-style. Thirty seconds of per-channel
peak, gridlines at the levels a person actually aims for, and clip events nailed
to the top edge in red so a transient you missed is still there when you look.

Two pieces, deliberately separable:

* :class:`LevelHistory` -- a rolling buffer of telemetry snapshots. No Qt, no
  drawing, so the eviction rules can be tested as the arithmetic they are.
* :class:`LevelHistoryStrip` -- a pyqtgraph plot over that buffer.

It supplements the latching clip indicator, it does not replace it: the latch
says *whether*, the strip says *when*.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np
import pyqtgraph as pg

#: How much history the strip shows. A side is twenty minutes; thirty seconds is
#: "the passage I just played", which is the unit the gain ritual works in.
HISTORY_SECONDS = 30.0

#: Bottom of the scale, matching the bars in :mod:`gui.meters`.
FLOOR_DBFS = -60.0

#: The levels a person setting gain actually aims at. -3 is the target the hint
#: text names, so it is the one drawn brightest.
GRIDLINES_DBFS = (0.0, -3.0, -6.0, -12.0, -20.0)

_BG = "#1e1f22"
_GRID = "#4a4d52"
_TARGET = "#c07000"          # -3 dBFS: the line you are aiming to stay under
_CEILING = "#c0392b"         # 0 dBFS: the line you must not touch
_CLIP = "#e74c3c"
#: Two channels of the same signal: the same hue, not a traffic light. Red and
#: amber are spoken for -- they mean danger here, and nothing else.
_TRACES = ("#5aa9d6", "#3b6ea5")


class LevelHistory:
    """A rolling window of telemetry snapshots, plus the clip events within it.

    Time comes from ``telemetry.elapsed_s``, which restarts at zero on every new
    stream. That is the signal for "a different stream is talking to me now" --
    the history is cleared rather than splicing two devices into one trace.
    """

    def __init__(self, seconds: float = HISTORY_SECONDS) -> None:
        self.seconds = float(seconds)
        self._t: deque[float] = deque()
        self._peaks: deque[tuple[float, ...]] = deque()
        self._clip_times: deque[float] = deque()
        self._clip_runs = 0
        self._last_t: float | None = None

    def __len__(self) -> int:
        return len(self._t)

    @property
    def clip_runs(self) -> int:
        return self._clip_runs

    def clear(self) -> None:
        self._t.clear()
        self._peaks.clear()
        self._clip_times.clear()
        self._clip_runs = 0
        self._last_t = None

    def push(self, telemetry) -> None:
        """Add one telemetry snapshot and evict anything older than the window."""
        t = float(telemetry.elapsed_s)
        if self._last_t is not None and t < self._last_t:
            self.clear()                     # the clock restarted: a new stream
        self._last_t = t

        self._t.append(t)
        self._peaks.append(tuple(float(p) for p in telemetry.peaks_dbfs))

        # A clip event is a *rise* in the run count -- the count itself latches,
        # so only the moment it increased is news.
        runs = int(telemetry.clip_runs)
        if runs > self._clip_runs:
            self._clip_times.append(t)
        self._clip_runs = max(self._clip_runs, runs)

        cutoff = t - self.seconds
        while self._t and self._t[0] < cutoff:
            self._t.popleft()
            self._peaks.popleft()
        while self._clip_times and self._clip_times[0] < cutoff:
            self._clip_times.popleft()

    # -- read-out, in plot coordinates ---------------------------------------
    @property
    def channels(self) -> int:
        return max((len(p) for p in self._peaks), default=0)

    def series(self, channel: int) -> tuple[list[float], list[float]]:
        """(x, y) for one channel: x is seconds *ago* (0 at the right edge).

        ``-inf`` (digital silence) is floored, because a plot cannot draw
        negative infinity and a meter has a bottom anyway.
        """
        if not self._t:
            return [], []
        now = self._t[-1]
        xs, ys = [], []
        for t, peaks in zip(self._t, self._peaks):
            if channel >= len(peaks):
                continue
            db = peaks[channel]
            if db is None or math.isnan(db):
                db = FLOOR_DBFS
            xs.append(t - now)
            ys.append(max(FLOOR_DBFS, min(0.0, db)))
        return xs, ys

    def clip_marks(self) -> list[float]:
        """x positions (seconds ago) of the clip events still in the window."""
        if not self._t:
            return []
        now = self._t[-1]
        return [t - now for t in self._clip_times]


class LevelHistoryStrip(pg.PlotWidget):
    """The strip: per-channel peak history with clip events on the top edge."""

    def __init__(self, channels: int = 2, seconds: float = HISTORY_SECONDS) -> None:
        super().__init__()
        self.history = LevelHistory(seconds)
        self._seconds = float(seconds)

        self.setBackground(_BG)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setMinimumHeight(110)
        self.setXRange(-self._seconds, 0, padding=0)
        self.setYRange(FLOOR_DBFS, 0, padding=0)

        plot = self.getPlotItem()
        plot.showAxis("right", False)
        plot.showAxis("top", False)

        left = plot.getAxis("left")
        left.setTextPen(_GRID)
        left.setPen(_GRID)
        # Label only the levels that mean something; a tick every 10 dB is noise.
        left.setTicks([[(db, f"{db:g}") for db in GRIDLINES_DBFS]])

        bottom = plot.getAxis("bottom")
        bottom.setTextPen(_GRID)
        bottom.setPen(_GRID)
        bottom.setTicks([[(-s, "now" if s == 0 else f"-{s:g}s")
                          for s in (0, 10, 20, 30) if s <= self._seconds]])

        for db in GRIDLINES_DBFS:
            if db == 0.0:
                pen = pg.mkPen(_CEILING, width=1)
            elif db == -3.0:
                pen = pg.mkPen(_TARGET, width=1, style=pg.QtCore.Qt.PenStyle.DashLine)
            else:
                pen = pg.mkPen(_GRID, width=1, style=pg.QtCore.Qt.PenStyle.DotLine)
            plot.addItem(pg.InfiniteLine(pos=db, angle=0, pen=pen, movable=False))

        self._traces = [
            plot.plot([], [], stepMode="right",
                      pen=pg.mkPen(_TRACES[i % len(_TRACES)], width=1))
            for i in range(channels)
        ]
        # Clip events ride at the very top of the strip, where the eye already is
        # when it is checking headroom.
        self._clip_marks = pg.ScatterPlotItem(
            x=[], y=[], symbol="s", size=6, pxMode=True,
            pen=pg.mkPen(_CLIP), brush=pg.mkBrush(_CLIP))
        plot.addItem(self._clip_marks)

    # -- feed ----------------------------------------------------------------
    def update_from(self, telemetry) -> None:
        self.history.push(telemetry)
        self.redraw()

    def reset(self) -> None:
        """Empty the strip. *Not* named ``clear``: :class:`pg.PlotWidget` copies
        ``PlotItem.clear`` onto the instance in its constructor, and an instance
        attribute shadows a subclass method -- an override there is silently
        never called."""
        self.history.clear()
        self.redraw()

    def redraw(self) -> None:
        for i, trace in enumerate(self._traces):
            xs, ys = self.history.series(i)
            trace.setData(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
        marks = self.history.clip_marks()
        self._clip_marks.setData(
            x=np.asarray(marks, dtype=float),
            y=np.full(len(marks), -0.8, dtype=float))

    # -- for tests -----------------------------------------------------------
    @property
    def clip_mark_count(self) -> int:
        return len(self._clip_marks.getData()[0])
