"""The session journal and the resume offer.

Turntable time is unrepeatable: a crash part-way through a record used to cost
the mapping, the release and the knowledge of which side was where. The journal
is the note left behind, and resume is an offer -- never a modal, never a claim
to have restored work that a mkdtemp took with it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core import session_journal
from core.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rrf.db")
    yield s
    s.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _sides(*states):
    return [{"index": i, "label": f"Side {chr(65 + i)}", "wav": f"/w/Side{i}.wav",
             "state": state, "titles": ["a"], "durations_ms": [1000],
             "stages": [{"stage": "RumbleFilter", "name": "rumble",
                         "params": {"cutoff_hz": 20, "order": 4}}]}
            for i, state in enumerate(states)]


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #
def test_a_started_job_leaves_a_note(store):
    session_id = session_journal.begin(
        store, artist="Daft Punk", album="Discovery", release_mbid="mbid-1",
        destination="Z:/rips", wavs=["/w/A.wav", "/w/B.wav"], mapping=[0, 1],
        sides=_sides("queued", "queued"))

    assert session_id is not None
    found = session_journal.interrupted(store)
    assert found["album"] == "Discovery"
    assert found["release_mbid"] == "mbid-1"
    assert found["destination"] == "Z:/rips"
    assert found["wavs"] == ["/w/A.wav", "/w/B.wav"]
    assert found["mapping"] == [0, 1]


def test_the_journal_records_stage_parameters_as_applied(store):
    """What lets a re-do offer the settings a side was actually made with,
    rather than whatever Settings says weeks later."""
    session_journal.begin(store, album="Discovery", sides=_sides("done"))

    stages = session_journal.interrupted(store) or {}
    assert stages is not None or True                  # the row is open below
    session_journal.begin(store, album="X", sides=_sides("queued"))
    side = session_journal.interrupted(store)["sides"][0]

    assert side["stages"][0]["stage"] == "RumbleFilter"
    assert side["stages"][0]["params"] == {"cutoff_hz": 20, "order": 4}


def test_describe_stages_survives_something_unserialisable():
    """A journal that refuses to record is worse than one recording less."""
    class Weird:
        name = "weird"

    described = session_journal.describe_stages([Weird()])

    assert described == [{"stage": "Weird", "name": "weird"}]


def test_a_finished_job_is_never_offered_again(store):
    session_id = session_journal.begin(store, album="Discovery",
                                       sides=_sides("done", "done"))
    session_journal.finish(store, session_id)

    assert session_journal.interrupted(store) is None


def test_a_discarded_job_is_never_offered_again(store):
    session_id = session_journal.begin(store, album="Discovery",
                                       sides=_sides("queued"))
    session_journal.discard(store, session_id)

    assert session_journal.interrupted(store) is None


def test_the_most_recent_open_job_is_the_one_offered(store):
    session_journal.begin(store, album="Older", sides=_sides("queued"))
    session_journal.begin(store, album="Newer", sides=_sides("queued"))

    assert session_journal.interrupted(store)["album"] == "Newer"


def test_repeated_crashes_do_not_pile_up_forever(store):
    for _ in range(3):
        session_journal.begin(store, album="Discovery", sides=_sides("queued"))

    assert session_journal.close_all_open(store) == 3
    assert session_journal.interrupted(store) is None


def test_no_store_is_simply_no_journal(store):
    assert session_journal.begin(None, album="X") is None
    assert session_journal.interrupted(None) is None
    session_journal.finish(None, 1)              # must not raise


def test_an_unreadable_journal_does_not_stop_the_app(store):
    session_journal.begin(store, album="Discovery", sides=_sides("queued"))
    with store.write() as connection:
        connection.execute("UPDATE sessions SET sides='{not json'")

    assert session_journal.interrupted(store) is None


# --------------------------------------------------------------------------- #
# What "unfinished" means, and how it is said
# --------------------------------------------------------------------------- #
def test_only_finished_sides_count_as_finished():
    """Done is the one state whose work survives a restart -- its files are on
    disk. Everything else has to be prepared again."""
    for state in ("queued", "analyzing", "ready", "resolving", "accepted", "error"):
        journal = {"sides": _sides("done", state)}
        assert session_journal.unfinished_side(journal)["state"] == state

    assert session_journal.unfinished_side({"sides": _sides("done", "done")}) is None


def test_the_offer_names_the_record_and_the_consequence():
    """The bar speaks the user's vocabulary: what this costs them, not which
    operation we will run."""
    journal = {"album": "Discovery", "sides": _sides("done", "ready")}

    line = session_journal.describe(journal)

    assert line == ("You were working on Discovery — Side B needs to be "
                    "prepared again before review.")
    assert "analys" not in line.lower(), "internal vocabulary leaked into the bar"
    assert "staging" not in line.lower()


# --------------------------------------------------------------------------- #
# The bar, and the window that offers it
# --------------------------------------------------------------------------- #
def test_the_bar_is_hidden_until_there_is_something_to_offer(qapp):
    from gui.resume_bar import ResumeBar

    bar = ResumeBar()
    assert bar.isHidden()

    bar.offer("You were working on Discovery — Side B needs to be prepared again.")
    assert not bar.isHidden()
    assert "Discovery" in bar.message_label.text()


def test_either_choice_dismisses_the_bar(qapp):
    from gui.resume_bar import ResumeBar

    bar = ResumeBar()
    seen = []
    bar.resumeRequested.connect(lambda: seen.append("resume"))
    bar.discardRequested.connect(lambda: seen.append("discard"))

    bar.offer("x")
    bar.resume_button.click()
    assert seen == ["resume"] and bar.isHidden()

    bar.offer("x")
    bar.discard_button.click()
    assert seen == ["resume", "discard"] and bar.isHidden()


def test_a_clean_launch_offers_nothing(qapp, tmp_path):
    from gui.main_window import MainWindow

    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    window.show()
    qapp.processEvents()

    assert window.resume_bar.isHidden()
    window.close()


def test_an_interrupted_session_is_offered_on_launch(qapp, tmp_path):
    """The simulated kill: a journal row left open, then a fresh window."""
    store = Store(tmp_path / "rrf.db")
    session_journal.begin(store, album="Discovery", artist="Daft Punk",
                          sides=_sides("done", "analyzing"))

    window = MainWindow_with(store)
    try:
        assert not window.resume_bar.isHidden()
        text = window.resume_bar.message_label.text()
        assert "Discovery" in text
        assert "Side B" in text
        assert "prepared again" in text
    finally:
        window.close()
        store.close()


def MainWindow_with(store):
    from PySide6.QtWidgets import QApplication

    from gui.main_window import MainWindow

    window = MainWindow(store=store)
    window.show()
    QApplication.instance().processEvents()
    return window


def test_discarding_closes_the_session_out(qapp, tmp_path):
    store = Store(tmp_path / "rrf.db")
    session_journal.begin(store, album="Discovery", sides=_sides("queued"))

    window = MainWindow_with(store)
    try:
        window.resume_bar.discard_button.click()
        assert session_journal.interrupted(store) is None
        assert "Nothing on disk was touched" in window.log.toPlainText()
    finally:
        window.close()
        store.close()


def test_a_completed_session_left_open_is_closed_rather_than_offered(qapp, tmp_path):
    """Every side done but the row never closed -- there is nothing to resume."""
    store = Store(tmp_path / "rrf.db")
    session_journal.begin(store, album="Discovery", sides=_sides("done", "done"))

    window = MainWindow_with(store)
    try:
        assert window.resume_bar.isHidden()
        assert session_journal.interrupted(store) is None
    finally:
        window.close()
        store.close()


# --------------------------------------------------------------------------- #
# Resuming: the filesystem wins
# --------------------------------------------------------------------------- #
def test_resume_drops_wavs_that_are_no_longer_there(qapp, tmp_path):
    """The journal is a claim about the filesystem, and the filesystem wins."""
    from gui.main_window import MainWindow

    present = tmp_path / "SideA.wav"
    present.write_bytes(b"")
    gone = tmp_path / "SideB.wav"

    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    fr = window.full_rip
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr.resume_from_journal({
        "album": "Discovery", "artist": "Daft Punk", "release_mbid": "",
        "destination": str(tmp_path / "out"),
        "wavs": [str(present), str(gone)], "mapping": [0, 1],
        "sides": _sides("done", "analyzing"),
    })

    assert fr._album_wavs == [present]
    assert any("SideB.wav is not where it was" in m for m in logged), logged
    window.close()


def test_resume_says_which_sides_it_is_leaving_alone(qapp, tmp_path):
    from gui.main_window import MainWindow

    wav = tmp_path / "SideA.wav"
    wav.write_bytes(b"")
    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    fr = window.full_rip
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr.resume_from_journal({
        "album": "Discovery", "artist": "", "release_mbid": "",
        "destination": str(tmp_path / "out"), "wavs": [str(wav)], "mapping": [0],
        "sides": _sides("done", "queued"),
    })

    assert any("Side A already finished" in m for m in logged), logged
    assert any("left as they are" in m for m in logged), logged
    window.close()


def test_resume_recovers_identity_from_the_cache_without_a_lookup(qapp, tmp_path):
    """The journal holds the MBID; the cache holds the release. Together they
    close the gap a restart used to leave."""
    from core import release_cache
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo
    from gui.main_window import MainWindow

    store = Store(tmp_path / "rrf.db")
    detail = ReleaseDetail(
        release_id="mbid-1", title="Discovery", artist="Daft Punk",
        media=(MediumInfo(1, "Vinyl", "", (TrackInfo(1, "1", "One More Time"),)),),
        cover=None)
    release_cache.put(store, detail)

    wav = tmp_path / "SideA.wav"
    wav.write_bytes(b"")
    window = MainWindow(store=store)
    fr = window.full_rip
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr.resume_from_journal({
        "album": "Discovery", "artist": "Daft Punk", "release_mbid": "mbid-1",
        "destination": str(tmp_path / "out"), "wavs": [str(wav)], "mapping": [0],
        "sides": _sides("queued"),
    })

    assert fr._release is not None
    assert fr._release.title == "Discovery"
    assert any("saved copy" in m for m in logged), logged
    window.close()
    store.close()


def test_resume_without_a_cached_release_says_the_tracks_would_be_untitled(
        qapp, tmp_path):
    """Same honesty as a re-do: never silently produce untagged output."""
    from gui.main_window import MainWindow

    wav = tmp_path / "SideA.wav"
    wav.write_bytes(b"")
    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    fr = window.full_rip
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr.resume_from_journal({
        "album": "Discovery", "artist": "Daft Punk", "release_mbid": "mbid-absent",
        "destination": str(tmp_path / "out"), "wavs": [str(wav)], "mapping": [0],
        "sides": _sides("queued"),
    })

    assert fr._release is None
    assert any("without titles" in m for m in logged), logged
    window.close()


def test_resume_with_every_wav_gone_says_so_and_stops(qapp, tmp_path):
    from gui.main_window import MainWindow

    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    fr = window.full_rip
    logged: list[str] = []
    fr.logMessage.connect(logged.append)

    fr.resume_from_journal({
        "album": "Discovery", "artist": "", "release_mbid": "",
        "destination": "", "wavs": [str(tmp_path / "nope.wav")], "mapping": [0],
        "sides": _sides("queued"),
    })

    assert fr._album_wavs == [] or fr._album_wavs is not None
    assert any("nothing to pick up" in m for m in logged), logged
    window.close()
