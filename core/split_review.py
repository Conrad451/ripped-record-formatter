"""Compare proposed split segments against an expected tracklist -- pure logic.

Two checks a UI runs after a proposal:

* :func:`detect_progressive_drift` -- the *mis-anchor signature*. When the wrong
  side is selected, segment lengths don't just differ randomly; the cumulative
  boundary error grows steadily across the side. That monotone growth (not raw
  magnitude) is what distinguishes "wrong side" from "one gap slightly off".
* :func:`segment_deviations` -- a per-segment soft flag for a single length that
  is far from its expected value (used to hint on user-moved markers). It never
  moves anything; it only points.

Durations here are in seconds. Both functions are total and side-effect free.
"""

from __future__ import annotations


def _cumulative_errors(actual: list[float], expected: list[float]) -> list[float]:
    """Boundary error (actual - expected) accumulated after each track."""
    n = min(len(actual), len(expected))
    errors: list[float] = []
    a = e = 0.0
    for i in range(n):
        a += actual[i]
        e += expected[i]
        errors.append(a - e)
    return errors


def detect_progressive_drift(
    actual_durations: list[float],
    expected_durations: list[float],
    *,
    drift_frac: float = 0.5,
    monotone_frac: float = 0.7,
    min_tracks: int = 3,
) -> bool:
    """Return ``True`` when segment boundaries drift steadily off the tracklist.

    The cumulative boundary error is computed track by track; drift is flagged
    when its magnitude is non-decreasing across at least ``monotone_frac`` of the
    steps *and* the final error exceeds ``drift_frac`` of an average track. A
    correctly matched side keeps the cumulative error hovering near zero and
    trips neither condition.
    """
    errors = _cumulative_errors(actual_durations, expected_durations)
    n = len(errors)
    if n < min_tracks:
        return False
    magnitudes = [abs(x) for x in errors]
    grew = sum(magnitudes[i + 1] >= magnitudes[i] for i in range(n - 1))
    mostly_growing = grew >= (n - 1) * monotone_frac

    avg_expected = sum(expected_durations[:n]) / n if n else 0.0
    final_large = magnitudes[-1] > drift_frac * avg_expected
    return mostly_growing and final_large


def segment_deviations(
    actual_durations: list[float],
    expected_durations: list[float],
    *,
    tolerance_frac: float = 0.25,
) -> list[bool]:
    """Per-segment flag: is this length off its expected by > ``tolerance_frac``?

    Returns a list aligned to ``actual_durations``; entries past the expected
    list (or with a zero expected) are ``False``.
    """
    flags: list[bool] = []
    for i, actual in enumerate(actual_durations):
        if i >= len(expected_durations) or expected_durations[i] <= 0:
            flags.append(False)
            continue
        rel = abs(actual - expected_durations[i]) / expected_durations[i]
        flags.append(rel > tolerance_frac)
    return flags
