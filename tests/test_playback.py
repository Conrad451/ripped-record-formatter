"""Audition playback: the gestures, the cursor, and the file handle.

The handle test is the important one. Staging is deleted after a side is
accepted, and on Windows an open media handle makes that delete fail with a
sharing violation -- so "stop" has to mean "released", not merely "paused".
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil

import numpy as np
import pytest
import soundfile as sf

SR = 44100


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _pump(qapp, predicate, timeout=5.0):
    """Spin the event loop until a condition holds. Media loading is asynchronous."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _wav(path, seconds=2.0, subtype="PCM_16"):
    t = np.arange(int(SR * seconds)) / SR
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             SR, subtype=subtype)
    return path


# --------------------------------------------------------------------------- #
# The player
# --------------------------------------------------------------------------- #
def test_player_reports_availability_and_never_raises(qapp, tmp_path):
    from gui.playback import AuditionPlayer

    p = AuditionPlayer()
    # Whatever the machine has, construction must not blow up, and the flag must
    # tell the truth so the UI can disable rather than crash.
    assert isinstance(p.available, bool)
    if not p.available:
        assert p.unavailable_reason           # ...and say why
        # Every transport call is a no-op, not an exception.
        p.set_source(_wav(tmp_path / "a.wav"))
        p.play(); p.pause(); p.toggle(); p.stop(); p.seek(1.0)
        p.preview_cut(1.0, 0.5); p.play_window(0.0, 1.0)
        assert p.position() == 0.0


def test_stop_releases_the_file_so_staging_can_be_deleted(qapp, tmp_path):
    """Accept-after-playback must not fail the staging delete on Windows."""
    from gui.playback import AuditionPlayer

    staging = tmp_path / "staging"
    staging.mkdir()
    wav = _wav(staging / "restored.wav")

    p = AuditionPlayer()
    if not p.available:
        pytest.skip("no audio backend on this machine")

    p.set_source(wav)
    p.play()
    assert _pump(qapp, lambda: p._player.duration() > 0), "media never loaded"
    _pump(qapp, lambda: p.position() > 0.0, timeout=2.0)

    p.stop()                                   # must RELEASE, not merely pause
    _pump(qapp, lambda: not p.is_playing(), timeout=2.0)

    # This is the assertion that matters: staging cleanup succeeds.
    shutil.rmtree(staging)
    assert not staging.exists()


def test_preview_cut_seeks_to_lead_in_before_the_marker(qapp, tmp_path):
    from gui.playback import AuditionPlayer

    p = AuditionPlayer()
    if not p.available:
        pytest.skip("no audio backend on this machine")

    p.set_source(_wav(tmp_path / "a.wav", seconds=30.0))
    assert _pump(qapp, lambda: p._player.duration() > 0), "media never loaded"

    p.preview_cut(20.0, lead_in=5.0)           # cut at 20s, 5s lead-in
    assert _pump(qapp, lambda: p.position() >= 14.0)

    assert p.position() == pytest.approx(15.0, abs=1.5)   # -> starts at ~15s
    p.stop()


def test_preview_cut_clamps_at_the_start(qapp, tmp_path):
    from gui.playback import AuditionPlayer

    p = AuditionPlayer()
    if not p.available:
        pytest.skip("no audio backend on this machine")
    p.set_source(_wav(tmp_path / "a.wav", seconds=10.0))
    assert _pump(qapp, lambda: p._player.duration() > 0)

    p.preview_cut(2.0, lead_in=5.0)            # would be -3s
    _pump(qapp, lambda: p.is_playing())
    assert p.position() >= 0.0                 # clamped, not negative
    p.stop()


def test_float_wav_is_transcoded_for_preview(tmp_path):
    """A float-sourced rip stages as float; Windows backends dislike that."""
    from gui.playback import transcode_for_preview

    src = _wav(tmp_path / "float.wav", subtype="FLOAT")
    assert sf.info(str(src)).subtype == "FLOAT"

    out = transcode_for_preview(src, tmp_path / "preview.wav")
    assert sf.info(str(out)).subtype == "PCM_16"
    assert sf.info(str(out)).samplerate == SR


