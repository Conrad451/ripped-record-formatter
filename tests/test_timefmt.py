"""The shared display time formatter."""

import pytest

from core.timefmt import format_timestamp, tick_decimals_for_span


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0:00"),
        (7, "0:07"),
        (7.4, "0:07"),
        (59, "0:59"),
        (60, "1:00"),
        (669, "11:09"),      # 11:09
        (3600, "1:00:00"),
        (3753, "1:02:33"),   # 1:02:33
        (-5, "0:00"),        # clamps
    ],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


# --------------------------------------------------------------------------- #
# Sub-minute precision: a zoomed-in axis must not print identical ticks.
# --------------------------------------------------------------------------- #
def test_fractional_seconds_render():
    assert format_timestamp(69.42, 1) == "1:09.4"
    assert format_timestamp(69.42, 2) == "1:09.42"
    assert format_timestamp(5.0, 2) == "0:05.00"
    assert format_timestamp(3753.25, 1) == "1:02:33.2"


def test_fractional_rounding_carries_cleanly():
    """59.96s at one decimal is 1:00.0, never 0:60.0."""
    assert format_timestamp(59.96, 1) == "1:00.0"
    assert format_timestamp(59.999, 2) == "1:00.00"
    assert format_timestamp(3599.99, 1) == "1:00:00.0"


def test_zero_decimals_matches_legacy_behaviour():
    assert format_timestamp(7) == "0:07"
    assert format_timestamp(669) == "11:09"
    assert format_timestamp(3753) == "1:02:33"
    assert format_timestamp(7, 0) == "0:07"


def test_tick_decimals_follow_the_visible_span():
    assert tick_decimals_for_span(600.0) == 0      # whole side: m:ss is plenty
    assert tick_decimals_for_span(60.0) == 0
    assert tick_decimals_for_span(59.0) == 1       # under a minute: tenths
    assert tick_decimals_for_span(10.0) == 1
    assert tick_decimals_for_span(9.0) == 2        # tight zoom: hundredths
    assert tick_decimals_for_span(0.5) == 2


def test_tight_zoom_produces_distinct_labels():
    """The actual defect: whole seconds collapse to one label at tight zoom."""
    ticks = [12.00, 12.10, 12.20, 12.30]

    coarse = [format_timestamp(t) for t in ticks]
    assert len(set(coarse)) == 1                   # all "0:12" -- useless

    decimals = tick_decimals_for_span(ticks[-1] - ticks[0])
    fine = [format_timestamp(t, decimals) for t in ticks]
    assert len(set(fine)) == len(ticks)            # distinct again
    assert fine == ["0:12.00", "0:12.10", "0:12.20", "0:12.30"]
