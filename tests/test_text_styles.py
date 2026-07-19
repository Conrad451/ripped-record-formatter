"""Secondary text is quieter than body text, never harder to read than the floor.

Three field reports of unreadable text before this was treated as one problem:
the Levels hint, the monitor hint, and the summary card's restoration receipt --
where the stakeholder could not read the declick numbers on their own receipt.

Every one of them said ``color: palette(mid)``. Measured against the palette the
app actually runs with, that is **1.73:1** against the window background: below
even the 3:1 minimum for large text. ``mid`` is a frame-shading role, correct
for a bevel and useless for words.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QPalette

from gui.text_styles import (
    MIN_CONTRAST,
    contrast_ratio,
    muted_colour,
    muted_style,
)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _palette(text: str, window: str) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.WindowText, QColor(text))
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    return palette


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def test_contrast_ratio_matches_the_wcag_reference_points():
    white, black = QColor("#ffffff"), QColor("#000000")
    assert contrast_ratio(white, black) == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio(white, white) == pytest.approx(1.0, abs=0.01)
    # A mid grey against white, from the WCAG worked examples.
    assert contrast_ratio(QColor("#767676"), white) == pytest.approx(4.54, abs=0.05)


def test_contrast_is_symmetric():
    a, b = QColor("#2b2b2b"), QColor("#c0c0c0")
    assert contrast_ratio(a, b) == pytest.approx(contrast_ratio(b, a))


# --------------------------------------------------------------------------- #
# The floor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,window,label", [
    ("#e8e8e8", "#2b2b2b", "dark"),
    ("#f0f0f0", "#1e1e1e", "darker"),
    ("#ffffff", "#000000", "black"),
    ("#1a1a1a", "#f0f0f0", "light"),
    ("#000000", "#efefef", "the palette this app actually runs with"),
])
def test_muted_text_clears_the_contrast_floor(qapp, text, window, label):
    palette = _palette(text, window)
    ratio = contrast_ratio(muted_colour(palette), QColor(window))
    assert ratio >= MIN_CONTRAST, f"{label}: {ratio:.2f} < {MIN_CONTRAST}"


@pytest.mark.parametrize("text,window", [
    ("#e8e8e8", "#2b2b2b"),
    ("#000000", "#efefef"),
])
def test_muted_text_is_quieter_than_body_text(qapp, text, window):
    """It must actually be de-emphasised -- a floor that just returns the body
    colour would pass the contrast test and defeat the purpose."""
    palette = _palette(text, window)
    background = QColor(window)
    body = contrast_ratio(QColor(text), background)
    muted = contrast_ratio(muted_colour(palette), background)

    assert muted < body, "muted text is not actually quieter than body text"
    assert muted >= MIN_CONTRAST


def test_palette_mid_is_the_thing_we_stopped_using(qapp):
    """Pins the measurement that motivated the change, on the real palette.

    If a future Qt makes Mid a legitimate text colour this fails, and someone
    gets to delete a workaround rather than inherit a mystery.
    """
    palette = qapp.palette()
    background = palette.color(QPalette.ColorRole.Window)
    mid = palette.color(QPalette.ColorRole.Mid)

    assert contrast_ratio(mid, background) < MIN_CONTRAST, (
        "palette(mid) now clears the floor; the muted helper may be redundant")
    assert contrast_ratio(muted_colour(palette), background) >= MIN_CONTRAST


def test_a_hopeless_palette_is_not_made_worse(qapp):
    """If body text already fails the floor, muting must not push it further."""
    palette = _palette("#7a7a7a", "#808080")
    background = QColor("#808080")
    body = contrast_ratio(QColor("#7a7a7a"), background)

    assert contrast_ratio(muted_colour(palette), background) >= body


def test_muted_style_carries_italics_and_extras(qapp):
    palette = _palette("#000000", "#ffffff")
    plain = muted_style(palette)
    assert plain.startswith("QLabel {") and "color: #" in plain
    assert "font-style: italic" in muted_style(palette, italic=True)
    assert "padding: 24px" in muted_style(palette, extra="padding: 24px")


# --------------------------------------------------------------------------- #
# Applied where it matters
# --------------------------------------------------------------------------- #
def test_no_widget_styles_its_text_with_palette_mid(qapp):
    """The one-offs are gone and stay gone.

    Borders and backgrounds may still use Mid -- that is what it is for. This
    is specifically about `color:`.
    """
    import pathlib

    offenders = []
    for path in pathlib.Path("gui").glob("*.py"):
        if path.name == "text_styles.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "color: palette(mid)" in line and "border" not in line:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"secondary text still borrowing a border colour: {offenders}"


def test_the_restoration_receipt_is_content_not_a_hint(qapp):
    """The field report itself: the stakeholder could not read the declick
    numbers on their own receipt. A receipt is a record of what was done to
    someone's audio -- it gets full contrast, not the muted treatment."""
    from types import SimpleNamespace

    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    card.restoration_labels = []          # normally seeded by render()
    side = SimpleNamespace(declick_repaired_samples=1015,
                           declick_total_samples=132300)
    label = card._restoration_line(side)

    assert label is not None
    assert "1,015 of 132,300" in label.text()
    assert "palette(text)" in label.styleSheet(), (
        "the receipt is muted again; it is content, not a hint")