# --------------------------------------------------------------------------- #
# Waveform: cursor, seek gesture, selection, nudge
# --------------------------------------------------------------------------- #
def test_playhead_tracks_position_and_clears(qapp):
    from gui.waveform import WaveformView

    view = WaveformView()
    assert view.playhead() is None

    view.set_playhead(12.5)
    assert view.playhead() == pytest.approx(12.5)
    view.set_playhead(30.0)                    # moves, not duplicates
    assert view.playhead() == pytest.approx(30.0)

    view.set_playhead(None)
    assert view.playhead() is None


def test_selected_marker_can_be_nudged(qapp):
    from gui.waveform import WaveformView

    view = WaveformView()
    view.add_marker(10.0)
    view.add_marker(20.0)

    assert view.selected_time() is None
    view.select_time(20.0)
    assert view.selected_time() == pytest.approx(20.0)

    assert view.nudge_selected(0.05) is True   # 50 ms right
    assert view.selected_time() == pytest.approx(20.05)
    assert view.nudge_selected(-0.10) is True
    assert view.selected_time() == pytest.approx(19.95)

    # Clearing the markers clears the selection with them.
    view.clear_markers()
    assert view.selected_time() is None
    assert view.nudge_selected(0.05) is False


def test_ctrl_click_seeks_and_plain_click_places(qapp):
    """The seek gesture must not collide with click-to-place."""
    from PySide6.QtCore import QPointF, Qt

    from gui.waveform import WaveformView

    view = WaveformView()

    class _Click:
        def __init__(self, mods):
            self._m = mods

        def double(self):
            return False

        def button(self):
            return Qt.MouseButton.LeftButton

        def modifiers(self):
            return self._m

        def scenePos(self):
            return QPointF(0, 0)

        def accept(self):
            pass

    seeks: list[float] = []
    view.seekRequested.connect(seeks.append)

    # Place mode + plain click -> places a marker, does NOT seek.
    view.set_place_mode(True)
    view._on_scene_clicked(_Click(Qt.KeyboardModifier.NoModifier))
    assert view.marker_count() == 1
    assert seeks == []

    # Place mode + Ctrl+click -> seeks, does NOT place.
    view._on_scene_clicked(_Click(Qt.KeyboardModifier.ControlModifier))
    assert view.marker_count() == 1            # unchanged
    assert len(seeks) == 1


def test_seek_issued_before_load_is_applied_once_loaded(qapp, tmp_path):
    """Preview cut pressed the instant a side opens must not be swallowed."""
    from gui.playback import AuditionPlayer

    p = AuditionPlayer()
    if not p.available:
        pytest.skip("no audio backend on this machine")

    p.set_source(_wav(tmp_path / "a.wav", seconds=30.0))
    p.preview_cut(20.0, lead_in=5.0)           # issued before the media is loaded
    assert _pump(qapp, lambda: p.position() >= 14.0), "pending seek was dropped"
    assert p.position() == pytest.approx(15.0, abs=1.5)
    p.stop()


# --------------------------------------------------------------------------- #
# Degradation: no audio backend must not break the review flow
# --------------------------------------------------------------------------- #
def test_missing_backend_disables_controls_without_crashing(qapp, monkeypatch):
    """A machine with no audio still reviews sides -- it just cannot listen."""
    import gui.playback as pb

    monkeypatch.setattr(pb, "_HAVE_QTMULTIMEDIA", False)
    monkeypatch.setattr(pb, "_BACKEND_ERROR", "QtMultimedia is not available (test)")

    player = pb.AuditionPlayer()
    assert player.available is False
    assert "not available" in player.unavailable_reason

    # And the tab builds, with the controls disabled and explaining themselves.
    import gui.full_rip as fr_mod

    monkeypatch.setattr(fr_mod, "AuditionPlayer", pb.AuditionPlayer)
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    assert fr.player.available is False
    assert not fr.play_btn.isEnabled()
    assert not fr.preview_cut_btn.isEnabled()
    assert "unavailable" in fr.play_btn.toolTip().lower()
    assert "unavailable" in fr.playback_hint.text().lower()

    # The review flow itself is untouched: the waveform still takes markers.
    fr.waveform.add_marker(5.0)
    assert fr.waveform.marker_count() == 1
