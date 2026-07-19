"""One definition of what secondary text looks like, with a contrast floor.

Three field reports of unreadable text -- the Levels hint, the monitor hint, and
then the summary card's restoration receipt, where the stakeholder could not
read the declick numbers on their own receipt. Each was fixed as an instance.
This is the cause: every one of them said ``color: palette(mid)``.

``mid`` is a *frame shading* role. Qt derives it for borders and grooves, and on
a dark theme it lands a short distance from the window background -- which is
correct for a bevel and useless for words. Widgets were reaching for it because
it is the closest thing the palette has to "grey", not because it means
"secondary text".

So secondary text is computed instead of borrowed: start from the real text
colour, blend it toward the background until it reads as de-emphasised, and then
stop blending the moment it would drop below a contrast floor. The floor is WCAG
AA for body text (4.5:1), which is a measurable version of the squint test and
holds on light and dark alike.

Receipts are deliberately *not* in here. A restoration receipt is content -- the
record of what was done to someone's audio -- and content gets full contrast.
Muting it was the original mistake.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

#: WCAG AA for normal-size text. Secondary text may look quieter than body text;
#: it may not become harder to read than this.
MIN_CONTRAST = 4.5

#: How far toward the background secondary text is allowed to travel before the
#: contrast floor starts pulling it back.
_MAX_BLEND = 0.55
_BLEND_STEP = 0.05


def _channel_luminance(value: float) -> float:
    value = value / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: QColor) -> float:
    """WCAG relative luminance, 0.0 (black) to 1.0 (white)."""
    return (0.2126 * _channel_luminance(colour.red())
            + 0.7152 * _channel_luminance(colour.green())
            + 0.0722 * _channel_luminance(colour.blue()))


def contrast_ratio(one: QColor, two: QColor) -> float:
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0."""
    a, b = relative_luminance(one), relative_luminance(two)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _blend(foreground: QColor, background: QColor, amount: float) -> QColor:
    """``amount`` of the way from ``foreground`` toward ``background``."""
    return QColor(
        round(foreground.red() + (background.red() - foreground.red()) * amount),
        round(foreground.green() + (background.green() - foreground.green()) * amount),
        round(foreground.blue() + (background.blue() - foreground.blue()) * amount),
    )


def muted_colour(palette: QPalette) -> QColor:
    """De-emphasised text that still clears :data:`MIN_CONTRAST`.

    Blends toward the window background in small steps and keeps the last one
    that still passes. If even the unblended text colour fails the floor -- a
    theme with genuinely poor contrast -- it is returned unchanged, because
    making it *worse* to satisfy a style rule would be the wrong trade.
    """
    text = palette.color(QPalette.ColorRole.WindowText)
    background = palette.color(QPalette.ColorRole.Window)

    best = text
    amount = 0.0
    while amount < _MAX_BLEND:
        amount += _BLEND_STEP
        candidate = _blend(text, background, amount)
        if contrast_ratio(candidate, background) < MIN_CONTRAST:
            break
        best = candidate
    return best


def muted_style(palette: QPalette, *, italic: bool = False, extra: str = "") -> str:
    """A stylesheet for secondary text: quieter, never unreadable."""
    parts = [f"color: {muted_colour(palette).name()}"]
    if italic:
        parts.append("font-style: italic")
    if extra:
        parts.append(extra.rstrip(";"))
    return "QLabel { " + "; ".join(parts) + "; }"


def apply_muted(widget, *, italic: bool = False, extra: str = "") -> None:
    """Style ``widget`` as secondary text, from its own palette."""
    widget.setStyleSheet(muted_style(widget.palette(), italic=italic, extra=extra))


def apply_body(widget, *, bold: bool = False) -> None:
    """Style ``widget`` as content: full contrast, no muting.

    For the things a receipt is *made of* -- what was done to the audio, where
    it went. These were muted, and a receipt nobody can read is not a receipt.
    """
    parts = ["color: palette(text)"]
    if bold:
        parts.append("font-weight: bold")
    widget.setStyleSheet("QLabel { " + "; ".join(parts) + "; }")
