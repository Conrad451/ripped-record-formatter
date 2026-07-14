"""One place for formatting a duration/timestamp for display.

Everything the user sees is ``m:ss`` (``h:mm:ss`` once past an hour). Internal
APIs, JSON, and config stay in raw numeric seconds/milliseconds -- this is a
*display* helper only.
"""

from __future__ import annotations


def format_timestamp(seconds: float, decimals: int = 0) -> str:
    """Format ``seconds`` as ``m:ss`` (or ``h:mm:ss`` when >= 1 hour).

    Rounds to the nearest second; negatives clamp to zero.

        >>> format_timestamp(7)
        '0:07'
        >>> format_timestamp(669)
        '11:09'
        >>> format_timestamp(3753)
        '1:02:33'

    ``decimals`` adds fractional seconds, which a zoomed-in waveform axis needs:
    whole seconds collapse to the same label once the visible span is only a few
    seconds wide, and an axis of identical ticks is useless.

        >>> format_timestamp(69.42, 1)
        '1:09.4'
        >>> format_timestamp(69.42, 2)
        '1:09.42'
        >>> format_timestamp(59.96, 1)
        '1:00.0'
    """
    seconds = max(0.0, float(seconds))
    if decimals <= 0:
        total = int(round(seconds))
        frac = ""
    else:
        # Round once, at the target precision, so 59.96s at 1dp becomes 1:00.0
        # rather than 0:60.0.
        quantum = 10 ** decimals
        ticks = int(round(seconds * quantum))
        total, remainder = divmod(ticks, quantum)
        frac = f".{remainder:0{decimals}d}"

    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}{frac}"
    return f"{minutes}:{secs:02d}{frac}"


def format_size(num_bytes: int) -> str:
    """Format a byte count for display: ``512 B``, ``1.5 KB``, ``24.3 MB``.

    Binary units (1 KB = 1024 B); one decimal past bytes. Negatives clamp to
    zero. A display helper only -- like :func:`format_timestamp`, the underlying
    value stays a raw integer everywhere else.

        >>> format_size(512)
        '512 B'
        >>> format_size(1536)
        '1.5 KB'
        >>> format_size(25_500_000)
        '24.3 MB'
    """
    size = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def tick_decimals_for_span(span_seconds: float) -> int:
    """How many fractional-second digits an axis covering ``span_seconds`` needs.

    The single place the zoom thresholds live, so every axis and readout agrees:
    below 10 s of visible span, ticks are ~1 s apart or closer and need
    hundredths; below 60 s, tenths; above that, whole seconds are distinct.
    """
    span = max(0.0, float(span_seconds))
    if span < 10.0:
        return 2
    if span < 60.0:
        return 1
    return 0
