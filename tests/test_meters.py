"""Meters: the headroom readout.

The max peak is only half a reading. The number the gain ritual actually targets
is the *margin* left under full scale, so the meter states it rather than leaving
it as mental arithmetic -- and colours it, so it can be read without being read.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

from gui.meters import format_headroom, headroom_colour


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
