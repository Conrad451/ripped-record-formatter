"""Level history strip: the rolling buffer, and the plot over it.

The buffer is arithmetic and is tested as such. The strip is tested offscreen,
driven by the same Telemetry the audio thread emits -- so what is under test is
"does a clip actually show up on the strip", not "does pyqtgraph draw lines".
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

import pytest

from core.recorder import TELEMETRY_INTERVAL_S, Telemetry
from gui.level_history import (
    FLOOR_DBFS,
    HISTORY_SECONDS,
    LevelHistory,
    LevelHistoryStrip,
)

RATE = TELEMETRY_INTERVAL_S          # 20 Hz


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _snap(t, peaks=(-20.0, -20.0), clip_runs=0):
    return Telemetry(peaks_dbfs=list(peaks), max_peak_dbfs=max(peaks),
                     clip_runs=clip_runs, elapsed_s=t)


def _trace_len(trace):
    """pyqtgraph hands back None -- not an empty array -- for an empty trace."""
    xs = trace.getData()[0]
    return 0 if xs is None else len(xs)


# --------------------------------------------------------------------------- #
# The buffer: N seconds at the telemetry rate, and eviction
# --------------------------------------------------------------------------- #
def test_holds_the_window_at_the_telemetry_rate():
    h = LevelHistory()
    # Ten minutes of 20 Hz telemetry pushed through a 30 s window.
    for i in range(int(600 / RATE)):
        h.push(_snap(i * RATE))

    expected = HISTORY_SECONDS / RATE            # 30 s at 20 Hz = 600 samples
    assert len(h) == pytest.approx(expected, abs=2)
    # Memory is bounded by the window, not by how long the monitor has run.
    assert len(h) < 610


def test_evicts_from_the_left_as_it_scrolls():
    h = LevelHistory(seconds=1.0)
    for i in range(100):
        h.push(_snap(i * RATE, peaks=(-float(i), -float(i))))

    xs, ys = h.series(0)
    assert xs[-1] == pytest.approx(0.0)          # newest sits at the right edge
    assert xs[0] >= -1.0                         # nothing older than the window
    assert min(xs) >= -1.0 - 1e-9


def test_x_is_seconds_ago_with_now_at_zero():
    h = LevelHistory()
    for t in (10.0, 10.5, 11.0):
        h.push(_snap(t))
    xs, _ = h.series(0)
    assert xs == pytest.approx([-1.0, -0.5, 0.0])


def test_digital_silence_is_floored_not_infinite():
    h = LevelHistory()
    h.push(_snap(0.0, peaks=(-math.inf, -math.inf)))
    _, ys = h.series(0)
    assert ys == [FLOOR_DBFS]                    # a plot cannot draw -inf


def test_a_new_stream_restarts_the_history():
    """elapsed_s going backwards means a different stream is talking now."""
    h = LevelHistory()
    for i in range(20):
        h.push(_snap(10.0 + i * RATE))
    assert len(h) == 20

    h.push(_snap(0.0))                           # the monitor restarted
    assert len(h) == 1                           # not spliced onto the old trace


# --------------------------------------------------------------------------- #
# Clip events: when, not just whether
# --------------------------------------------------------------------------- #
def test_a_clip_run_leaves_a_mark_at_the_moment_it_happened():
    h = LevelHistory()
    h.push(_snap(0.0, clip_runs=0))
    h.push(_snap(1.0, peaks=(0.0, 0.0), clip_runs=1))     # clips here
    h.push(_snap(2.0, clip_runs=1))                       # still latched, not new

    assert h.clip_marks() == pytest.approx([-1.0])        # one mark, 1 s ago
    assert h.clip_runs == 1


def test_each_new_run_is_its_own_mark():
    h = LevelHistory()
    h.push(_snap(0.0, clip_runs=0))
    h.push(_snap(1.0, clip_runs=1))
    h.push(_snap(2.0, clip_runs=3))              # two more runs since last snapshot
    assert len(h.clip_marks()) == 2              # the *rises*, not the count
    assert h.clip_runs == 3


def test_clip_marks_scroll_out_of_the_window_with_everything_else():
    h = LevelHistory(seconds=5.0)
    h.push(_snap(0.0, clip_runs=0))
    h.push(_snap(1.0, clip_runs=1))              # a clip at t=1
    assert len(h.clip_marks()) == 1

    h.push(_snap(30.0, clip_runs=1))             # ...29 s later it is off the strip
    assert h.clip_marks() == []
    assert h.clip_runs == 1                      # but the latch still remembers


# --------------------------------------------------------------------------- #
# The strip itself, offscreen
# --------------------------------------------------------------------------- #
def test_the_strip_renders_and_updates_from_telemetry(qapp):
    strip = LevelHistoryStrip(channels=2)
    strip.resize(400, 120)
    strip.show()

    for i in range(60):                          # 3 s of telemetry
        strip.update_from(_snap(i * RATE, peaks=(-10.0, -14.0)))
    qapp.processEvents()

    left, right = strip._traces
    lx, ly = left.getData()
    rx, ry = right.getData()
    assert len(lx) == 60 and len(rx) == 60       # both channels traced
    assert ly[-1] == pytest.approx(-10.0)        # ...at their own levels
    assert ry[-1] == pytest.approx(-14.0)
    assert lx[-1] == pytest.approx(0.0)          # newest at the right edge


def test_an_injected_full_scale_run_puts_a_tick_on_the_strip(qapp):
    strip = LevelHistoryStrip(channels=2)
    assert strip.clip_mark_count == 0

    strip.update_from(_snap(0.0, peaks=(-20.0, -20.0), clip_runs=0))
    strip.update_from(_snap(0.5, peaks=(0.0, 0.0), clip_runs=1))   # full scale
    strip.update_from(_snap(1.0, peaks=(-20.0, -20.0), clip_runs=1))
    qapp.processEvents()

    assert strip.clip_mark_count == 1
    xs, ys = strip._clip_marks.getData()
    assert xs[0] == pytest.approx(-0.5)          # at the moment it clipped, and
                                                 # it stays there as it scrolls
    assert ys[0] > FLOOR_DBFS / 2                # ...pinned to the top edge


def test_the_strip_runs_during_capture_not_only_pre_roll(qapp):
    """Same widget, same feed: telemetry is telemetry whoever is emitting it."""
    strip = LevelHistoryStrip(channels=2)
    for i in range(10):
        strip.update_from(_snap(i * RATE, peaks=(-6.0, -6.0)))
    before = _trace_len(strip._traces[0])

    # The Recorder takes over: its telemetry restarts the clock at zero.
    strip.update_from(_snap(0.0, peaks=(-6.0, -6.0)))
    assert before == 10
    assert _trace_len(strip._traces[0]) == 1           # a fresh trace for the take


def test_reset_empties_the_strip(qapp):
    strip = LevelHistoryStrip(channels=2)
    strip.update_from(_snap(0.0, clip_runs=1))
    strip.reset()
    assert strip.clip_mark_count == 0
    assert _trace_len(strip._traces[0]) == 0
