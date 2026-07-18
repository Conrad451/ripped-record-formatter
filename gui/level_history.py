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

#: Scale shared with the bars in :mod:`gui.meters` -- one definition, so a level
#: lands in the same place in both views. -3 is the target the hint text names, so
#: it is the gridline drawn brightest.
from gui.level_scale import FLOOR_DBFS, GRIDLINES_DBFS  # noqa: E402  (re-exported)

#: Lane names, top to bottom.
_CHANNEL_LABELS = ("L", "R")

#: Which gridlines get a number on the left axis. All of GRIDLINES_DBFS are drawn;
#: a ~45 px lane has room for three labels, not five.
_LABELLED_DBFS = (0.0, -6.0, -20.0)

_BG = "#1e1f22"
_GRID = "#4a4d52"
_TARGET = "#c07000"          # -3 dBFS: the line you are aiming to stay under
_CEILING = "#c0392b"         # 0 dBFS: the line you must not touch
_CLIP = "#e74c3c"
_LABEL = "#8b9199"           # the "L"/"R" lane names
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
        #: (time, channel) -- the channel is which lane the tick belongs in.
        self._clip_times: deque[tuple[float, int]] = deque()
        self._clip_runs = 0
        self._channel_runs: list[int] = []
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
        self._channel_runs = []
        self._last_t = None

    def push(self, telemetry) -> None:
        """Add one telemetry snapshot and evict anything older than the window."""
        t = float(telemetry.elapsed_s)
        if self._last_t is not None and t < self._last_t:
            self.clear()                     # the clock restarted: a new stream
        self._last_t = t

        peaks = tuple(float(p) for p in telemetry.peaks_dbfs)
        self._t.append(t)
        self._peaks.append(peaks)

        # A clip event is a *rise* in the run count -- the count itself latches,
        # so only the moment it increased is news. Per-channel counts say which
        # lane the tick belongs in; a producer that doesn't track them still gets
        # a tick, attributed to whichever channel was loudest at that instant --
        # an inference, but a better one than marking both lanes.
        by_channel = [int(r) for r in getattr(telemetry, "clip_runs_by_channel", ())]
        if by_channel:
            if len(self._channel_runs) < len(by_channel):
                self._channel_runs += [0] * (len(by_channel) - len(self._channel_runs))
            for ch, runs in enumerate(by_channel):
                if runs > self._channel_runs[ch]:
                    self._clip_times.append((t, ch))
                self._channel_runs[ch] = max(self._channel_runs[ch], runs)
        elif int(telemetry.clip_runs) > self._clip_runs:
            loudest = max(range(len(peaks)), key=peaks.__getitem__) if peaks else 0
            self._clip_times.append((t, loudest))
        self._clip_runs = max(self._clip_runs, int(telemetry.clip_runs))

        cutoff = t - self.seconds
        while self._t and self._t[0] < cutoff:
            self._t.popleft()
            self._peaks.popleft()
        while self._clip_times and self._clip_times[0][0] < cutoff:
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

    def clip_marks(self, channel: int | None = None) -> list[float]:
        """x positions (seconds ago) of the clip events still in the window.

        ``channel`` restricts them to the events attributed to that channel --
        which is how each lane gets only its own ticks. ``None`` returns them all.
        """
        if not self._t:
            return []
        now = self._t[-1]
        return [t - now for t, ch in self._clip_times
                if channel is None or ch == channel]


