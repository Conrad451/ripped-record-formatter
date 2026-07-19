"""The record-to-rip seam (9.10): declaring an album, and the bridge to Full Rip.

Two stakeholder findings drive all of this. Recording the last side used to dead
-end -- the WAVs were mapped in Full Rip but nothing in the Record tab said so or
offered a next step -- and there was no way to say *what record* was on the
platter, so a session captured anonymous WAVs and identity had to be established
later, in another tab.

The binding ruling throughout: the user declares completion explicitly. Nothing
here infers "album done" from a release's side count, because field experience
says that count is often wrong (CD collapses, pressing differences). MusicBrainz
content is trustworthy; MusicBrainz *shape* is advisory. So: no inference, no
auto-anything, and an anonymous session stays a first-class flow.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.recorder import DeviceInfo, RecordingResult
from core.tracks import safe_part
from gui.record_tab import next_side_name, side_labels

SR = 44100


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    """Stub device enumeration and the monitor stream -- no real audio device."""
    import core.recorder as rec_mod
    import gui.record_tab as tab_mod

    devices = [
        DeviceInfo(index=7, name="Line In (Realtek)", hostapi="Windows WASAPI",
                   samplerate=192000, max_channels=2),
    ]
    monkeypatch.setattr(tab_mod, "list_input_devices", lambda: devices)
    monkeypatch.setattr(tab_mod, "list_output_devices", lambda: [])
    monkeypatch.setattr(rec_mod.LevelMonitor, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(rec_mod.LevelMonitor, "stop", lambda self: None)
    return devices


def _side_wav(path, seconds=0.2):
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(SR * seconds)) / SR
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             SR, subtype="PCM_16")
    return path


def _result(path):
    return RecordingResult(path=path, duration=1.0, samplerate=SR,
                           subtype="PCM_16", max_peak_dbfs=-3.0, clip_runs=0)


def _release(title="Songs From the Big Chair", artist="Tears for Fears", sides=2,
             medium_titles=None):
    """A release with ``sides`` media, three tracks each."""
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo

    titles = medium_titles or [""] * sides
    return ReleaseDetail("id", title, artist, media=tuple(
        MediumInfo(s + 1, "Vinyl", titles[s], tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"{s}-{i}", 180000) for i in range(3)))
        for s in range(sides)))


def _record_into(w, folder, name="SideA.wav"):
    """Drive a completed capture landing at ``folder/name``, as a real stop does."""
    wav = _side_wav(Path(folder) / name)
    w.record_tab.recordingFinished.emit(_result(wav))
    return wav


def _on_record_tab(w):
    """Where the user actually is while recording (Full Rip is the default tab)."""
    w.tabs.setCurrentWidget(w.record_tab)
    return w


# --------------------------------------------------------------------------- #
# 2 -- the bridge button
# --------------------------------------------------------------------------- #
def test_the_bridge_is_disabled_until_a_recording_has_landed(qapp, tmp_path):
    """Nothing recorded, nothing to process. The button is not an invitation yet."""
    from gui.main_window import MainWindow

    w = MainWindow()
    assert w.record_tab.process_button.isEnabled() is False

    # A capture that lands *elsewhere* is not this session's business either.
    other = tmp_path / "elsewhere"
    w.full_rip._album_wavs = [_side_wav(tmp_path / "album" / "SideA.wav")]
    w.full_rip._rebuild_mapping_table()
    _record_into(w, other, "Stray.wav")
    qapp.processEvents()

    assert w.record_tab.process_button.isEnabled() is False


def test_a_landed_recording_arms_the_bridge_and_it_only_bridges(qapp, tmp_path):
    """The button switches to Full Rip with staging intact -- and starts nothing."""
    from gui.main_window import MainWindow

    w = _on_record_tab(MainWindow())
    fr = w.full_rip
    _record_into(w, tmp_path, "SideA.wav")
    qapp.processEvents()

    assert w.record_tab.process_button.isEnabled() is True
    assert w.tabs.currentWidget() is not fr           # nothing moved on its own

    w.record_tab.process_button.click()
    qapp.processEvents()

    assert w.tabs.currentWidget() is fr               # ...now it did
    assert [p.name for p in fr._album_wavs] == ["SideA.wav"]   # staging intact
    assert fr.mapping_table.rowCount() == 1
    # A bridge, not a trigger: no job exists and nothing is running.
    assert fr._album is None
    assert fr._busy is False


def test_the_bridge_points_at_lookup_when_the_album_is_anonymous(qapp, tmp_path):
    """No identity yet -> the next action is naming the record, not starting it."""
    from gui.main_window import MainWindow

    w = MainWindow()
    _record_into(w, tmp_path, "SideA.wav")
    w.record_tab.process_button.click()
    qapp.processEvents()

    assert w.full_rip._emphasised is w.full_rip.lookup_button
    assert w.full_rip._album is None                  # still nothing started


def test_the_bridge_points_at_start_album_once_identity_is_known(qapp, tmp_path):
    """Release set -> the next action is Start album. Emphasised, never pressed."""
    from gui.main_window import MainWindow

    w = MainWindow()
    w.record_tab.apply_release(_release())
    _record_into(w, w.record_tab.folder_edit.text() or tmp_path, "SideA.wav")
    w.record_tab.process_button.click()
    qapp.processEvents()

    assert w.full_rip._emphasised is w.full_rip.start_album_btn
    assert w.full_rip._album is None                  # emphasis is not a press


# --------------------------------------------------------------------------- #
# 1c -- identity rides the handoff
# --------------------------------------------------------------------------- #
def test_a_release_set_on_record_threads_through_to_full_rip(qapp, tmp_path):
    """Files, mapping and identity arrive together -- not identity later, elsewhere."""
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    release = _release()
    w.record_tab.apply_release(release)

    _record_into(w, tmp_path, "SideA.wav")
    qapp.processEvents()

    assert fr._release is release                    # identity came with the audio
    assert fr.artist_edit.text() == "Tears for Fears"
    assert fr.album_edit.text() == "Songs From the Big Chair"
    assert len(fr._sides) == 2                       # ...and so did the side picker
    assert [p.name for p in fr._album_wavs] == ["SideA.wav"]

    w.record_tab.process_button.click()
    qapp.processEvents()

    # Fully staged: files, mapping, identity, sides -- and still not started.
    assert w.tabs.currentWidget() is fr
    assert fr.mapping_table.rowCount() == 1
    assert fr._album_mapping == [0]                  # SideA -> Side A
    assert fr._album is None


def test_full_rips_own_release_is_not_overwritten_by_the_handoff(qapp, tmp_path):
    """A release chosen in Full Rip is the more recent word; the handoff defers."""
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    chosen = _release("Hounds of Love", "Kate Bush")
    fr._apply_release(chosen)
    w.record_tab.apply_release(_release())           # a different, older declaration

    _record_into(w, tmp_path, "SideA.wav")
    qapp.processEvents()

    assert fr._release is chosen


# --------------------------------------------------------------------------- #
# 1 (unset) -- anonymous capture is first-class, not degraded
# --------------------------------------------------------------------------- #
def test_an_unset_session_behaves_exactly_as_before(qapp, tmp_path):
    """No release declared: naming, folder and handoff are untouched by 9.10."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    folder = str(tmp_path / "anon")
    tab.folder_edit.setText(folder)
    tab.file_edit.setText("SideA.wav")

    assert tab.release is None
    # isVisibleTo, not isVisible: these windows are never shown (offscreen), so
    # isVisible() would be False for the wrong reason and prove nothing.
    assert tab.release_preview.isVisibleTo(tab) is False
    assert tab.clear_release_button.isVisibleTo(tab) is False
    assert tab.folder_edit.text() == folder           # nothing offered, nothing forced

    _record_into(w, folder, "SideA.wav")
    qapp.processEvents()

    assert [p.name for p in w.full_rip._album_wavs] == ["SideA.wav"]
    assert w.full_rip._release is None                # still anonymous
    assert tab.process_button.isEnabled() is True     # ...but the bridge still works


