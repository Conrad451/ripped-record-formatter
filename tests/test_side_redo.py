"""Re-doing one finished side: a tool that shows its work accepts appeals.

The stakeholder's case, which this file is the acceptance test for: Discovery
ripped end to end, and Side B was accepted with four tracks when the record has
five. A split was missed and Accept locked the door. Every option was wrong --
Re-tag cannot split a FLAC, "Run this album again" re-does the side that was
already right, and "Retry side" was scoped to sides that had *errored*, so it
sat disabled for the entire session.

The raw WAVs survive by design. This is the door back in.

Drives the real FullRipTab wiring with a stubbed analysis/encode pipeline --
the lifecycle is what is under test, not the DSP.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

from core.album import SideState
from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _discovery():
    """Two sides. Side B has five tracks -- the one the splitter got wrong."""
    def track(pos, title):
        return TrackInfo(position=pos, number=str(pos), title=title, length_ms=3000)

    return ReleaseDetail(
        release_id="discovery", title="Discovery", artist="Daft Punk",
        media=(
            MediumInfo(1, "Vinyl", tracks=(track(1, "One More Time"),
                                           track(2, "Aerodynamic"),
                                           track(3, "Digital Love"))),
            MediumInfo(2, "Vinyl", tracks=(track(1, "Harder Better Faster"),
                                           track(2, "Crescendolls"),
                                           track(3, "Nightvision"),
                                           track(4, "Superheroes"),
                                           track(5, "High Life"))),
        ),
    )


def _tab(qapp, tmp_path, monkeypatch, encode=None):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr.settings.config.filename_side_letters = False
    fr._apply_release(_discovery())

    src = tmp_path / "src"
    src.mkdir()
    a, b = src / "SideA.wav", src / "SideB.wav"
    a.write_bytes(b"")
    b.write_bytes(b"")

    fr._album_wavs = [a, b]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, 1]

    out = tmp_path / "out"
    out.mkdir()
    fr.output_edit.setText(str(out))

    monkeypatch.setattr(type(fr), "_album_analyze", lambda self, side, cancel: "analysis")
    monkeypatch.setattr(type(fr), "_album_encode",
                        encode or (lambda self, side, cancel: None))
    return fr, out, (a, b)


def _drain(qapp, predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_to_completion(qapp, fr):
    fr._start_album()
    assert fr._album is not None
    accepted = set()

    def done():
        album = fr._album
        if album is not None:
            for side in album.sides:
                if side.state == SideState.READY and side.index not in accepted:
                    album.accept_side(side.index, [1.0], list(side.titles))
                    accepted.add(side.index)
        return fr._album is None

    assert _drain(qapp, done), "the album never concluded"


# --------------------------------------------------------------------------- #
# The affordance
# --------------------------------------------------------------------------- #
def test_the_receipt_offers_a_re_do_on_every_side(qapp, tmp_path, monkeypatch):
    """A receipt is where you find out the record came out wrong, so it is
    where the appeal belongs."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    # isHidden reflects explicit visibility even when no top-level window is shown.
    assert not fr.summary_card.isHidden()
    assert set(fr.summary_card.redo_buttons) == {0, 1}, "each side needs its own way back"
    for button in fr.summary_card.redo_buttons.values():
        assert button.isEnabled()


def test_a_finished_side_can_be_re_done_while_the_other_is_left_alone(
        qapp, tmp_path, monkeypatch):
    """The whole point: Side B is wrong, Side A is not, and re-running the
    album would have re-done both."""
    written: list[tuple[int, float]] = []

    def encode(self, side, cancel):
        path = self._album_output_root
        marker = tmp_path / "out" / f"side{side.index}.flac"
        marker.write_text(f"{side.index}", encoding="utf-8")
        written.append((side.index, marker.stat().st_mtime_ns))
        return None

    fr, out, _wavs = _tab(qapp, tmp_path, monkeypatch, encode=encode)
    _run_to_completion(qapp, fr)
    assert len(written) == 2

    side_a = out / "side0.flac"
    before = side_a.stat().st_mtime_ns
    before_bytes = side_a.read_bytes()

    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)
    assert fr._album is not None, "the re-do did not start a job"
    assert fr._redoing_side == 1

    def done():
        album = fr._album
        if album is not None:
            for side in album.sides:
                if side.state == SideState.READY:
                    album.accept_side(side.index, [1.0], list(side.titles))
        return fr._album is None

    assert _drain(qapp, done), "the re-do never concluded"

    # Side B was written again; Side A was not touched.
    assert [i for i, _ in written].count(1) == 2
    assert [i for i, _ in written].count(0) == 1
    assert side_a.stat().st_mtime_ns == before
    assert side_a.read_bytes() == before_bytes


