"""The shared display time formatter."""

import pytest

from core.timefmt import format_timestamp


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
