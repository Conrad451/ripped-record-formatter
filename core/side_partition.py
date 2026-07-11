"""Partition a flat tracklist into vinyl sides -- pure, GUI-agnostic.

When MusicBrainz gives a single CD medium for a record that was actually pressed
across several vinyl sides, the user needs to say "this is really 2 sides" and
place the dividers. This module owns that logic; the GUI is a thin view over it.

Rules:

* **Track order is immutable.** Only *divider positions* move -- a divider sits
  between two adjacent tracks. ``num_sides`` sides need ``num_sides - 1``
  dividers.
* **Default placement** targets near-equal cumulative duration per side: for the
  k-th divider we pick the between-track boundary whose cumulative duration is
  closest to ``k / num_sides`` of the total, left to right, always leaving at
  least one track for every remaining side.

Durations are integer milliseconds (missing durations should be passed as 0).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Side:
    """One side: the contiguous tracks assigned to it and their total length."""

    index: int                      # 0-based side index
    track_indices: tuple[int, ...]  # indices into the original flat tracklist
    total_ms: int

    @property
    def track_count(self) -> int:
        return len(self.track_indices)


def default_dividers(durations_ms: list[int], num_sides: int) -> list[int]:
    """Return ``num_sides - 1`` divider positions for near-equal sides.

    A divider position ``p`` means "a new side starts at track index ``p``", so
    positions are in ``1 .. len-1`` and strictly increasing.
    """
    n = len(durations_ms)
    num_sides = max(1, min(num_sides, n)) if n else 1
    if num_sides <= 1 or n == 0:
        return []

    total = sum(durations_ms)
    cumulative: list[int] = []
    running = 0
    for d in durations_ms:
        running += d
        cumulative.append(running)   # cumulative[i] = sum of tracks 0..i

    dividers: list[int] = []
    for k in range(1, num_sides):
        target = total * k / num_sides
        low = (dividers[-1] + 1) if dividers else 1
        high = n - (num_sides - k)   # leave >= 1 track for each remaining side
        best_pos = low
        best_diff = None
        for pos in range(low, high + 1):
            diff = abs(cumulative[pos - 1] - target)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_pos = pos
        dividers.append(best_pos)
    return dividers


def partition(durations_ms: list[int], num_sides: int,
              dividers: list[int] | None = None) -> list[Side]:
    """Split the tracklist into :class:`Side` s at ``dividers`` (defaults if None).

    Order is preserved; each side's ``total_ms`` is the sum of its tracks.
    """
    n = len(durations_ms)
    if n == 0:
        return []
    if dividers is None:
        dividers = default_dividers(durations_ms, num_sides)
    # Sanitise: keep in range, unique, sorted.
    dividers = sorted({p for p in dividers if 0 < p < n})

    bounds = [0, *dividers, n]
    sides: list[Side] = []
    for side_index in range(len(bounds) - 1):
        start, stop = bounds[side_index], bounds[side_index + 1]
        sides.append(Side(
            index=side_index,
            track_indices=tuple(range(start, stop)),
            total_ms=sum(durations_ms[start:stop]),
        ))
    return sides
