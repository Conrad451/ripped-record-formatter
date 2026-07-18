"""Record-to-analyze pipelining: a completed recording that lands mapped joins
a running album immediately, instead of waiting for a restart.

Drives the real FullRipTab handoff (add_recorded_wav) with a stubbed analyze/
encode pipeline -- this is about admission and lifecycle, not DSP.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

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


def _wav(path):
    sf.write(str(path), np.zeros(SR // 10, dtype="float32"), SR, subtype="PCM_16")
    return path


def _release():
    def track(pos, title):
        return TrackInfo(position=pos, number=str(pos), title=title, length_ms=3000)

    return ReleaseDetail("r", "Kind of Blue", "Miles Davis", media=(
        MediumInfo(1, "Vinyl", tracks=(track(1, "So What"), track(2, "Freddie"))),
        MediumInfo(2, "Vinyl", tracks=(track(1, "Blue in Green"), track(2, "Flamenco"))),
    ))


def _drain(qapp, predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _tab(qapp, tmp_path, monkeypatch):
    """A FullRipTab with the release loaded and a stubbed pipeline, side A on disk."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr.settings.config.filename_side_letters = False
    fr._apply_release(_release())
    monkeypatch.setattr(type(fr), "_album_analyze", lambda self, side, cancel: "analysis")
    monkeypatch.setattr(type(fr), "_album_encode", lambda self, side, cancel: None)

    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "out").mkdir()
    fr.output_edit.setText(str(tmp_path / "out"))
    return fr, src


def _start_with_side_a(qapp, fr, src):
    """Map side A and press Start; return once A is READY (job open, not concluded)."""
    fr._album_wavs = [_wav(src / "SideA.wav")]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0]
    fr._start_album()
    assert fr._album is not None
    assert _drain(qapp, lambda: any(
        s.index == 0 and s.state == SideState.READY for s in fr._album.sides))


# --------------------------------------------------------------------------- #
# The core case: a recorded side joins the running job
# --------------------------------------------------------------------------- #
def test_a_recorded_side_joins_a_running_album(qapp, tmp_path, monkeypatch):
    fr, src = _tab(qapp, tmp_path, monkeypatch)
    _start_with_side_a(qapp, fr, src)

    # Side B records into the same folder while the job is still open.
    assert fr.add_recorded_wav(_wav(src / "SideB.wav")) is True

    # B was admitted into the *same* controller and analysed -- no restart.
    assert _drain(qapp, lambda: any(s.index == 1 for s in fr._album.sides))
    assert _drain(qapp, lambda: any(
        s.index == 1 and s.state == SideState.READY for s in fr._album.sides))
    labels = [fr.side_list.item(i).text() for i in range(fr.side_list.count())]
    assert any("Side B" in t for t in labels)          # queued->analysing in the list


def test_admission_carries_a_recordings_warnings_forward(qapp, tmp_path, monkeypatch):
    fr, src = _tab(qapp, tmp_path, monkeypatch)
    _start_with_side_a(qapp, fr, src)

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    assert fr.add_recorded_wav(_wav(src / "SideB.wav"),
                               warnings=["input overflow: a dropout is present"]) is True

    assert _drain(qapp, lambda: any(s.index == 1 for s in fr._album.sides))   # still admits
    assert any("joined the album" in m and "warning" in m for m in logged)


def test_cancel_album_cancels_an_admitted_side(qapp, tmp_path, monkeypatch):
    fr, src = _tab(qapp, tmp_path, monkeypatch)
    _start_with_side_a(qapp, fr, src)
    ctrl = fr._album

    assert fr.add_recorded_wav(_wav(src / "SideB.wav")) is True
    assert _drain(qapp, lambda: any(s.index == 1 for s in ctrl.sides))

    fr._cancel_album()
    assert _drain(qapp, lambda: fr._album is None)      # cancel concludes + releases
    assert any(s.index == 1 and s.state == SideState.CANCELLED for s in ctrl.sides)


# --------------------------------------------------------------------------- #
# The other two cases: no job, and a concluded job -- both map only
# --------------------------------------------------------------------------- #
def test_no_running_job_maps_only_and_starts_nothing(qapp, tmp_path, monkeypatch):
    fr, src = _tab(qapp, tmp_path, monkeypatch)
    fr._album_wavs = [_wav(src / "SideA.wav")]
    fr._rebuild_mapping_table()

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    assert fr.add_recorded_wav(_wav(src / "SideB.wav")) is True

    assert fr._album is None                            # nothing was auto-started
    assert fr.mapping_table.rowCount() == 2             # just mapped
    assert any("press Start album when ready" in m for m in logged)


def test_a_late_side_recorded_at_conclusion_lands_via_the_defer(qapp, tmp_path, monkeypatch):
    """A side still recording when the album concludes lands via the clean-slate
    defer (9.7): the reset waits, so the WAV maps into the kept table rather than
    being orphaned. The job already finished, so it maps -- it is not admitted."""
    fr, src = _tab(qapp, tmp_path, monkeypatch)
    _start_with_side_a(qapp, fr, src)

    # Side B is being recorded when side A completes and the album concludes.
    fr.set_recording_active(True)
    fr._album.accept_side(0, [1.0], ["So What"])
    assert _drain(qapp, lambda: fr._album is None)
    assert fr.mapping_table.rowCount() == 1             # reset deferred: table kept

    logged: list[str] = []
    fr.logMessage.connect(logged.append)
    assert fr.add_recorded_wav(_wav(src / "SideB.wav")) is True

    assert fr._album is None                            # not restarted
    assert fr.mapping_table.rowCount() == 2             # late side mapped into the table