def test_the_re_do_job_carries_only_the_target_side(qapp, tmp_path, monkeypatch):
    """A scoped job, not a resurrected album."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)

    assert fr._album is not None
    assert [s.index for s in fr._album.sides] == [1]
    assert fr._album.sides[0].label == "Side B"
    fr._album.cancel_all()


def test_the_overwrite_prompt_is_scoped_to_the_side_being_re_done(
        qapp, tmp_path, monkeypatch):
    """One ask, and it must not threaten the other side's files."""
    fr, out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    asked: list = []
    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: (asked.append(only_side), True)[1])
    fr._redo_side_from_card(1)

    assert asked == [1], "the overwrite question was not scoped to this side"
    if fr._album is not None:
        fr._album.cancel_all()


def test_the_planned_names_for_a_re_do_cover_only_that_side(qapp, tmp_path, monkeypatch):
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)

    everything = fr._planned_filenames()
    just_b = fr._planned_filenames(only_side=1)

    assert len(everything) == 8            # three on A, five on B
    assert len(just_b) == 5
    assert all("Harder" in n or "Crescendolls" in n or "Nightvision" in n
               or "Superheroes" in n or "High Life" in n for n in just_b)


# --------------------------------------------------------------------------- #
# One review area, one job
# --------------------------------------------------------------------------- #
def test_starting_an_album_during_a_re_do_is_refused_in_plain_words(
        qapp, tmp_path, monkeypatch):
    """The simpler rule wins, and it explains itself rather than crashing or
    silently discarding the re-do."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)
    assert fr._album is not None

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    fr._start_album()

    blocked = [m for m in logged if "re-do first" in m]
    assert blocked, f"the second job was not refused with an explanation: {logged}"
    assert "Side B" in blocked[0]
    assert "one job at a time" in blocked[0]
    fr._album.cancel_all()


# --------------------------------------------------------------------------- #
# Retry and re-do are one gesture (item 3)
# --------------------------------------------------------------------------- #
def test_the_re_do_button_is_live_for_a_finished_side(qapp, tmp_path, monkeypatch):
    """It used to be enabled only for errored sides, which meant it was greyed
    out for an entire successful session -- and a control disabled all session
    reads as broken rather than as inapplicable."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    fr._start_album()
    assert _drain(qapp, lambda: all(s.state == SideState.READY for s in fr._album.sides))

    for side in list(fr._album.sides):
        fr._album.accept_side(side.index, [1.0], list(side.titles))
    assert _drain(qapp, lambda: fr._album is None or
                  all(s.state == SideState.DONE for s in fr._album.sides))

    assert fr.retry_side_btn.text() == "Re-do side"


def test_the_controller_re_runs_a_done_side(qapp, tmp_path, monkeypatch):
    """core-level: DONE is re-runnable, CANCELLED deliberately is not."""
    from core.album import AlbumController, SideJob

    jobs = [SideJob(index=0, label="Side A", wav_path=tmp_path / "a.wav",
                    titles=["x"], durations_ms=[1000])]
    controller = AlbumController(
        jobs, lambda side, cancel: "analysis", lambda side, cancel: None,
        max_analysis_workers=1, max_encode_workers=1)
    try:
        side = controller.sides[0]
        side.state = SideState.DONE
        side.result = object()

        assert controller.retry_side(0) is True
        assert side.result is None, "the stale receipt survived the re-run"

        side.state = SideState.CANCELLED
        assert controller.retry_side(0) is False, "a cancelled side was resurrected"
    finally:
        controller.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# Identity (item 4)