def test_the_album_lookup_reuses_the_existing_metadata_panel(qapp, monkeypatch):
    """One lookup UI in the app, opened as a modal -- not a second one built here."""
    from PySide6.QtWidgets import QDialog
    from gui.main_window import MainWindow
    from gui.metadata_panel import MetadataPanel

    monkeypatch.setattr(QDialog, "exec", lambda self: None)   # don't block

    w = MainWindow()
    tab = w.record_tab
    tab.lookup_button.click()

    panel = tab.findChild(MetadataPanel)
    assert panel is not None                     # the real panel, hosted in a dialog
    assert panel._settings is tab.settings       # ...and it can see the MB contact

    # Choosing a release in it is what declares the album.
    panel.releaseSelected.emit(_release())
    assert tab.release is not None
    assert tab.release.title == "Songs From the Big Chair"


def test_declaring_an_album_shows_the_compact_preview_strip(qapp):
    """The release-preview strip appears in the Album row once one is chosen."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    assert tab.release_preview.isVisibleTo(tab) is False

    tab.apply_release(_release())

    assert tab.release_preview.isVisibleTo(tab) is True
    assert tab.clear_release_button.isVisibleTo(tab) is True
    assert "Songs From the Big Chair" in tab.release_preview.title_label.text()
    assert tab.release is not None

    # And it can be taken back off again, by hand.
    tab.clear_release_button.click()
    assert tab.release is None
    assert tab.release_preview.isVisibleTo(tab) is False


# --------------------------------------------------------------------------- #
# 1b -- naming follows side labels where they exist, and never stops at the count
# --------------------------------------------------------------------------- #
def test_naming_runs_past_the_releases_side_count_without_complaint(qapp):
    """A two-side release does not cap a four-side capture. Shape is advisory."""
    labels = side_labels(_release(sides=2))
    assert labels == ["", ""]                        # vinyl media rarely name sides

    # Past side B the release has nothing to say, and naming simply carries on.
    assert next_side_name("SideA.wav", labels) == "SideB.wav"
    assert next_side_name("SideB.wav", labels) == "SideC.wav"
    assert next_side_name("SideC.wav", labels) == "SideD.wav"


def test_naming_uses_the_releases_own_side_labels_where_it_has_them(qapp):
    """A medium that names itself gets to name the file; the rest is lettering."""
    labels = side_labels(_release(sides=2, medium_titles=["Record One", "Record Two"]))
    assert next_side_name("Record One.wav", labels) == "Record Two.wav"
    # ...and past the last named side it falls through rather than stopping.
    assert next_side_name("Record Two.wav", labels) == "Record Two_2.wav"


def test_naming_is_unchanged_when_no_release_is_set(qapp):
    """The pre-9.10 behaviour, verbatim."""
    assert next_side_name("SideA.wav") == "SideB.wav"
    assert next_side_name("SideA.wav", []) == "SideB.wav"
    assert next_side_name("whatever.wav", []) == "whatever_2.wav"


def test_names_from_musicbrainz_are_made_safe_for_windows(qapp):
    assert safe_part("AC/DC") == "AC DC"
    assert safe_part("Where Are We Now?") == "Where Are We Now"
    assert safe_part("") == "Unknown"


# --------------------------------------------------------------------------- #
# 1a / 1d -- folders are offered, never forced
# --------------------------------------------------------------------------- #
def test_the_folder_suggestion_follows_the_root_and_stays_editable(qapp, tmp_path):
    """{WAV root}/{Artist}/{Album}, prefilled and freely overtypeable.

    Derives from the *WAV* root since v3.0.0. It used to follow the FLAC root,
    so captures were offered a home inside the finished-library tree and the
    stakeholder corrected the path by hand every session.
    """
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    root = str(tmp_path / "WAVs")
    tab.settings.set(default_source_dir=root)

    tab.apply_release(_release())

    assert tab.folder_edit.text() == str(
        Path(root) / "Tears for Fears" / "Songs From the Big Chair")
    assert tab.folder_edit.isReadOnly() is False      # an offer, not a fact

    # And the trunk stays a trunk -- the derived path is not written back as one.
    assert tab.settings.config.default_source_dir == root


def test_the_suggestion_never_overwrites_a_hand_entered_path(qapp, tmp_path):
    """A path the user typed is theirs. The offer simply does not arrive."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    tab.settings.set(default_output_dir=str(tmp_path / "Rips"))

    mine = str(tmp_path / "somewhere I chose")
    tab.folder_edit.setText(mine)
    tab.folder_edit.textEdited.emit(mine)             # as typing does
    tab.apply_release(_release())

    assert tab.folder_edit.text() == mine


