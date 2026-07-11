"""One place for formatting a duration/timestamp for display.

Everything the user sees is ``m:ss`` (``h:mm:ss`` once past an hour). Internal
APIs, JSON, and config stay in raw numeric seconds/milliseconds -- this is a
*display* helper only.
"""

from __future__ import annotations


def format_timestamp(seconds: float) -> str:
    """Format ``seconds`` as ``m:ss`` (or ``h:mm:ss`` when >= 1 hour).

    Rounds to the nearest second; negatives clamp to zero.

        >>> format_timestamp(7)
        '0:07'
        >>> format_timestamp(669)
        '11:09'
        >>> format_timestamp(3753)
        '1:02:33'
    """
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
