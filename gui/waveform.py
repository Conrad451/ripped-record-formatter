"""Waveform view: a pyqtgraph plot of a min/max envelope with split markers.

The heavy lifting (reading the file, computing the envelope) is done off-thread
by :mod:`core.waveform`; this widget only draws a :class:`WaveformEnvelope` and
manages the split markers on top of it.

Markers are draggable vertical :class:`~pyqtgraph.InfiniteLine` s: drag to move,
double-click to delete, add one via :meth:`add_marker` (host wires a button).
A marker's confidence, if known, appears **only** in its tooltip and is labelled
as a within-rip ranking -- never presented as an absolute quality grade.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal

_WAVE_PEN = pg.mkPen("#3b6ea5")
_MARKER_PEN = pg.mkPen("#c0392b", width=2)
_MARKER_HOVER = pg.mkPen("#e74c3c", width=3)
_REGION_BRUSH = pg.mkBrush(255, 196, 0, 60)


class WaveformView(pg.PlotWidget):
    """Display-only waveform with editable split markers."""

    markersChanged = Signal()   # any add / move / delete

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground("w")
        self.showGrid(x=True, y=False, alpha=0.2)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.setLabel("bottom", "Time", units="s")
        self.setYRange(-1.0, 1.0)
        self.getPlotItem().hideButtons()

        self._curve = pg.PlotCurveItem(pen=_WAVE_PEN)
        self.addItem(self._curve)
        self._markers: list[pg.InfiniteLine] = []
        self._region: pg.LinearRegionItem | None = None
        self._env = None

        self.scene().sigMouseClicked.connect(self._on_scene_clicked)

    # -- envelope -----------------------------------------------------------
    def set_envelope(self, env) -> None:
        """Draw a :class:`core.waveform.WaveformEnvelope` (or clear on ``None``)."""
        self._env = env
        if env is None or env.num_buckets == 0:
            self._curve.clear()
            return
        # Each bucket becomes one vertical bar from its min to its max: repeat the
        # time and alternate min/max, drawn as disconnected pairs.
        times = np.asarray(env.times)
        x = np.repeat(times, 2)
        y = np.empty(2 * times.shape[0], dtype=np.float32)
        y[0::2] = env.mins
        y[1::2] = env.maxs
        self._curve.setData(x=x, y=y, connect="pairs")
        self.setXRange(0.0, max(env.duration, 1e-6), padding=0.02)
        self.setYRange(-1.0, 1.0)

    @property
    def duration(self) -> float:
        return self._env.duration if self._env is not None else 0.0

    # -- markers ------------------------------------------------------------
    def _add_marker_line(self, time: float, confidence: float | None) -> pg.InfiniteLine:
        line = pg.InfiniteLine(
            pos=float(time), angle=90, movable=True,
            pen=_MARKER_PEN, hoverPen=_MARKER_HOVER,
        )
        if confidence is not None:
            line.setToolTip(
                f"confidence {confidence:.2f}\n"
                "(within-rip ranking, not an absolute quality grade)"
            )
        line.sigPositionChangeFinished.connect(lambda *_: self.markersChanged.emit())
        self.addItem(line)
        self._markers.append(line)
        return line

    def add_marker(self, time: float, confidence: float | None = None) -> pg.InfiniteLine:
        line = self._add_marker_line(time, confidence)
        self.markersChanged.emit()
        return line

    def add_marker_at_center(self) -> None:
        (x0, x1), _ = self.getPlotItem().getViewBox().viewRange()
        self.add_marker((x0 + x1) / 2.0)

    def set_markers(self, times, confidences=None) -> None:
        self.clear_markers(emit=False)
        for i, t in enumerate(times):
            conf = confidences[i] if confidences is not None and i < len(confidences) else None
            self._add_marker_line(t, conf)
        self.markersChanged.emit()

    def marker_times(self) -> list[float]:
        return sorted(float(line.value()) for line in self._markers)

    def marker_count(self) -> int:
        return len(self._markers)

    def clear_markers(self, emit: bool = True) -> None:
        for line in self._markers:
            self.removeItem(line)
        self._markers.clear()
        if emit:
            self.markersChanged.emit()

    def _remove_marker(self, line: pg.InfiniteLine) -> None:
        self.removeItem(line)
        if line in self._markers:
            self._markers.remove(line)

    # -- region highlight + zoom (used by the unresolved-gap flow) ----------
    def highlight_region(self, start: float, end: float) -> None:
        self.clear_region()
        self._region = pg.LinearRegionItem(values=(start, end), movable=False, brush=_REGION_BRUSH)
        self._region.setZValue(-10)
        self.addItem(self._region)

    def clear_region(self) -> None:
        if self._region is not None:
            self.removeItem(self._region)
            self._region = None

    def zoom_to(self, start: float, end: float, pad_frac: float = 0.15) -> None:
        span = max(1e-6, end - start)
        pad = span * pad_frac
        self.setXRange(start - pad, end + pad, padding=0)

    def zoom_full(self) -> None:
        if self._env is not None:
            self.setXRange(0.0, max(self._env.duration, 1e-6), padding=0.02)

    # -- double-click to delete a marker ------------------------------------
    def _x_tolerance(self, pixels: float) -> float:
        vb = self.getPlotItem().getViewBox()
        (x0, x1), _ = vb.viewRange()
        width = vb.width() or 1
        return (x1 - x0) / width * pixels

    def _on_scene_clicked(self, event) -> None:
        if not event.double() or not self._markers:
            return
        vb = self.getPlotItem().getViewBox()
        x = vb.mapSceneToView(event.scenePos()).x()
        nearest = min(self._markers, key=lambda line: abs(line.value() - x))
        if abs(nearest.value() - x) <= self._x_tolerance(8):
            self._remove_marker(nearest)
            self.markersChanged.emit()
            event.accept()
