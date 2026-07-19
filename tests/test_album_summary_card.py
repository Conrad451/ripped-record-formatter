"""Offscreen GUI tests for the finished-album summary card.

Renders the card from fabricated AlbumSummary objects (no real album run) and
asserts what the user sees: per-side lines in the right states, the warnings
roll-up only when a track carried a warning, and the open-folder action
targeting the captured destination.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from core.album import AlbumSummary, SideState, SideSummary


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _summary(*, with_warnings: bool) -> AlbumSummary:
    """Side A done (2 tracks), Side B errored -- optionally A carried a warning."""
    a = SideSummary(
        index=0, label="Side A", state=SideState.DONE, track_count=2,
        output_paths=(Path("out/[01].flac"), Path("out/[02].flac")),
        total_bytes=2_500_000, duration_s=185.0,
        warnings=("Could not embed cover art: boom",) if with_warnings else (),
        warned_tracks=1 if with_warnings else 0,
    )
    b = SideSummary(index=1, label="Side B", state=SideState.ERROR)
    return AlbumSummary(done=1, error=1, sides=(a, b))


def test_card_renders_per_side_lines_and_heading(qapp):
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    card.render(_summary(with_warnings=False), artist="Miles Davis",
                album="Kind of Blue", destination=Path("/tmp/out"))

    assert "Miles Davis" in card.title_label.text()
    assert "Kind of Blue" in card.title_label.text()

    texts = [label.text() for label in card.side_labels]
    assert any("Side A" in t and "2 tracks" in t and "3:05" in t and "done" in t
               for t in texts), texts
    assert any("Side B" in t and "error" in t for t in texts), texts


def test_warnings_rollup_only_when_a_track_warned(qapp):
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()

    card.render(_summary(with_warnings=False), destination=Path("/tmp/out"))
    assert card.warnings_button is None          # clean run: no roll-up at all

    card.render(_summary(with_warnings=True), destination=Path("/tmp/out"))
    assert card.warnings_button is not None
    assert "1 track carried warnings" in card.warnings_button.text()
    # Collapsed by default; toggling reveals the list. (isVisibleTo sidesteps the
    # top-level window never being shown in an offscreen test.)
    assert not card.warnings_list.isVisibleTo(card)
    card.warnings_button.setChecked(True)
    assert card.warnings_list.isVisibleTo(card)
    assert "Could not embed cover art: boom" in card.warnings_list.text()


def test_open_folder_targets_the_captured_destination(qapp, monkeypatch):
    import gui.summary_card as sc

    captured = {}
    monkeypatch.setattr(sc.QDesktopServices, "openUrl",
                        lambda url: captured.__setitem__("url", url))

    card = sc.AlbumSummaryCard()
    dest = Path.home() / "rrf-out" / "Kind of Blue"
    card.render(_summary(with_warnings=False), destination=dest)

    assert card.open_button.isEnabled()
    card.open_button.click()

    assert "url" in captured, "open-folder did not fire"
    assert captured["url"].toLocalFile() == sc.QUrl.fromLocalFile(str(dest)).toLocalFile()


def test_open_folder_disabled_without_a_destination(qapp):
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    card.render(_summary(with_warnings=False), destination=None)
    assert not card.open_button.isEnabled()


def test_dismiss_hides_card_and_calls_back(qapp):
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    called = []
    card.render(_summary(with_warnings=False), destination=Path("/tmp/out"),
                on_dismiss=lambda: called.append(True))
    card.setVisible(True)
    card.dismiss_button.click()
    assert called == [True]
    assert not card.isVisible()


# --------------------------------------------------------------------------- #
# Restoration receipt: shown per side, only when there is something to report
# --------------------------------------------------------------------------- #
def _declick_summary(*, repaired, total) -> AlbumSummary:
    """Side A with a declick tally; Side B with none (declick was off there)."""
    a = SideSummary(
        index=0, label="Side A", state=SideState.DONE, track_count=2,
        total_bytes=2_500_000, duration_s=185.0,
        declick_repaired_samples=repaired, declick_total_samples=total,
    )
    b = SideSummary(index=1, label="Side B", state=SideState.DONE, track_count=3,
                    total_bytes=3_000_000, duration_s=200.0)
    return AlbumSummary(done=2, error=0, sides=(a, b))


def test_restoration_line_renders_for_the_side_that_has_a_tally(qapp):
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    card.render(_declick_summary(repaired=1015, total=132300), artist="Miles Davis",
                album="Kind of Blue", destination=Path("/tmp/out"))

    texts = [label.text() for label in card.restoration_labels]
    assert len(texts) == 1, texts          # Side B had no declick, so no line
    assert "1,015" in texts[0] and "132,300" in texts[0]
    assert "0.77%" in texts[0]
    # The label must say samples. adeclick counts samples containing clicks, not
    # clicks; calling 1,015 "clicks" would overstate it fivefold on this capture.
    assert "samples" in texts[0]
    assert "clicks repaired" not in texts[0]


def test_no_restoration_line_when_nothing_was_repaired_or_declick_was_off(qapp):
    """Zero, absent and unparsed all render as silence -- never as a '0' receipt."""
    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    card.render(_declick_summary(repaired=0, total=132300), artist="A", album="B")
    assert card.restoration_labels == []

    card.render(_declick_summary(repaired=None, total=None), artist="A", album="B")
    assert card.restoration_labels == []
