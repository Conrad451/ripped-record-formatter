"""The one dBFS scale the level UI draws on.

The instantaneous bars (:mod:`gui.meters`) and the history lanes
(:mod:`gui.level_history`) show the same number about the same signal, so they
must place it in the same spot. Before this module each owned a private copy of
the floor and the mapping, which is exactly the shape of bug where two views of
one signal drift apart. One definition, imported by both.

Qt-free, so the arithmetic can be tested as arithmetic.
"""

from __future__ import annotations

import math

#: Bottom of every level scale in the app. Below this there is nothing to show.
FLOOR_DBFS = -60.0

#: The levels a person setting gain actually aims at, drawn as landmarks on both
#: the bars and the lanes. -3 is the target the hint text names.
GRIDLINES_DBFS = (0.0, -3.0, -6.0, -12.0, -20.0)


def dbfs_fraction(dbfs: float) -> float:
    """dBFS -> 0..1 along the scale, linear in dB from :data:`FLOOR_DBFS` to 0.

    Linear-in-dB is the honest mapping for a level display, but note what it
    does to the top of the range: everything above -20 dBFS lives in the top
    third, so -7 dBFS legitimately sits at 0.88. That is why both views draw the
    :data:`GRIDLINES_DBFS` landmarks -- the position is truthful, and the marks
    are what let the eye read it as "below -6" instead of "nearly full".
    """
    if dbfs is None or math.isinf(dbfs) or math.isnan(dbfs):
        return 0.0 if (dbfs is None or dbfs < 0) else 1.0
    if dbfs <= FLOOR_DBFS:
        return 0.0
    if dbfs >= 0.0:
        return 1.0
    return (dbfs - FLOOR_DBFS) / (0.0 - FLOOR_DBFS)
