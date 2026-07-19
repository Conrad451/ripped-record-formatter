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


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


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


# --------------------------------------------------------------------------- #
# Instrument-grade bars (9.14 Part 2): per-channel numerics, a calibrated rule
# --------------------------------------------------------------------------- #
def test_the_channel_readout_states_dbfs_to_one_decimal():
    from gui.meters import format_channel_peak

    assert format_channel_peak(-7.04) == "−7.0"
    assert format_channel_peak(-7.0) == "−7.0"
    assert format_channel_peak(-0.92) == "−0.9"
    assert format_channel_peak(0.0) == "0.0"
    assert format_channel_peak(-math.inf) == "—"        # nothing heard yet


def test_injected_amplitudes_reach_the_bar_readouts_as_true_dbfs(qapp):
    """The number beside the bar is the number the telemetry carried.

    This is the readout end of the same invariant the recorder enforces: a
    known amplitude has to survive the whole path and arrive as its true dBFS,
    or the meter is decoration.
    """
    from core.recorder import _to_dbfs
    from gui.meters import LevelMeters

    meters = LevelMeters(channels=2)
    for amplitude, expected in ((0.9, "−0.9"), (0.5, "−6.0"), (1.0, "0.0")):
        meters.rows[0].set_level(_to_dbfs(amplitude))
        assert meters.rows[0].peak_label.text() == expected


def test_the_clip_zone_starts_at_the_level_the_hint_names(qapp):
    """The red band and the sentence under it must mean the same thing."""
    from gui.level_scale import dbfs_fraction
    from gui.meters import CLIP_ZONE_DBFS, clip_zone_fraction

    assert CLIP_ZONE_DBFS == -3.0                       # what the hint text says
    assert clip_zone_fraction() == pytest.approx(0.95)  # top 5% of a -60..0 bar
    assert clip_zone_fraction() == dbfs_fraction(CLIP_ZONE_DBFS)


def test_the_scale_rules_the_same_landmarks_as_the_lanes(qapp):
    """One scale authority: the rule under the bars and the history lanes draw
    the same numbers at the same fractions."""
    from gui.level_scale import dbfs_fraction
    from gui.meters import MeterScale

    scale = MeterScale()
    scale.resize(200, MeterScale.HEIGHT)
    # Every landmark is on the scale, and inside the widget.
    for db in GRIDLINES_DBFS:
        x = 200 * dbfs_fraction(db)
        assert 0.0 <= x <= 200.0
    assert set(GRIDLINES_DBFS) == {0.0, -3.0, -6.0, -12.0, -20.0}


def test_per_channel_max_hold_updates_independently(qapp):
    """The single shared max could not say *which* side was hot. These can."""
    from gui.meters import LevelMeters

    meters = LevelMeters(channels=2)
    meters.rows[0].set_level(-4.0)      # left peaks
    meters.rows[1].set_level(-20.0)     # right stays quiet

    assert meters.rows[0].max_hold == pytest.approx(-4.0)
    assert meters.rows[1].max_hold == pytest.approx(-20.0)
    assert meters.rows[0].hold_label.text() == "−4.0"
    assert meters.rows[1].hold_label.text() == "−20.0"

    # Left falls back; its hold stays put while right climbs past it.
    meters.rows[0].set_level(-30.0)
    meters.rows[1].set_level(-2.0)

    assert meters.rows[0].max_hold == pytest.approx(-4.0)   # held, not followed
    assert meters.rows[1].max_hold == pytest.approx(-2.0)
    assert meters.rows[0].peak_label.text() == "−30.0"      # current still moves


def test_reset_clears_both_channels_holds(qapp):
    from gui.meters import LevelMeters

    meters = LevelMeters(channels=2)
    meters.rows[0].set_level(-4.0)
    meters.rows[1].set_level(-9.0)
    meters.reset()

    for row in meters.rows:
        assert math.isinf(row.max_hold)
        assert row.hold_label.text() == "—"
        assert row.peak_label.text() == "—"


def test_the_bars_have_real_physical_presence(qapp):
    """Gain-setting is the app's highest-stakes judgment; the meter is sized for
    it rather than being a hairline. Guards against a silent shrink."""
    from gui.meters import BAR_HEIGHT, LevelMeters

    assert BAR_HEIGHT >= 24
    meters = LevelMeters(channels=2)
    for row in meters.rows:
        assert row.bar.minimumHeight() >= 24
