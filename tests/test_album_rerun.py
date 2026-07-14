"""Album lifecycle in the GUI: finishing, re-running, and where the FLACs go.

The stakeholder's incident, in one file. An album that reached "all sides done"
never concluded: the tab kept the spent controller, so Start answered "Album:
already running." forever and the only way to re-run was to restart the app. The
reason they wanted to re-run is the other half of it -- the output folder was
wrong, they corrected the field, and it changed nothing, because the encode uses
the folder captured when Start was pressed.

These drive the real FullRipTab wiring with a fake AlbumController pipeline
(analysis and encode are stubbed -- this is about the lifecycle, not the DSP).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

from core.album import AlbumSummary, SideState
from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _release():
    def track(pos, title):
        return TrackInfo(position=pos, number=str(pos), title=title, length_ms=3000)

    return ReleaseDetail(
        release_id="r", title="Kind of Blue", artist="Miles Davis",
        media=(
            MediumInfo(1, "Vinyl", tracks=(track(1, "So What"),
                                           track(2, "Freddie Freeloader"))),
            MediumInfo(2, "Vinyl", tracks=(track(1, "Blue in Green"),
                                           track(2, "Flamenco Sketches"))),
        ),
    )


def _tab(qapp, tmp_path, monkeypatch, encode=None):
    """A FullRipTab with a mapped two-side album and a stubbed pipeline."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr.settings.config.filename_side_letters = False
    fr._apply_release(_release())

    src = tmp_path / "src"
    src.mkdir()
    a = src / "SideA.wav"
    b = src / "SideB.wav"
    a.write_bytes(b"")
    b.write_bytes(b"")

    fr._album_wavs = [a, b]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, 1]

    out = tmp_path / "out"
    out.mkdir()
    fr.output_edit.setText(str(out))

    # Stub the pipeline: no DSP, no ffmpeg. The lifecycle is what is under test.
    monkeypatch.setattr(type(fr), "_album_analyze", lambda self, side, cancel: "analysis")
    monkeypatch.setattr(type(fr), "_album_encode",
                        encode or (lambda self, side, cancel: None))
    return fr, out