class LevelHistoryStrip(pg.GraphicsLayoutWidget):
    """Stacked per-channel lanes over a shared time axis: L on top, R beneath.

    One lane per channel, each with its own dBFS scale, its own gridlines and its
    own clip ticks, sharing one scroll window and one set of time labels.

    **Two X-linked plots, not one plot with the second channel offset in Y.** The
    offset trick keeps a single coordinate space, and then every requirement of a
    lane becomes a workaround in it: the left axis has to have its tick *text*
    remapped so the lower lane reads -60..0 instead of -120..-60; the traces have
    to be clipped by hand so neither draws into the other's lane; the gridlines
    and clip ticks have to be drawn twice at manually offset positions. Two
    :class:`~pyqtgraph.PlotItem`\\ s get all of that from Qt: real per-lane axes
    with real labels, clipping at the ViewBox boundary for free, and ``setXLink``
    keeping the scroll window and time labels locked by construction rather than
    by us remembering to set both. The cost is a second ViewBox, which at a 20 Hz
    feed over 600 points is not a cost.

    Only the bottom lane draws the time axis -- it is one shared axis, so labelling
    it twice would state the same thing twice.
    """

    def __init__(self, channels: int = 2, seconds: float = HISTORY_SECONDS) -> None:
        super().__init__()
        self.history = LevelHistory(seconds)
        self._seconds = float(seconds)

        self.setBackground(_BG)
        self.setMinimumHeight(110)
        self.ci.setSpacing(2)

        self.lanes: list[pg.PlotItem] = []
        self._traces = []
        self._clip_marks = []
        self._lane_labels = []

        for i in range(channels):
            last = i == channels - 1
            plot = self.addPlot(row=i, col=0)
            self._configure_lane(plot, show_time_axis=last)

            for db in GRIDLINES_DBFS:
                if db == 0.0:
                    pen = pg.mkPen(_CEILING, width=1)
                elif db == -3.0:
                    pen = pg.mkPen(_TARGET, width=1,
                                   style=pg.QtCore.Qt.PenStyle.DashLine)
                else:
                    pen = pg.mkPen(_GRID, width=1,
                                   style=pg.QtCore.Qt.PenStyle.DotLine)
                plot.addItem(pg.InfiniteLine(pos=db, angle=0, pen=pen, movable=False))

            self._traces.append(plot.plot(
                [], [], stepMode="right",
                pen=pg.mkPen(_TRACES[i % len(_TRACES)], width=1)))

            # Clip ticks ride at the top edge *of their own lane*, so which channel
            # clipped is the thing you read first.
            marks = pg.ScatterPlotItem(
                x=[], y=[], symbol="s", size=6, pxMode=True,
                pen=pg.mkPen(_CLIP), brush=pg.mkBrush(_CLIP))
            plot.addItem(marks)
            self._clip_marks.append(marks)

            label = pg.TextItem(_CHANNEL_LABELS[i] if i < len(_CHANNEL_LABELS)
                                else str(i + 1),
                                color=_LABEL, anchor=(0, 0))
            label.setPos(-self._seconds, 0.0)
            plot.addItem(label)
            self._lane_labels.append(label)

            self.lanes.append(plot)

        # One time axis for all lanes: link every lane's X to the first, so a
        # scroll or a range change can never leave them showing different windows.
        for plot in self.lanes[1:]:
            plot.setXLink(self.lanes[0])
        # Push the window once *after* linking: a range set before the link exists
        # is the linked lane's own, and the two only converge on the next update.
        self.lanes[0].setXRange(-self._seconds, 0, padding=0)

    def _configure_lane(self, plot, *, show_time_axis: bool) -> None:
        """Fix one lane's scales, axes and interaction."""
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setXRange(-self._seconds, 0, padding=0)
        plot.setYRange(FLOOR_DBFS, 0, padding=0)
        # Pin the window: nothing may pan or auto-range a lane out of step with
        # its neighbour, and the trace must not draw past its own axis.
        plot.setLimits(xMin=-self._seconds, xMax=0, yMin=FLOOR_DBFS, yMax=0)
        plot.getViewBox().setDefaultPadding(0)
        plot.showAxis("right", False)
        plot.showAxis("top", False)

        left = plot.getAxis("left")
        left.setTextPen(_GRID)
        left.setPen(_GRID)
        # All five gridlines are *drawn* in every lane, but a lane is only ~45 px
        # tall once two of them share the strip's height budget, which is not room
        # for five stacked labels. Label the three that anchor the scale and leave
        # -3 and -12 as unlabelled ticks -- -3 is drawn as the bright dashed target
        # line anyway, which identifies it better than a number would.
        left.setTicks([
            [(db, f"{db:g}") for db in _LABELLED_DBFS],
            [(db, "") for db in GRIDLINES_DBFS if db not in _LABELLED_DBFS],
        ])
        left.setWidth(34)               # equal width, so the lanes align vertically

        bottom = plot.getAxis("bottom")
        if show_time_axis:
            bottom.setTextPen(_GRID)
            bottom.setPen(_GRID)
            bottom.setTicks([[(-s, "now" if s == 0 else f"-{s:g}s")
                              for s in (0, 10, 20, 30) if s <= self._seconds]])
        else:
            plot.showAxis("bottom", False)

    # -- feed ----------------------------------------------------------------
    def update_from(self, telemetry) -> None:
        self.history.push(telemetry)
        self.redraw()

    def reset(self) -> None:
        """Empty the strip. *Not* named ``clear``: a pyqtgraph widget copies
        ``PlotItem.clear`` onto the instance in its constructor, and an instance
        attribute shadows a subclass method -- an override there is silently
        never called."""
        self.history.clear()
        self.redraw()

    def redraw(self) -> None:
        for i, trace in enumerate(self._traces):
            xs, ys = self.history.series(i)
            trace.setData(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
        for i, marks in enumerate(self._clip_marks):
            xs = self.history.clip_marks(i)
            marks.setData(x=np.asarray(xs, dtype=float),
                          y=np.full(len(xs), -0.8, dtype=float))

    # -- for tests -----------------------------------------------------------
    @property
    def clip_mark_count(self) -> int:
        """Ticks across every lane."""
        return sum(len(m.getData()[0]) for m in self._clip_marks)

    def lane_clip_mark_count(self, channel: int) -> int:
        """Ticks in one lane -- which channel the strip says clipped."""
        return len(self._clip_marks[channel].getData()[0])

    def lane_x_range(self, channel: int) -> tuple[float, float]:
        """The lane's visible time window, for asserting the lanes stay locked."""
        return tuple(self.lanes[channel].getViewBox().viewRange()[0])