def test_the_output_folder_derives_from_the_root_and_stays_editable(qapp, tmp_path):
    """1d: the same trunk feeds Full Rip's output folder."""
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    root = str(tmp_path / "Out")
    fr.settings.set(default_output_dir=root)

    fr._apply_release(_release())

    assert fr.output_edit.text() == str(
        Path(root) / "Tears for Fears" / "Songs From the Big Chair")
    assert fr.output_edit.isReadOnly() is False

    # Hand-entered wins here too.
    mine = str(tmp_path / "my own output")
    fr.output_edit.setText(mine)
    fr.output_edit.textEdited.emit(mine)
    fr._apply_release(_release("Hounds of Love", "Kate Bush"))
    assert fr.output_edit.text() == mine


def test_a_release_selected_on_record_derives_full_rips_output_too(qapp, tmp_path):
    """Declared once on the Record tab, the trunk reaches both derived paths."""
    from gui.main_window import MainWindow

    w = MainWindow()
    root = str(tmp_path / "Out")
    w.settings.set(default_output_dir=root)
    w.record_tab.apply_release(_release())

    _record_into(w, w.record_tab.folder_edit.text(), "SideA.wav")
    qapp.processEvents()

    assert w.full_rip.output_edit.text() == str(
        Path(root) / "Tears for Fears" / "Songs From the Big Chair")


