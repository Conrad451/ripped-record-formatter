"""Album orchestration: mapping heuristics, state machine, isolation, pipelining."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core.album import (
    AlbumController,
    SideJob,
    SideState,
    guess_side_index,
    propose_wav_side_map,
    sides_from_proposal,
)


def _wait_until(predicate, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# Mapping heuristics
# --------------------------------------------------------------------------- #
def test_guess_side_index_patterns():
    assert guess_side_index("SideA.wav") == 0
    assert guess_side_index("side_b.wav") == 1
    assert guess_side_index("side-2.wav") == 1
    assert guess_side_index("A.wav") == 0
    assert guess_side_index("B.wav") == 1
    assert guess_side_index("01 - Side 1.wav") == 0
    assert guess_side_index("random_name.wav") is None


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def _controller(sides, analyze, encode, on_change=None, **kw):
    return AlbumController(sides, analyze, encode, on_change, **kw)


# --------------------------------------------------------------------------- #
# Folder-first mapping: one row per WAV, ambiguity is never guessed
# --------------------------------------------------------------------------- #


def test_proposal_maps_only_confident_side_names():
    wavs = ["SideA.wav", "SideB.wav"]
    assert propose_wav_side_map(wavs, 2) == [0, 1]


def test_proposal_leaves_ambiguous_files_unmapped():
    """A file with no side hint is skipped, never guessed into a free slot.

    This is the mixed-folder case: bonus.wav and interview.wav belong to some
    other album, and the old sorted-order fallback would have mapped them.
    """
    wavs = ["SideA.wav", "bonus.wav", "SideB.wav", "interview.wav"]
    assert propose_wav_side_map(wavs, 2) == [0, None, 1, None]

    # Nothing at all recognisable -> everything skipped.
    assert propose_wav_side_map(["track01.wav", "track02.wav"], 2) == [None, None]


def test_proposal_stronger_hint_wins_a_contested_side():
    """"SideA" beats a bare "A"; the loser is left for the user, not bumped."""
    wavs = ["A_bonus.wav", "SideA.wav"]
    assert propose_wav_side_map(wavs, 2) == [None, 0]


def test_skipped_rows_are_excluded_from_the_job():
    """Only mapped rows become sides -- a folder of two albums yields one job."""
    wavs = ["SideA.wav", "other_album_sideA.wav", "SideB.wav", "notes.wav"]
    proposal = [0, None, 1, None]          # user left the foreign rows on "skip"

    sides = sides_from_proposal(wavs, proposal)

    assert sides == {0: Path("SideA.wav"), 1: Path("SideB.wav")}
    assert Path("notes.wav") not in sides.values()
    assert Path("other_album_sideA.wav") not in sides.values()
    assert len(sides) == 2                 # two sides, not four


def test_happy_path_state_sequence():
    side = SideJob(0, "A", Path("a.wav"))
    seq: list[SideState] = []
    ctrl = _controller([side], lambda s, c: "analysis", lambda s, c: None,
                       on_change=lambda s: seq.append(s.state))
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.READY)
    ctrl.accept_side(0, [10.0], ["t1"])
    assert _wait_until(lambda: side.state == SideState.DONE)
    ctrl.shutdown(wait=True)
    assert seq == [SideState.ANALYZING, SideState.READY, SideState.ACCEPTED,
                   SideState.ENCODING, SideState.DONE]


def test_unmapped_side_errors_without_stopping_others():
    sides = [SideJob(0, "A", None), SideJob(1, "B", Path("b.wav"))]
    ctrl = _controller(sides, lambda s, c: "ok", lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: sides[0].state == SideState.ERROR)
    assert _wait_until(lambda: sides[1].state == SideState.READY)
    assert "no WAV" in sides[0].error
    ctrl.shutdown(wait=True)


def test_one_side_analysis_failure_is_isolated():
    sides = [SideJob(0, "A", Path("a.wav")), SideJob(1, "B", Path("b.wav"))]

    def analyze(side, should_cancel):
        if side.index == 0:
            raise ValueError("bad WAV")
        return "ok"

    ctrl = _controller(sides, analyze, lambda s, c: None, max_analysis_workers=1)
    ctrl.start()
    assert _wait_until(lambda: sides[0].state == SideState.ERROR)
    assert _wait_until(lambda: sides[1].state == SideState.READY)
    assert "bad WAV" in sides[0].error
    ctrl.shutdown(wait=True)


def test_cancel_all_marks_waiting_sides_cancelled():
    sides = [SideJob(0, "A", Path("a.wav")), SideJob(1, "B", Path("b.wav"))]
    gate = threading.Event()
    ctrl = _controller(sides, lambda s, c: (gate.wait(2), "ok")[1], lambda s, c: None)
    ctrl.start()
    _wait_until(lambda: sides[0].state == SideState.ANALYZING)
    ctrl.cancel_all()
    gate.set()
    assert _wait_until(lambda: all(s.state == SideState.CANCELLED for s in sides))
    ctrl.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# Pipelining: side 2 analyses while side 1 is in review.
# --------------------------------------------------------------------------- #
def test_pipelining_overlaps_analysis_with_review():
    sides = [SideJob(0, "A", Path("a.wav")), SideJob(1, "B", Path("b.wav"))]
    started = {0: threading.Event(), 1: threading.Event()}
    release = {0: threading.Event(), 1: threading.Event()}

    def analyze(side, should_cancel):
        started[side.index].set()
        release[side.index].wait(3)
        return side.index

    ctrl = _controller(sides, analyze, lambda s, c: None, max_analysis_workers=1)
    ctrl.start()

    assert started[0].wait(3)                       # side 0 analysing
    release[0].set()                                 # let it finish -> READY
    assert _wait_until(lambda: sides[0].state == SideState.READY)

    # With side 0 sitting in review (READY, not accepted), side 1's analysis
    # has already begun -- the machine works while the user thinks.
    assert started[1].wait(3)
    assert sides[0].state == SideState.READY
    assert sides[1].state == SideState.ANALYZING

    release[1].set()
    ctrl.shutdown(wait=True)
