"""9.7 -- auto-mapping sides, and a clean slate between albums.

Part A drives the real mapping table (re-entrant proposal, the confidence ladder
through the GUI). Part B drives an album to completion with a stubbed pipeline and
inspects the between-albums reset, the folder policies, and the Run-again do-over.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.album import SideState
from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo

SR = 44100


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _wav(path, seconds=3.0):
    sf.write(str(path), np.zeros(int(SR * seconds), dtype="float32"), SR, subtype="PCM_16")
    return path


def _release():
    """Two sides: A = 2 x 3000 ms (6 s total), B = 2 x 2000 ms (4 s total)."""
    def track(pos, ms):
        return TrackInfo(position=pos, number=str(pos), title=f"t{pos}", length_ms=ms)

    return ReleaseDetail("r", "Kind of Blue", "Miles Davis", media=(
        MediumInfo(1, "Vinyl", tracks=(track(1, 3000), track(2, 3000))),
        MediumInfo(2, "Vinyl", tracks=(track(1, 2000), track(2, 2000))),
    ))


def _tab(qapp):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr.settings.config.filename_side_letters = False
    return fr


# --------------------------------------------------------------------------- #
# Part A -- auto-mapping through the mapping table
# --------------------------------------------------------------------------- #
def test_lookup_after_scan_triggers_auto_mapping(qapp, tmp_path):
    """The reported gap: a release looked up AFTER scanning must still match.

    Numeric filenames give no side hint, so before the release everything is on
    skip; applying the release makes count-and-order map them in order.
    """
    fr = _tab(qapp)
    src = tmp_path / "src"
    src.mkdir()
    fr._album_wavs = [_wav(src / "01.wav"), _wav(src / "02.wav")]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [None, None]           # no sides yet -> all skip

    fr._apply_release(_release())                      # lookup second
    assert fr._album_mapping == [0, 1]                 # mapped without touching the table


def test_hand_set_rows_survive_a_re_proposal(qapp, tmp_path):
    fr = _tab(qapp)
    fr._apply_release(_release())
    src = tmp_path / "src"
    src.mkdir()
    fr._album_wavs = [_wav(src / "SideA.wav"), _wav(src / "SideB.wav")]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, 1]

    # The user deliberately sets SideA's row back to skip.
    fr.mapping_table.cellWidget(0, 1).setCurrentIndex(0)
    assert fr._album_mapping[0] is None

    fr._rebuild_mapping_table()                        # a re-proposal must respect it
    assert fr._album_mapping == [None, 1]


def test_duration_match_maps_a_badly_named_file_with_a_tooltip(qapp, tmp_path):
    fr = _tab(qapp)
    fr._apply_release(_release())                      # side B total = 4 s
    src = tmp_path / "src"
    src.mkdir()
    # SideA maps by name; the 4-second "unknown" matches side B by duration alone.
    fr._album_wavs = [_wav(src / "SideA.wav", 6.0), _wav(src / "unknown.wav", 4.0)]
    fr._rebuild_mapping_table()

    assert fr._album_mapping == [0, 1]
    tip = fr.mapping_table.cellWidget(1, 1).toolTip()
    assert "Duration matches Side B" in tip


def test_single_wav_single_side_maps_without_asking(qapp, tmp_path):
    fr = _tab(qapp)
    single = ReleaseDetail("r", "Single", "A", media=(
        MediumInfo(1, "Vinyl", tracks=(TrackInfo(1, "1", "only", 180000),)),))
    fr._apply_release(single)
    fr._album_wavs = [_wav(tmp_path / "whatever.wav")]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0]


# --------------------------------------------------------------------------- #
# Part B -- clean slate between albums
# --------------------------------------------------------------------------- #
def _drain(qapp, predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_album(qapp, fr, tmp_path, monkeypatch):
    """Drive a two-side album to completion with a stubbed pipeline."""
    monkeypatch.setattr(type(fr), "_album_analyze", lambda self, side, cancel: "analysis")
    monkeypatch.setattr(type(fr), "_album_encode", lambda self, side, cancel: None)
    fr._apply_release(_release())
    src = tmp_path / "src"
    src.mkdir()
    fr._album_wavs = [_wav(src / "SideA.wav"), _wav(src / "SideB.wav")]
    fr._rebuild_mapping_table()
    out = tmp_path / "out"
    out.mkdir()
    fr.output_edit.setText(str(out))
    fr._start_album()
    assert fr._album is not None

    def done():
        album = fr._album
        if album is not None:
            for s in album.sides:
                if s.state == SideState.READY:
                    album.accept_side(s.index, [1.0], list(s.titles))
        return fr._album is None

    assert _drain(qapp, done)
    return out


def test_conclusion_clears_identity_unconditionally(qapp, tmp_path, monkeypatch):
    fr = _tab(qapp)
    _run_album(qapp, fr, tmp_path, monkeypatch)

    # Identity is gone -- nothing to mistag the next record with.
    assert fr.artist_edit.text() == ""
    assert fr.album_edit.text() == ""
    assert fr._release is None
    assert fr._cover is None
    assert fr._sides == []
    assert fr._album_wavs == []
    assert fr._album_mapping == []
    assert fr.mapping_table.rowCount() == 0
    assert fr._flat_titles == []


@pytest.mark.parametrize("policy,default,expected", [
    ("keep", "D:/defaults/out", "KEEP_ORIGINAL"),
    ("reset", "D:/defaults/out", "D:/defaults/out"),
    ("clear", "D:/defaults/out", ""),
])
def test_output_folder_policy(qapp, tmp_path, monkeypatch, policy, default, expected):
    fr = _tab(qapp)
    fr.settings.config.output_post_album_policy = policy
    fr.settings.config.default_output_dir = default
    out = _run_album(qapp, fr, tmp_path, monkeypatch)

    want = str(out) if expected == "KEEP_ORIGINAL" else expected
    assert fr.output_edit.text() == want
    # The configured default is never touched by a reset.
    assert fr.settings.config.default_output_dir == default
    assert fr.settings.config.output_post_album_policy == policy


@pytest.mark.parametrize("policy,default,expected", [
    ("keep", "D:/defaults/src", "KEEP_ORIGINAL"),
    ("reset", "D:/defaults/src", "D:/defaults/src"),
    ("clear", "D:/defaults/src", ""),
])
def test_source_folder_policy(qapp, tmp_path, monkeypatch, policy, default, expected):
    fr = _tab(qapp)
    fr.settings.config.source_post_album_policy = policy
    fr.settings.config.default_source_dir = default
    original_browse = fr._browse_start = "C:/original/src"
    _run_album(qapp, fr, tmp_path, monkeypatch)

    want = original_browse if expected == "KEEP_ORIGINAL" else expected
    assert fr._browse_start == want
    assert fr.settings.config.default_source_dir == default   # default untouched


def test_summary_card_survives_the_reset(qapp, tmp_path, monkeypatch):
    """The card copies what it needs at render time, so the identity reset that
    runs right after conclusion cannot blank it out."""
    fr = _tab(qapp)
    _run_album(qapp, fr, tmp_path, monkeypatch)
    qapp.processEvents()

    assert fr.summary_card.isVisibleTo(fr)
    assert "Kind of Blue" in fr.summary_card.title_label.text()   # still there post-reset
    assert fr._release is None                                    # ...even though identity cleared


def test_run_again_restores_identity_and_mapping(qapp, tmp_path, monkeypatch):
    fr = _tab(qapp)
    out = _run_album(qapp, fr, tmp_path, monkeypatch)
    assert fr._release is None                     # cleared on conclusion

    fr._run_album_again()

    assert fr.artist_edit.text() == "Miles Davis"  # restored
    assert fr.album_edit.text() == "Kind of Blue"
    assert fr._release is not None
    assert len(fr._sides) == 2
    assert [Path(w).name for w in fr._album_wavs] == ["SideA.wav", "SideB.wav"]
    assert fr._album_mapping == [0, 1]
    assert fr.output_edit.text() == str(out)
    # ...and Start runs the restored album.
    fr._start_album()
    assert fr._album is not None
    fr._cancel_album()
    assert _drain(qapp, lambda: fr._album is None)
