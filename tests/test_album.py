"""Album orchestration: mapping heuristics, state machine, isolation, pipelining."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core.album import (
    AlbumController,
    AlbumSummary,
    SideJob,
    SideState,
    SideSummary,
    guess_side_index,
    measure_outputs,
    probe_duration_ms,
    propose_side_map,
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


# --------------------------------------------------------------------------- #
# Auto-mapping: the confidence ladder (propose_side_map)
# --------------------------------------------------------------------------- #
def test_ladder_a_filenames_are_highest_confidence():
    assert propose_side_map(["SideA.wav", "SideB.wav"], 2) == [0, 1]


def test_ladder_b_count_and_order_maps_ordered_names():
    # No side hint, but exactly 2 ordered WAVs for 2 sides -> map in ordinal order.
    assert propose_side_map(["01.wav", "02.wav"], 2) == [0, 1]
    assert propose_side_map(["02.wav", "01.wav"], 2) == [1, 0]   # order is the ordinal


def test_ladder_b_single_wav_single_side_maps_trivially():
    assert propose_side_map(["my recording.wav"], 1) == [0]


def test_ladder_b_count_and_order_needs_matching_counts():
    # 3 WAVs for 2 sides: order does not determine it -> all skip.
    assert propose_side_map(["01.wav", "02.wav", "03.wav"], 2) == [None, None, None]


def test_ladder_c_duration_maps_a_badly_named_file():
    # "bounce.wav" has no side hint and no ordinal, but its length matches side B
    # alone (1200s vs 1180s, within 5%).
    m = propose_side_map(
        ["SideA.wav", "bounce.wav"], 2,
        wav_durations_ms=[600_000, 1_200_000],
        side_totals_ms=[600_000, 1_180_000],
    )
    assert m == [0, 1]


def test_ladder_c_refuses_two_way_ambiguity():
    # One WAV within 5% of BOTH sides -> no single answer, stays on skip.
    m = propose_side_map(
        ["mystery.wav"], 2,
        wav_durations_ms=[1_000_000],
        side_totals_ms=[1_000_000, 1_010_000],
    )
    assert m == [None]


def test_ladder_c_refuses_a_contested_side():
    # Two hint-less WAVs both match side A's length; neither is the clear winner.
    m = propose_side_map(
        ["foo.wav", "bar.wav"], 2,
        wav_durations_ms=[1_000_000, 1_000_000],
        side_totals_ms=[1_000_000, 5_000_000],
    )
    assert m == [None, None]


def test_ladder_hand_set_skip_is_never_refilled():
    # The user set SideA's row to skip; re-proposal must not grab it back.
    m = propose_side_map(
        ["SideA.wav", "SideB.wav"], 2,
        current=[None, None], locked={0},
    )
    assert m == [None, 1]


def test_ladder_only_fills_skip_and_respects_taken_sides():
    # Row 0 hand-set to side B(1); SideA fills the still-free side A(0).
    m = propose_side_map(
        ["SideB.wav", "SideA.wav"], 2,
        current=[1, None], locked={0},
    )
    assert m == [1, 0]


def test_probe_duration_ms_reads_header_only(tmp_path):
    import numpy as np
    import soundfile as sf

    p = tmp_path / "one_sec.wav"
    sf.write(str(p), np.zeros(44100, dtype="float32"), 44100, subtype="PCM_16")
    assert abs(probe_duration_ms(p) - 1000) <= 5
    assert probe_duration_ms(tmp_path / "missing.wav") == 0     # unreadable -> 0


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


# --------------------------------------------------------------------------- #
# Admission: a late-arriving side joins a still-running job (record pipelining)
# --------------------------------------------------------------------------- #
def test_admit_side_queues_a_late_side_into_a_running_job():
    a = SideJob(0, "Side A", Path("a.wav"))
    gate = threading.Event()                      # hold analysis so the job stays open
    ctrl = _controller([a], lambda s, c: (gate.wait(2), "ok")[1], lambda s, c: None,
                       max_analysis_workers=2)
    ctrl.start()
    assert _wait_until(lambda: a.state == SideState.ANALYZING)

    b = SideJob(1, "Side B", Path("b.wav"))
    assert ctrl.admit_side(b) is True
    assert b in ctrl.sides                        # appended to the live list
    assert _wait_until(lambda: b.state == SideState.ANALYZING)   # it started analysing

    gate.set()
    assert _wait_until(lambda: a.state == SideState.READY and b.state == SideState.READY)
    ctrl.shutdown(wait=True)


def test_admit_side_is_refused_once_the_album_has_concluded():
    """A finished album is finished: the late side is not admitted (map + re-run)."""
    a = SideJob(0, "Side A", Path("a.wav"))
    ctrl = _controller([a], lambda s, c: "ok", lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: a.state == SideState.READY)
    ctrl.accept_side(0, [1.0])
    assert _wait_until(lambda: ctrl.finished)     # single side done -> concluded

    b = SideJob(1, "Side B", Path("b.wav"))
    assert ctrl.admit_side(b) is False
    assert b not in ctrl.sides
    ctrl.shutdown(wait=True)


def test_admitting_before_conclusion_defers_the_finish_to_include_the_late_side():
    """The design seam: a job whose existing sides all finish must not conclude
    while a side admitted before conclusion is still in flight."""
    a = SideJob(0, "Side A", Path("a.wav"))
    finished: list = []
    ctrl = AlbumController([a], lambda s, c: "ok", lambda s, c: None,
                           on_finished=finished.append)
    ctrl.start()
    assert _wait_until(lambda: a.state == SideState.READY)

    b = SideJob(1, "Side B", Path("b.wav"))
    assert ctrl.admit_side(b) is True

    ctrl.accept_side(0, [1.0])                     # A completes...
    assert _wait_until(lambda: a.state == SideState.DONE)
    # ...but B is still in flight, so the album has NOT concluded.
    assert finished == []
    assert not ctrl.finished

    assert _wait_until(lambda: b.state == SideState.READY)
    ctrl.accept_side(1, [1.0])
    assert _wait_until(lambda: ctrl.finished)
    assert len(finished) == 1                      # concluded exactly once...
    assert finished[0].total == 2                  # ...counting both sides
    ctrl.shutdown(wait=True)


def test_cancel_all_covers_an_admitted_side():
    a = SideJob(0, "Side A", Path("a.wav"))
    gate = threading.Event()
    ctrl = _controller([a], lambda s, c: (gate.wait(2), "ok")[1], lambda s, c: None,
                       max_analysis_workers=2)
    ctrl.start()
    assert _wait_until(lambda: a.state == SideState.ANALYZING)
    b = SideJob(1, "Side B", Path("b.wav"))
    assert ctrl.admit_side(b) is True

    ctrl.cancel_all()
    gate.set()
    assert _wait_until(lambda: all(s.state == SideState.CANCELLED for s in ctrl.sides))
    assert b.state == SideState.CANCELLED          # the late side was cancelled too
    ctrl.shutdown(wait=True)


def test_admit_side_is_refused_when_the_index_is_already_present():
    a = SideJob(0, "Side A", Path("a.wav"))
    ctrl = _controller([a], lambda s, c: "ok", lambda s, c: None)
    ctrl.start()
    dup = SideJob(0, "Side A again", Path("a2.wav"))
    assert ctrl.admit_side(dup) is False
    assert dup not in ctrl.sides
    ctrl.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# Failures must say why -- and be retryable
# --------------------------------------------------------------------------- #
def test_analysis_failure_records_cause_phase_and_traceback():
    side = SideJob(0, "Side B", Path("b.wav"))

    def analyze(s, c):
        raise PermissionError(13, "Permission denied")

    ctrl = _controller([side], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.ERROR)
    ctrl.shutdown(wait=True)

    assert "PermissionError" in side.error
    assert "Permission denied" in side.error          # the actual cause, not "error"
    assert side.failed_phase == "analysis"
    assert "Traceback" in side.error_detail
    assert "PermissionError" in side.error_detail


def test_encode_failure_records_the_encode_phase():
    side = SideJob(0, "Side A", Path("a.wav"))

    def encode(s, c):
        raise OSError("share went away")

    ctrl = _controller([side], lambda s, c: "ok", encode)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.READY)
    ctrl.accept_side(0, [1.0], ["t"])
    assert _wait_until(lambda: side.state == SideState.ERROR)
    ctrl.shutdown(wait=True)

    assert side.failed_phase == "encode"
    assert "share went away" in side.error


def test_retry_reruns_only_the_failed_side():
    a = SideJob(0, "Side A", Path("a.wav"))
    b = SideJob(1, "Side B", Path("b.wav"))
    attempts = {1: 0}

    def analyze(s, c):
        if s.index == 1:
            attempts[1] += 1
            if attempts[1] == 1:
                raise OSError("share hiccup")     # transient: fails once
        return "ok"

    ctrl = _controller([a, b], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: b.state == SideState.ERROR)
    assert _wait_until(lambda: a.state == SideState.READY)
    assert "share hiccup" in b.error

    # Retry just side B; side A is untouched.
    assert ctrl.retry_side(1) is True
    assert _wait_until(lambda: b.state == SideState.READY)
    ctrl.shutdown(wait=True)

    assert b.error == "" and b.failed_phase == ""   # error cleared on retry
    assert attempts[1] == 2
    assert a.state == SideState.READY               # never disturbed


def test_second_failure_redisplays_the_new_message():
    side = SideJob(0, "Side B", Path("b.wav"))
    calls = {"n": 0}

    def analyze(s, c):
        calls["n"] += 1
        raise OSError(f"failure #{calls['n']}")

    ctrl = _controller([side], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.ERROR)
    assert "failure #1" in side.error

    assert ctrl.retry_side(0) is True
    assert _wait_until(lambda: side.state == SideState.ERROR and "failure #2" in side.error)
    ctrl.shutdown(wait=True)
    assert "failure #2" in side.error     # no retry limit; the new cause is shown


def test_retry_refuses_a_side_that_is_not_errored():
    side = SideJob(0, "A", Path("a.wav"))
    ctrl = _controller([side], lambda s, c: "ok", lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.READY)
    assert ctrl.retry_side(0) is False        # READY is not a failure
    ctrl.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# Guard trips are review requests, not failures
# --------------------------------------------------------------------------- #
def test_guard_trip_parks_needs_attention_and_keeps_the_analysis():
    from core.album import NeedsAttention

    side = SideJob(0, "Side B", Path("b.wav"))
    proposal = {"confirmed": [10.0], "unresolved": ["window-1", "window-2"]}

    def analyze(s, c):
        raise NeedsAttention("expected 4 tracks; only 1 of 3 boundaries confirmed",
                             proposal)

    ctrl = _controller([side], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.NEEDS_ATTENTION)
    ctrl.shutdown(wait=True)

    assert side.state is SideState.NEEDS_ATTENTION
    assert side.state is not SideState.ERROR         # not a failure
    assert side.analysis is proposal                 # ...and the work is NOT discarded
    assert "only 1 of 3 boundaries confirmed" in side.attention
    assert side.error == ""                          # nothing was recorded as an error


def test_real_exception_still_errors_and_is_retryable():
    """I/O and decode failures keep ERROR + Retry, exactly as in v2.0.1."""
    side = SideJob(0, "Side A", Path("a.wav"))

    def analyze(s, c):
        raise PermissionError(13, "Permission denied")

    ctrl = _controller([side], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.ERROR)
    ctrl.shutdown(wait=True)

    assert side.state is SideState.ERROR
    assert side.analysis is None                     # no usable work to keep
    assert "Permission denied" in side.error
    assert side.failed_phase == "analysis"
    assert side.attention == ""


def test_needs_attention_side_is_retryable_and_reviewable():
    from core.album import NeedsAttention

    side = SideJob(0, "Side B", Path("b.wav"))
    calls = {"n": 0}

    def analyze(s, c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NeedsAttention("guard tripped", {"p": 1})
        return {"p": 2}                              # mapping fixed -> clean run

    ctrl = _controller([side], analyze, lambda s, c: None)
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.NEEDS_ATTENTION)

    # Reviewing it is allowed, exactly like a READY side.
    ctrl.mark_resolving(0)
    assert side.state is SideState.RESOLVING

    # And retry is allowed too (the user may have fixed the mapping first).
    side.state = SideState.NEEDS_ATTENTION
    assert ctrl.retry_side(0) is True
    assert _wait_until(lambda: side.state == SideState.READY)
    ctrl.shutdown(wait=True)
    assert side.attention == ""                      # cleared on retry
    assert side.analysis == {"p": 2}


def test_guard_threshold_is_a_parameter():
    from core.split_review import wrong_side_suspected

    # A 6-track side has 5 boundaries; 3 unresolved.
    assert wrong_side_suspected(6, 3, frac=0.5) is True     # 3 > 2.5 -> flagged
    assert wrong_side_suspected(6, 3, frac=0.8) is False    # 3 < 4.0 -> tolerated
    assert wrong_side_suspected(6, 3, frac=0.2) is True
    # Raising it toward 1.0 means being told less often.
    assert wrong_side_suspected(6, 4, frac=0.9) is False
    assert wrong_side_suspected(6, 5, frac=0.9) is True


# --------------------------------------------------------------------------- #
# The album concludes
#
# Every side reaching a terminal state used to mean nothing at the album level:
# the controller had no notion of being over, so the GUI held a spent object
# forever and a completed album answered "already running" to a second Start.
# --------------------------------------------------------------------------- #
def _finishing_controller(n=2, encode=None, on_finished=None, **kw):
    sides = [SideJob(index=i, label=f"Side {i}", wav_path=Path(f"s{i}.wav"))
             for i in range(n)]
    return AlbumController(
        sides,
        analyze_fn=lambda side, cancel: object(),
        encode_fn=encode or (lambda side, cancel: None),
        on_finished=on_finished,
        **kw,
    ), sides


def test_album_is_not_finished_while_a_side_is_still_working():
    album, sides = _finishing_controller()
    assert album.finished is False
    album.start()
    _wait_until(lambda: all(s.state == SideState.READY for s in sides))
    # Analysis is done, but nobody has accepted anything: the album is not over.
    assert album.finished is False


def test_every_side_done_finishes_the_album_and_summarises():
    seen = []
    album, sides = _finishing_controller(on_finished=seen.append)
    album.start()
    _wait_until(lambda: all(s.state == SideState.READY for s in sides))
    for s in sides:
        album.accept_side(s.index, [1.0])

    assert _wait_until(lambda: album.finished), "album never concluded"
    assert _wait_until(lambda: len(seen) == 1), "on_finished did not fire"
    assert all(s.state == SideState.DONE for s in sides)

    summary = seen[0]
    assert (summary.done, summary.error, summary.cancelled) == (2, 0, 0)
    assert summary.describe() == "Album complete: 2 sides done."


def test_finish_fires_exactly_once_even_when_sides_race():
    """Two pools can observe the last terminal state at the same instant."""
    seen = []
    album, sides = _finishing_controller(n=4, on_finished=seen.append,
                                         max_encode_workers=4)
    album.start()
    _wait_until(lambda: all(s.state == SideState.READY for s in sides))
    for s in sides:
        album.accept_side(s.index, [1.0])

    assert _wait_until(lambda: album.finished)
    time.sleep(0.15)                     # let any second firing land
    assert len(seen) == 1                # claimed once, not once per side


def test_a_failed_side_still_finishes_the_album_and_is_counted():
    def encode(side, cancel):
        if side.index == 1:
            raise RuntimeError("ffmpeg fell over")

    seen = []
    album, sides = _finishing_controller(encode=encode, on_finished=seen.append)
    album.start()
    _wait_until(lambda: all(s.state == SideState.READY for s in sides))
    for s in sides:
        album.accept_side(s.index, [1.0])

    assert _wait_until(lambda: album.finished)
    assert seen[0].describe() == "Album complete: 1 done, 1 error."
    # "Finished" over a failed side would be a lie of omission.
    assert seen[0].done == 1 and seen[0].error == 1


def test_cancelling_everything_also_finishes_the_album():
    album, sides = _finishing_controller()
    seen = []
    album._on_finished = seen.append
    album.cancel_all()                   # before anything starts: all waiting

    assert _wait_until(lambda: album.finished)
    assert seen[0].describe() == "Album complete: 2 cancelled."


def test_unmapped_sides_finish_the_album_immediately():
    seen = []
    sides = [SideJob(index=0, label="Side A", wav_path=None)]
    album = AlbumController(sides, lambda s, c: object(), lambda s, c: None,
                            on_finished=seen.append)
    album.start()                        # ERROR on setup, and that is terminal
    assert album.finished
    assert seen[0].describe() == "Album complete: 1 error."


def test_retrying_after_the_album_finished_lets_it_finish_again():
    """A retry re-opens a concluded job; it has to be able to conclude twice."""
    calls = []

    def encode(side, cancel):
        calls.append(side.index)
        if len(calls) == 1:
            raise RuntimeError("transient")

    seen = []
    album, sides = _finishing_controller(n=1, encode=encode, on_finished=seen.append)
    album.start()
    _wait_until(lambda: sides[0].state == SideState.READY)
    album.accept_side(0, [1.0])
    assert _wait_until(lambda: album.finished)
    assert seen[0].error == 1

    assert album.retry_side(0) is True
    assert album.finished is False        # re-opened
    _wait_until(lambda: sides[0].state == SideState.READY)
    album.accept_side(0, [1.0])
    assert _wait_until(lambda: album.finished)
    assert len(seen) == 2 and seen[1].done == 1


def test_summary_describes_a_single_side_in_the_singular():
    assert AlbumSummary(done=1).describe() == "Album complete: 1 side done."
    assert AlbumSummary(done=3).describe() == "Album complete: 3 sides done."
    assert AlbumSummary().describe() == "Album complete: no sides."
    assert AlbumSummary(done=1, cancelled=2).describe() == (
        "Album complete: 1 done, 2 cancelled.")


# --------------------------------------------------------------------------- #
# The richer summary: per-side receipts, roll-up sizes/warnings, honest errors
# --------------------------------------------------------------------------- #
def test_summary_carries_sizes_and_warnings_with_an_error_side():
    sides = [SideJob(index=0, label="Side A", wav_path=Path("a.wav")),
             SideJob(index=1, label="Side B", wav_path=Path("b.wav"))]
    album = AlbumController(sides, analyze_fn=lambda s, c: object(),
                            encode_fn=lambda s, c: None)
    try:
        # Side A finished, wrote two tracks (one carried a warning); B failed.
        sides[0].state = SideState.DONE
        sides[0].result = SideSummary(
            index=0, label="Side A", state=SideState.ENCODING,   # placeholder state
            track_count=2, output_paths=(Path("out/[01].flac"), Path("out/[02].flac")),
            total_bytes=2048, duration_s=185.0,
            warnings=("Could not embed cover art: boom",), warned_tracks=1)
        sides[1].state = SideState.ERROR

        summary = album.summary()

        assert (summary.done, summary.error, summary.cancelled) == (1, 1, 0)
        assert summary.total_bytes == 2048
        assert summary.warnings == ("Could not embed cover art: boom",)
        assert summary.warned_tracks == 1

        by_index = {s.index: s for s in summary.sides}
        a = by_index[0]
        assert a.state == SideState.DONE          # re-stamped from the side, not ENCODING
        assert a.track_count == 2 and a.total_bytes == 2048 and a.duration_s == 185.0
        # An error side still appears, honestly, carrying nothing it never wrote.
        b = by_index[1]
        assert b.state == SideState.ERROR
        assert b.track_count == 0 and b.total_bytes == 0 and b.warnings == ()
    finally:
        album.shutdown(wait=False)


def test_measure_outputs_totals_bytes_and_duration(tmp_path):
    import numpy as np
    import soundfile as sf

    one_sec = tmp_path / "a.wav"
    half_sec = tmp_path / "b.wav"
    sf.write(str(one_sec), np.zeros(44100, dtype="float32"), 44100)
    sf.write(str(half_sec), np.zeros(22050, dtype="float32"), 44100)

    # A missing path is skipped, not fatal.
    total_bytes, duration_s = measure_outputs([one_sec, half_sec, tmp_path / "gone.wav"])

    assert total_bytes == one_sec.stat().st_size + half_sec.stat().st_size
    assert abs(duration_s - 1.5) < 0.01


# --------------------------------------------------------------------------- #
# Teardown: a controller must never outlive the process that made it
# --------------------------------------------------------------------------- #
def test_a_controller_registers_itself_until_shut_down():
    """The registry exists so nothing can be left holding pool threads.

    ThreadPoolExecutor threads are non-daemon and Python joins every one from
    its own atexit hook, so a single controller nobody closed is enough to keep
    the interpreter alive after the last window has gone.
    """
    from core import album

    ctrl = AlbumController([SideJob(0, "Side A", Path("a.wav"))],
                           lambda s, c: "analysis", lambda s, c: None)
    try:
        assert ctrl in album._LIVE_CONTROLLERS
    finally:
        ctrl.shutdown(wait=True)
    assert ctrl not in album._LIVE_CONTROLLERS


def test_shutdown_all_closes_a_controller_nobody_closed():
    from core import album

    ctrl = AlbumController([SideJob(0, "Side A", Path("a.wav"))],
                           lambda s, c: "analysis", lambda s, c: None)

    assert album.shutdown_all(wait=True) >= 1
    assert ctrl not in album._LIVE_CONTROLLERS
    assert album.shutdown_all(wait=True) == 0        # idempotent


def test_shutdown_does_not_rewrite_finished_sides_as_cancelled():
    """Shutting the pools down is a statement about the pools.

    Marking settled sides CANCELLED would make "we stopped listening"
    indistinguishable from "the user cancelled it", for anything reading state
    afterwards -- including a receipt.
    """
    ctrl = AlbumController([SideJob(0, "Side A", Path("a.wav"))],
                           lambda s, c: "analysis", lambda s, c: None)
    side = ctrl.sides[0]
    ctrl.start()
    assert _wait_until(lambda: side.state == SideState.READY)

    ctrl.shutdown(wait=True)

    assert side.state == SideState.READY


def test_shutdown_asks_running_work_to_stop():
    """cancel_futures only drops work that has not started; a running analysis
    is what actually kept the process alive."""
    import threading

    started = threading.Event()
    observed = {}

    def analyze(side, should_cancel):
        started.set()
        for _ in range(200):
            if should_cancel():
                observed["cancelled"] = True
                return "stopped"
            time.sleep(0.01)
        return "analysis"

    ctrl = AlbumController([SideJob(0, "Side A", Path("a.wav"))],
                           analyze, lambda s, c: None)
    ctrl.start()
    assert started.wait(3.0)

    ctrl.shutdown(wait=True)

    assert observed.get("cancelled"), "a running task was never asked to stop"