# --------------------------------------------------------------------------- #
# 3 -- the post-stop line says where the side went
# --------------------------------------------------------------------------- #
def test_the_saved_line_names_the_album_when_it_is_known(qapp, tmp_path):
    """"...mapped to Side B of Songs From the Big Chair in Full Rip." """
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    logged: list[str] = []
    tab.logMessage.connect(logged.append)

    tab.note_handoff(True, side_label="Side B", album="Songs From the Big Chair")
    tab._report(_result(_side_wav(tmp_path / "SideB.wav")))

    saved = next(m for m in logged if m.startswith("Record: saved"))
    assert "mapped to Side B of Songs From the Big Chair in Full Rip" in saved


def test_the_saved_line_uses_the_anonymous_form_when_it_is_not(qapp, tmp_path):
    """"...mapped to Side B in Full Rip." -- no album, no apology."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    logged: list[str] = []
    tab.logMessage.connect(logged.append)

    tab.note_handoff(True, side_label="Side B", album=None)
    tab._report(_result(_side_wav(tmp_path / "SideB.wav")))

    saved = next(m for m in logged if m.startswith("Record: saved"))
    assert "mapped to Side B in Full Rip" in saved
    assert " of " not in saved


def test_the_saved_line_says_nothing_about_mapping_when_nothing_mapped(qapp, tmp_path):
    """A capture that landed nowhere claims nothing -- the pre-9.10 line, verbatim."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    logged: list[str] = []
    tab.logMessage.connect(logged.append)

    tab.note_handoff(False)
    tab._report(_result(_side_wav(tmp_path / "SideB.wav")))

    saved = next(m for m in logged if m.startswith("Record: saved"))
    assert "Full Rip" not in saved
    assert saved.endswith("Loudest point: -3.0 dBFS.")