def _drain(qapp, predicate, timeout=8.0):
    """Wait for `predicate`, pumping the event loop.

    Necessary here and not in the other album tests: the controller announces
    completion from a pool thread through a queued Qt signal, so it is only
    delivered when the main thread actually runs its event loop.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_to_completion(qapp, fr):
    """Start, accept every side as it readies, and wait for the album to conclude."""
    fr._start_album()
    assert fr._album is not None
    sides = list(fr._album.sides)
    accepted = set()

    def done():
        album = fr._album
        if album is not None:
            for side in album.sides:
                if side.state == SideState.READY and side.index not in accepted:
                    album.accept_side(side.index, [1.0], list(side.titles))
                    accepted.add(side.index)
        return fr._album is None            # the tab released it: the job is over

    assert _drain(qapp, done), [(s.label, s.state) for s in sides]
    return sides


# --------------------------------------------------------------------------- #
# The album concludes, and Start means Start again
# --------------------------------------------------------------------------- #
def test_completion_releases_the_controller_and_re_arms_start(qapp, tmp_path, monkeypatch):
    fr, _out = _tab(qapp, tmp_path, monkeypatch)

    sides = _run_to_completion(qapp, fr)

    assert all(s.state == SideState.DONE for s in sides)
    assert fr._album is None                       # fully released
    assert fr.start_album_btn.isEnabled()          # ...and armed
    assert not fr.cancel_album_btn.isEnabled()


def test_the_completed_album_logs_one_summary_line(qapp, tmp_path, monkeypatch):
    fr, _out = _tab(qapp, tmp_path, monkeypatch)
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    _run_to_completion(qapp, fr)
    qapp.processEvents()

    assert "Album complete: 2 sides done." in logged
    assert sum(1 for m in logged if m.startswith("Album complete:")) == 1


def test_a_finished_album_shows_the_summary_card_and_start_dismisses_it(
        qapp, tmp_path, monkeypatch):
    """The receipt occupies the idle review space alone, and a fresh Start
    steps it aside without being blocked by it."""
    fr, _out = _tab(qapp, tmp_path, monkeypatch)

    _run_to_completion(qapp, fr)
    qapp.processEvents()

    # Up, and the sole occupant of the review space.
    assert fr.summary_card.isVisibleTo(fr)
    assert not fr.empty_state.isVisibleTo(fr)
    assert not fr.review_box.isVisibleTo(fr)
    assert "Kind of Blue" in fr.summary_card.title_label.text()

    # A re-run is not blocked by the card, and dismisses it.
    fr._start_album()
    assert fr._album is not None
    assert not fr.summary_card.isVisibleTo(fr)

    fr._cancel_album()
    assert _drain(qapp, lambda: fr._album is None)


def test_pressing_start_again_runs_a_fresh_job_not_already_running(qapp, tmp_path,
                                                                   monkeypatch):
    """The bug, exactly: a finished album used to answer 'already running.'"""
    fr, _out = _tab(qapp, tmp_path, monkeypatch)
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    first = _run_to_completion(qapp, fr)
    assert all(s.state == SideState.DONE for s in first)

    logged.clear()
    second = _run_to_completion(qapp, fr)

    assert "Album: already running." not in logged
    # A genuinely new job: new SideJob objects, re-analysed from the WAVs.
    assert all(s.state == SideState.DONE for s in second)
    assert all(new is not old for new, old in zip(second, first))


def test_a_side_done_last_time_is_analysed_again_not_skipped(qapp, tmp_path,
                                                             monkeypatch):
    """Re-run is start-over: the WAVs may have changed, so assume nothing."""
    analysed: list[int] = []
    fr, _out = _tab(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(type(fr), "_album_analyze",
                        lambda self, side, cancel: (analysed.append(side.index),
                                                    "analysis")[1])

    _run_to_completion(qapp, fr)
    assert sorted(analysed) == [0, 1]

    _run_to_completion(qapp, fr)
    # Both sides analysed a second time -- nothing was skipped for being done.
    assert sorted(analysed) == [0, 0, 1, 1]


def test_cancelling_also_concludes_and_re_arms(qapp, tmp_path, monkeypatch):
    """Cancel already implied this arming; completion had to mean it too."""
    fr, _out = _tab(qapp, tmp_path, monkeypatch)
    fr._start_album()
    assert fr._album is not None

    fr._cancel_album()
    assert _drain(qapp, lambda: fr._album is None)
    assert fr.start_album_btn.isEnabled()


# --------------------------------------------------------------------------- #
# Destination: captured at Start, and visibly so
# --------------------------------------------------------------------------- #
def test_the_destination_is_frozen_while_the_album_runs(qapp, tmp_path, monkeypatch):
    fr, out = _tab(qapp, tmp_path, monkeypatch)

    assert fr.output_edit.isEnabled()
    assert not fr.destination_label.isVisible()

    fr._start_album()

    # Frozen, and the captured destination is on screen -- the field and the
    # encode can no longer disagree, because the field cannot move.
    assert not fr.output_edit.isEnabled()
    assert "fixed while an album is running" in fr.output_edit.toolTip()
    assert str(out) in fr.destination_label.text()
    assert fr._album_output_root == str(out)

    fr._cancel_album()
    assert _drain(qapp, lambda: fr._album is None)
    assert fr.output_edit.isEnabled()              # released again on completion


def test_the_encode_uses_the_captured_root_not_the_live_field(qapp, tmp_path,
                                                              monkeypatch):
    """Editing the field mid-run cannot redirect a side. It is disabled -- but
    even forced, the encode reads the captured root."""
    seen: list[str] = []

    def encode(self, side, cancel):
        seen.append(self._album_output_root)

    fr, out = _tab(qapp, tmp_path, monkeypatch, encode=encode)
    fr._start_album()

    # Force the field to somewhere else, as a user could not.
    fr.output_edit.setText(str(tmp_path / "elsewhere"))

    sides = list(fr._album.sides)
    accepted = set()

    def done():
        album = fr._album
        if album is not None:
            for s in album.sides:
                if s.state == SideState.READY and s.index not in accepted:
                    album.accept_side(s.index, [1.0], list(s.titles))
                    accepted.add(s.index)
        return fr._album is None

    assert _drain(qapp, done), [(s.label, s.state) for s in sides]
    assert seen == [str(out), str(out)]           # both sides: the captured root


# --------------------------------------------------------------------------- #
# Overwrite: refuse by default, ask once, at album granularity
# --------------------------------------------------------------------------- #
def test_existing_files_prompt_once_and_cancel_refuses_to_start(qapp, tmp_path,
                                                                monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    fr, out = _tab(qapp, tmp_path, monkeypatch)
    (out / "[01] - So What.flac").write_bytes(b"old")
    (out / "[03] - Blue in Green.flac").write_bytes(b"old")

    asked: list[str] = []
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    def refuse(parent, title, text, *a, **k):
        asked.append(text)
        return QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(QMessageBox, "question", staticmethod(refuse))

    fr._start_album()

    assert len(asked) == 1                        # once per job, not per file
    assert "2 file(s) already exist" in asked[0]
    assert fr._album is None                      # refused: nothing started
    assert (out / "[01] - So What.flac").read_bytes() == b"old"   # untouched
    assert any("were left alone" in m for m in logged)


def test_confirming_the_prompt_starts_the_album(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    fr, out = _tab(qapp, tmp_path, monkeypatch)
    (out / "[01] - So What.flac").write_bytes(b"old")

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr._start_album()

    assert fr._album is not None
    assert any("overwriting 1 existing file(s)" in m for m in logged)


def test_an_empty_destination_never_prompts(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    fr, _out = _tab(qapp, tmp_path, monkeypatch)
    asked: list[int] = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(1)))

    fr._start_album()

    assert asked == []                            # nothing to overwrite, no question
    assert fr._album is not None


def test_planned_filenames_match_what_the_encoder_writes(qapp, tmp_path, monkeypatch):
    """The prompt must describe the files that would actually land."""
    fr, _out = _tab(qapp, tmp_path, monkeypatch)

    assert fr._planned_filenames() == [
        "[01] - So What.flac",
        "[02] - Freddie Freeloader.flac",
        "[03] - Blue in Green.flac",
        "[04] - Flamenco Sketches.flac",
    ]
