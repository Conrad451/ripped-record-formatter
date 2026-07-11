"""Pure side-partition logic + wrong-side guard."""

from core.side_partition import Side, default_dividers, partition
from core.split_review import wrong_side_suspected


def _all_indices(sides):
    return [i for s in sides for i in s.track_indices]


def test_order_preserved_and_durations_summed():
    durations = [100, 200, 300, 400, 500, 600]
    sides = partition(durations, 2)
    # every track present exactly once, in original order
    assert _all_indices(sides) == list(range(6))
    # totals equal the sum of each side's slice
    assert sum(s.total_ms for s in sides) == sum(durations)
    for s in sides:
        assert s.total_ms == sum(durations[i] for i in s.track_indices)


def test_default_dividers_target_equal_duration():
    durations = [300, 100, 100, 100, 100, 300]  # total 1000, ideal split at 500/500
    assert default_dividers(durations, 2) == [3]
    sides = partition(durations, 2)
    assert [s.track_count for s in sides] == [3, 3]
    assert [s.total_ms for s in sides] == [500, 500]


def test_three_equal_sides():
    durations = [100] * 6
    assert default_dividers(durations, 3) == [2, 4]
    sides = partition(durations, 3)
    assert [s.track_indices for s in sides] == [(0, 1), (2, 3), (4, 5)]


def test_one_side_is_the_whole_list():
    durations = [100, 200, 300]
    assert default_dividers(durations, 1) == []
    sides = partition(durations, 1)
    assert len(sides) == 1
    assert sides[0].track_indices == (0, 1, 2)


def test_sides_equal_track_count_gives_one_track_each():
    durations = [100, 200, 300]
    sides = partition(durations, 3)
    assert [s.track_indices for s in sides] == [(0,), (1,), (2,)]


def test_sides_more_than_tracks_is_clamped():
    durations = [100, 200, 300]
    sides = partition(durations, 9)  # can't exceed 3 tracks
    assert [s.track_indices for s in sides] == [(0,), (1,), (2,)]


def test_custom_dividers_respected():
    durations = [100, 200, 300, 400]
    sides = partition(durations, 2, dividers=[1])
    assert [s.track_indices for s in sides] == [(0,), (1, 2, 3)]


def test_empty_tracklist():
    assert partition([], 2) == []
    assert default_dividers([], 2) == []


def test_wrong_side_suspected():
    # 14 tracks -> 13 boundaries; 13 unresolved is clearly the wrong side.
    assert wrong_side_suspected(14, 13) is True
    assert wrong_side_suspected(14, 7) is True     # > 6.5
    assert wrong_side_suspected(14, 6) is False     # <= 6.5
    assert wrong_side_suspected(5, 1) is False
    assert wrong_side_suspected(1, 0) is False      # single track, no boundaries