# --------------------------------------------------------------------------- #
def test_a_re_do_inherits_the_release_without_a_fresh_lookup(
        qapp, tmp_path, monkeypatch):
    """The clean slate clears identity when an album ends; the re-do restores it
    from the snapshot rather than making the user look the record up again."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    assert fr._release is None, "the clean slate should have cleared identity"

    lookups: list = []
    monkeypatch.setattr(type(fr), "_open_lookup", lambda self: lookups.append(True))
    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)

    assert lookups == [], "a re-do asked for a lookup it did not need"
    assert fr._release is not None
    assert fr._release.title == "Discovery"
    # ...and the titles the re-encode will write are this side's, in order.
    assert fr._album.sides[0].titles == [
        "Harder Better Faster", "Crescendolls", "Nightvision",
        "Superheroes", "High Life"]
    fr._album.cancel_all()


def test_a_re_do_without_identity_refuses_to_write_untitled_tracks(
        qapp, tmp_path, monkeypatch):
    """The amendment: correctly split but silently untagged is not an acceptable
    appeal. It trades a visible problem for an invisible one."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    # The state a restart leaves behind: audio and destination known, tracklist
    # and cover gone.
    fr._rerun_snapshot["release"] = None
    fr._rerun_snapshot["cover"] = None

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    answered: list = []
    monkeypatch.setattr(type(fr), "_offer_lookup_before_redo",
                        lambda self, letter: (answered.append(letter), False)[1])
    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)

    fr._redo_side_from_card(1)

    assert answered == ["B"], "the user was never asked"
    assert fr._album is None, "it went ahead without tags anyway"
    warned = [m for m in logged if "tracklist and cover" in m]
    assert warned, f"the missing identity was never explained: {logged}"
    assert "without titles or cover" in warned[0]


def test_a_missing_source_wav_is_refused_with_a_reason(qapp, tmp_path, monkeypatch):
    """The WAVs are the master. If one is gone, say so rather than failing
    somewhere deeper."""
    fr, _out, wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    wavs[1].unlink()
    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    fr._redo_side_from_card(1)

    assert fr._album is None
    assert any("not where it was" in m for m in logged), logged
    assert any("raw WAVs are the master" in m for m in logged), logged


# --------------------------------------------------------------------------- #
# The receipt after a re-do
# --------------------------------------------------------------------------- #
def test_the_card_still_describes_the_whole_record_after_a_re_do(
        qapp, tmp_path, monkeypatch):
    """The card describes a record, not a job. After re-doing Side B it must
    still list Side A -- shrinking to the one side just re-done would lose the
    other, which is the opposite of what a re-do is for."""
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    before = fr._rerun_snapshot["summary"]
    assert len(before.sides) == 2

    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)

    def done():
        album = fr._album
        if album is not None:
            for side in album.sides:
                if side.state == SideState.READY:
                    album.accept_side(side.index, [1.0], list(side.titles))
        return fr._album is None

    assert _drain(qapp, done)

    after = fr._rerun_snapshot["summary"]
    assert len(after.sides) == 2, "the receipt shrank to the side just re-done"
    assert {s.index for s in after.sides} == {0, 1}
    assert not fr.summary_card.isHidden()


def test_a_re_do_is_reported_as_a_side_not_an_album(qapp, tmp_path, monkeypatch):
    fr, _out, _wavs = _tab(qapp, tmp_path, monkeypatch)
    _run_to_completion(qapp, fr)

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    monkeypatch.setattr(type(fr), "_confirm_overwrite",
                        lambda self, d, only_side=None: True)
    fr._redo_side_from_card(1)

    started = [m for m in logged if "re-doing Side B" in m]
    assert started, logged
    assert "left alone" in started[0]
    if fr._album is not None:
        fr._album.cancel_all()