class _FakeRecorder:
    """Stands in for a running Recorder so the real _stop_recording path runs."""

    def __init__(self, result):
        self._result = result
        self.recording = True

    def stop(self):
        return self._result


def test_the_mapping_mention_survives_the_real_stop_ordering(qapp, tmp_path):
    """End to end, through _stop_recording itself.

    The ordering is the whole point: the handoff has to resolve *before* the
    summary line is written, or the line has nothing to report. Driving the real
    stop is what proves it -- emitting recordingFinished by hand would skip the
    very sequencing under test.
    """
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    tab.apply_release(_release())
    tab.folder_edit.setText(str(tmp_path))
    _record_into(w, tmp_path, "SideA.wav")            # seeds Full Rip's folder
    qapp.processEvents()

    logged: list[str] = []
    tab.logMessage.connect(logged.append)

    tab._recorder = _FakeRecorder(_result(_side_wav(tmp_path / "SideB.wav")))
    tab._stop_recording()
    qapp.processEvents()

    saved = next(m for m in logged if m.startswith("Record: saved SideB"))
    assert "mapped to Side B of Songs From the Big Chair in Full Rip" in saved
    # ...and the next side is queued up, as it always was.
    assert tab.file_edit.text() == "SideC.wav"


# --------------------------------------------------------------------------- #
# 1 (clean slate) + the standing "no auto-anything" guarantee
# --------------------------------------------------------------------------- #
def test_the_clean_slate_clears_the_declared_album_and_disarms_the_bridge(qapp, tmp_path):
    """9.7's between-albums reset reaches the Record tab's session state too."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    tab.apply_release(_release())
    _record_into(w, tmp_path, "SideA.wav")
    qapp.processEvents()
    assert tab.release is not None and tab.process_button.isEnabled() is True

    w.full_rip._reset_identity()                      # as a concluded album does
    qapp.processEvents()

    assert tab.release is None
    assert tab.release_preview.isVisibleTo(tab) is False
    assert tab.process_button.isEnabled() is False


def test_nothing_in_this_flow_ever_starts_processing(qapp, tmp_path, monkeypatch):
    """The whole seam, exercised end to end, must never call _start_album."""
    from gui.full_rip import FullRipTab
    from gui.main_window import MainWindow

    started: list[int] = []
    monkeypatch.setattr(FullRipTab, "_start_album",
                        lambda self: started.append(1))

    w = MainWindow()
    w.record_tab.apply_release(_release())
    _record_into(w, tmp_path, "SideA.wav")
    _record_into(w, tmp_path, "SideB.wav")
    qapp.processEvents()
    w.record_tab.process_button.click()
    qapp.processEvents()

    assert started == []                              # the user starts albums
    assert w.full_rip._album is None


def test_captures_are_offered_the_wav_root_never_the_flac_library(qapp, tmp_path):
    """The field finding, as a regression guard.

    The raw WAV is the master and belongs with masters. The finished library is
    a different place with a different lifecycle, and only Full Rip writes
    there -- so a capture must never default into it.
    """
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    wav_root = str(tmp_path / "WAVs")
    flac_root = str(tmp_path / "Library")
    tab.settings.set(default_source_dir=wav_root, default_output_dir=flac_root)

    tab.apply_release(_release())

    suggested = tab.folder_edit.text()
    assert suggested.startswith(wav_root), suggested
    assert flac_root not in suggested, "a capture was aimed at the finished library"


def test_with_no_wav_root_the_suggestion_does_not_fall_back_to_the_library(
        qapp, tmp_path):
    """Better to suggest nothing than to suggest the wrong tree."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    flac_root = str(tmp_path / "Library")
    tab.settings.set(default_source_dir="", default_output_dir=flac_root,
                     record_output_dir="")
    tab.folder_edit.setText("")

    tab.apply_release(_release())

    assert flac_root not in tab.folder_edit.text()
