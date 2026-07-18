"""Meters: the headroom readout.

The max peak is only half a reading. The number the gain ritual actually targets
is the *margin* left under full scale, so the meter states it rather than leaving
it as mental arithmetic -- and colours it, so it can be read without being read.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

import pytest

from gui.level_scale import FLOOR_DBFS, GRIDLINES_DBFS
from gui.meters import _fraction, format_headroom, headroom_colour


# --------------------------------------------------------------------------- #
# Headroom: the number the gain ritual actually targets
# --------------------------------------------------------------------------- #
def test_headroom_is_stated_as_itself():
    assert format_headroom(-4.2) == "max −4.2 dBFS (4.2 dB headroom)"
    assert format_headroom(-0.5) == "max −0.5 dBFS (0.5 dB headroom)"
    assert format_headroom(0.0) == "max 0.0 dBFS (0.0 dB headroom)"
    assert format_headroom(-math.inf) == "max —"       # nothing heard yet


def test_headroom_colour_thresholds():
    green, amber, red = (headroom_colour(-6.0), headroom_colour(-2.0),
                         headroom_colour(0.0))
    assert green.name() == "#3aa655"
    assert amber.name() == "#c07000"
    assert red.name() == "#c0392b"

    # The boundaries themselves: green *below* -3, amber from -3, red from -0.5.
    assert headroom_colour(-3.1).name() == green.name()
    assert headroom_colour(-3.0).name() == amber.name()
    assert headroom_colour(-0.6).name() == amber.name()
    assert headroom_colour(-0.5).name() == red.name()
    assert headroom_colour(+0.0).name() == red.name()


# --------------------------------------------------------------------------- #
# The bar scale, and its agreement with the history lanes (9.9 part 4d)
# --------------------------------------------------------------------------- #
def test_bar_fraction_is_dbfs_scaled_on_the_shared_minus_60_to_0_map():
    assert _fraction(0.0) == pytest.approx(1.0)
    assert _fraction(FLOOR_DBFS) == pytest.approx(0.0)
    assert _fraction(-70.0) == pytest.approx(0.0)      # below the floor, clamped
    assert _fraction(12.0) == pytest.approx(1.0)       # over full scale, clamped
    assert _fraction(-math.inf) == pytest.approx(0.0)  # digital silence

    # -7 dBFS sits where -7 dBFS belongs on a -60..0 scale: 53/60 of the way up.
    assert _fraction(-7.0) == pytest.approx(53.0 / 60.0)
    assert _fraction(-3.0) == pytest.approx(0.95)
    assert _fraction(-30.0) == pytest.approx(0.5)


def test_the_bars_and_the_history_lanes_use_one_scale():
    """The whole point of gui.level_scale: two views of one signal cannot drift.
    A level must land at the same fraction of the bar as of the lane."""
    from gui.level_history import FLOOR_DBFS as LANE_FLOOR
    from gui.level_history import GRIDLINES_DBFS as LANE_GRID

    assert LANE_FLOOR == FLOOR_DBFS
    assert LANE_GRID == GRIDLINES_DBFS

    for db in (-7.0, -3.0, -6.0, -12.0, -20.0, -45.0):
        lane_fraction = (db - LANE_FLOOR) / (0.0 - LANE_FLOOR)   # lane y -> height
        assert _fraction(db) == pytest.approx(lane_fraction)
