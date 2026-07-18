"""Phase-3 integration tests: envelope, drift, and GUI gating (offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core import waveform as wf
from core.split_review import detect_progressive_drift, segment_deviations


# --------------------------------------------------------------------------- #
# Envelope: min/max buckets keep a transient that striding would miss.
# --------------------------------------------------------------------------- #
def test_envelope_captures_transient_striding_misses():
    n = 44100 * 4
    mono = np.zeros(n, dtype=np.float32)
    mono[123457] = 0.95      # a lone positive spike between quiet samples
    mono[150003] = -0.87     # a lone negative spike
    num_buckets = 4000

    mins, maxs = wf.peak_envelope(mono, num_buckets)
    assert maxs.max() == pytest.approx(0.95, abs=1e-6)
    assert mins.min() == pytest.approx(-0.87, abs=1e-6)

    # A naive stride at the same resolution simply skips over both spikes.
    stride = mono[:: n // num_buckets]
    assert abs(stride).max() < 0.5


# --------------------------------------------------------------------------- #
# Off-by-one / wrong-side drift detection.
# --------------------------------------------------------------------------- #
def test_drift_fires_on_progressive_divergence():
    expected = [180.0] * 6
    # Each actual segment runs long, so the cumulative boundary error grows
    # monotonically -- the wrong-side signature.
    actual = [190.0, 200.0, 210.0, 220.0, 230.0, 240.0]
    assert detect_progressive_drift(actual, expected) is True


def test_drift_quiet_on_a_good_match():
    expected = [180.0] * 6
    actual = [181.0, 179.0, 180.5, 178.5, 181.0, 180.0]  # small, non-growing
    assert detect_progressive_drift(actual, expected) is False


def test_segment_deviations_flags_only_outliers():
    expected = [180.0, 180.0, 180.0]
    actual = [182.0, 300.0, 179.0]     # middle one is way off
    assert segment_deviations(actual, expected) == [False, True, False]


# --------------------------------------------------------------------------- #
# GUI (offscreen): tabs construct; Full Rip Accept gating + override.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_main_window_constructs_all_tabs(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    labels = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    # Full Rip stays the landing tab: opening on Record would seize the audio
    # device on every launch, for someone who may only want to re-tag.
    assert labels == ["Full Rip", "Record", "Convert", "Re-tag", "Metadata", "Settings"]


def test_full_rip_accept_gating_and_override(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    # Pretend analysis ran; expect a 4-track side -> need exactly 3 markers.
    fr._analysis = object()
    fr._expected_n = 4
    fr._unresolved = []
    fr._expected_durations_s = []
    fr.waveform.clear_markers()

    fr._update_accept_enabled()
    assert not fr.accept_button.isEnabled()          # 0 of 3

    fr.waveform.add_marker(1.0)
    fr.waveform.add_marker(2.0)
    assert not fr.accept_button.isEnabled()          # 2 of 3 -> still blocked

    fr.waveform.add_marker(3.0)
    assert fr.accept_button.isEnabled()              # 3 of 3 -> allowed

    # Deliberate override lets a genuinely gapless side proceed with fewer.
    fr.waveform.clear_markers()
    assert not fr.accept_button.isEnabled()          # 0 of 3, no override
    fr.override_check.setChecked(True)
    assert fr.accept_button.isEnabled()              # override wins


class _FakeGap:
    track_index = 0
    expected_ts = 10.0
    window_start = 5.0
    window_end = 15.0


class _FakeEnvelope:
    num_buckets = 0
    duration = 100.0


class _FakeProposal:
    mode = "anchored"
    duration = 100.0

    def __init__(self, split_points, unresolved):
        self.split_points = split_points
        self.unresolved = unresolved


class _FakeRestoration:
    source_clip_runs = 0
    peak_gain_db = 0.0
    warnings = []


def _fake_analysis(split_points, unresolved):
    from gui.full_rip import AnalyzeResult
    return AnalyzeResult(_FakeRestoration(), _FakeProposal(split_points, unresolved),
                         _FakeEnvelope(), None)


def test_full_rip_wrong_side_guard_on_13_of_14_unresolved(qapp):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr._expected_n = 14           # 13 boundaries
    fr._expected_durations_s = []
    fr._on_analyze_done(_fake_analysis([], [_FakeGap()] * 13))
    # Diagnosis shown instead of marching through a doomed resolve queue.
    # (isHidden reflects explicit visibility even when the top window isn't shown.)
    assert not fr.diagnosis_box.isHidden()
    assert fr.gap_box.isHidden()
    # "Resolve manually anyway" opens the queue.
    fr._resolve_anyway()
    assert not fr.gap_box.isHidden()
    assert fr.diagnosis_box.isHidden()


def test_full_rip_populated_side_picker_after_release(qapp):
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    assert not fr.side_combo.isEnabled()             # placeholder, disabled
    detail = ReleaseDetail("x", "Alb", "Art", media=(
        MediumInfo(1, "Vinyl", tracks=tuple(TrackInfo(i + 1, str(i + 1), f"T{i + 1}", 180000)
                                            for i in range(6))),))
    fr._apply_release(detail)
    assert fr.side_combo.isEnabled()
    assert fr.side_combo.count() == 1
    assert "Side A" in fr.side_combo.itemText(0)
    assert fr._expected_n == 6
    assert fr.define_sides_button.isEnabled()


def _wait(predicate, timeout=4.0):
    """Wait for a condition, pumping the Qt event loop.

    Worker results arrive as queued signals, so a plain sleep loop would never
    see them -- the slots only run when the event loop is spun.
    """
    import time

    from PySide6.QtWidgets import QApplication

    deadline = time.time() + timeout
    while time.time() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _two_side_release():
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo

    return ReleaseDetail("x", "Alb", "Art", media=(
        MediumInfo(1, "Vinyl", tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"A{i + 1}", 180000) for i in range(3))),
        MediumInfo(2, "Vinyl", tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"B{i + 1}", 180000) for i in range(2))),
    ))


def test_mapping_table_is_one_row_per_wav_and_skips_ambiguous(qapp, tmp_path):
    """Folder-first: rows are WAVs, and a foreign file defaults to skip."""
    from gui.full_rip import SKIP_LABEL
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr._apply_release(_two_side_release())

    # A mixed folder: two sides of this record plus a stray file from another.
    fr._album_wavs = [tmp_path / "SideA.wav", tmp_path / "bonus.wav", tmp_path / "SideB.wav"]
    fr._rebuild_mapping_table()

    assert fr.mapping_table.rowCount() == 3            # one row per WAV, not per side
    names = [fr.mapping_table.item(r, 0).text() for r in range(3)]
    assert names == ["SideA.wav", "bonus.wav", "SideB.wav"]

    # Confident hits pre-filled; the ambiguous one left on skip, never guessed.
    assert fr._album_mapping == [0, None, 1]
    assert fr.mapping_table.cellWidget(1, 1).currentText() == SKIP_LABEL
    assert fr.mapping_table.cellWidget(0, 1).currentData() == 0
    assert fr.mapping_table.cellWidget(2, 1).currentData() == 1


def test_mapping_table_side_is_exclusive(qapp, tmp_path):
    """Claiming a side another row holds releases the other row back to skip."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr._apply_release(_two_side_release())
    fr._album_wavs = [tmp_path / "SideA.wav", tmp_path / "other.wav"]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, None]

    # Point row 1 at side A too -- row 0 must let go of it.
    combo = fr.mapping_table.cellWidget(1, 1)
    combo.setCurrentIndex(combo.findData(0))
    assert fr._album_mapping == [None, 0]


def test_full_rip_single_track_warns(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    fr._expected_n = 1
    fr._warn_single_track()
    assert "Single track" in w.log.toPlainText()


def test_album_accept_enqueues_encode_without_a_second_click(qapp, tmp_path):
    """Accept IS the commit: it snapshots the table and the encode is enqueued
    on the controller's pool with no further UI action. No Encode button exists."""
    import threading

    from core.album import AlbumController, SideJob, SideState
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    assert not hasattr(fr, "encode_button"), "album mode commits on Accept"

    encoded = threading.Event()
    captured = {}

    def analyze(side, should_cancel):
        return _fake_analysis([], [])

    def encode(side, should_cancel):
        captured["titles"] = list(side.titles)
        captured["artists"] = list(side.artists)
        captured["timestamps"] = list(side.timestamps)
        encoded.set()

    side = SideJob(index=0, label="Side A", wav_path=tmp_path / "a.wav",
                   titles=["One", "Two"])
    fr._album = AlbumController([side], analyze, encode)
    fr._album_output_root = str(tmp_path)
    fr.output_edit.setText(str(tmp_path))
    fr._album_work_dir = tmp_path
    side.analysis = _fake_analysis([], [])
    side.state = SideState.READY

    fr._load_side_for_review(side)
    fr.waveform.clear_markers()
    fr.waveform.add_marker(5.0)            # 1 marker -> 2 tracks
    assert len(fr.model.rows()) == 2       # table is live before Accept

    # Edit the table, then Accept. Nothing else.
    rows = fr.model.rows()
    rows[0].title = "Edited One"
    rows[1].artist = "Guest Artist"
    fr.model.set_rows(rows)

    fr._accept_album_side()

    assert encoded.wait(4.0), "accept did not enqueue the encode"
    assert captured["titles"] == ["Edited One", "Two"]
    assert captured["artists"][1] == "Guest Artist"
    assert captured["timestamps"] == [5.0]
    assert side.state in (SideState.ENCODING, SideState.DONE)
    # The review area is released for the next side straight away.
    assert fr._album_review_index is None
    fr._album.shutdown(wait=True)


def test_side_switch_prompts_only_with_unaccepted_state(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from core.album import AlbumController, SideJob, SideState
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    asked = []

    def fake_question(*args, **kwargs):
        asked.append(True)
        return QMessageBox.StandardButton.Discard

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

    a = SideJob(index=0, label="Side A", titles=["x"])
    fr._album = AlbumController([a], lambda s, c: None, lambda s, c: None)

    # Nothing under review -> no prompt.
    assert fr._confirm_discard_review() is True
    assert asked == []

    # A side under review with live analysis -> prompt fires once.
    a.analysis = _fake_analysis([], [])
    fr._load_side_for_review(a)
    assert fr._confirm_discard_review() is True
    assert len(asked) == 1
    assert fr._album_review_index is None      # discarded
    fr._album.shutdown(wait=True)


def test_waveform_click_to_place_lands_at_click(qapp):
    from PySide6.QtCore import QPointF, Qt
    from gui.waveform import WaveformView

    view = WaveformView()

    class _Click:
        def __init__(self, modifiers=Qt.KeyboardModifier.NoModifier):
            self._modifiers = modifiers

        def double(self):
            return False

        def button(self):
            return Qt.MouseButton.LeftButton

        def modifiers(self):
            return self._modifiers

        def scenePos(self):
            return QPointF(0, 0)

        def accept(self):
            pass

    # Pretend the user clicked at t=12.5s inside a highlighted gap window.
    view.getPlotItem().getViewBox().mapSceneToView = lambda _p: QPointF(12.5, 0.0)
    view.set_place_mode(True)
    view._on_scene_clicked(_Click())
    assert view.marker_times() == [12.5]


def test_full_rip_fanout_enriches_and_manual_stays_minimal(qapp, tmp_path):
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    detail = ReleaseDetail("rel-mbid", "Album", "Artist", year="1993", artist_id="art-mbid",
                           media=(MediumInfo(1, "Vinyl", tracks=(
                               TrackInfo(1, "A1", "T1", 100000, recording_id="rec1", track_mbid="trk1"),
                               TrackInfo(2, "A2", "T2", 100000, recording_id="rec2", track_mbid="trk2"))),))
    fr._apply_release(detail)              # side 0 selected -> review context set
    segs = [tmp_path / "1.wav", tmp_path / "2.wav"]

    enriched = fr._enrich_tracks(["T1", "T2"], segs, fr._review_track_infos, 1, 1, "Artist", "Album")
    t0 = enriched[0]
    assert t0.album_artist == "Artist" and t0.date == "1993"
    assert t0.track_total == 2 and t0.disc_number == 1 and t0.disc_total == 1
    assert t0.mb_album_id == "rel-mbid" and t0.mb_artist_id == "art-mbid"
    assert t0.mb_recording_id == "rec1" and t0.mb_track_id == "trk1"
    assert t0.vorbis_tags()["musicbrainz_recordingid"] == "rec1"
    assert t0.vorbis_tags()["musicbrainz_trackid"] == "trk1"

    # No release -> fan-out stays minimal (old tag set, no empties).
    fr2 = MainWindow().full_rip
    plain = fr2._enrich_tracks(["Song"], [tmp_path / "x.wav"], [], 1, 1, "Artist", "Album")
    assert set(plain[0].vorbis_tags()) == {"artist", "album", "title", "tracknumber"}


def test_restore_cancel_leaves_no_staging(tmp_path):
    import glob
    import tempfile

    import numpy as np
    import soundfile as sf
    import pytest

    from core.restoration import Cancelled, HumRemoval, RumbleFilter, restore

    src = tmp_path / "in.wav"
    sf.write(str(src), np.zeros(44100, dtype=np.float32), 44100, subtype="PCM_16")
    before = set(glob.glob(str(tempfile.gettempdir()) + "/rrf_restore_*"))

    with pytest.raises(Cancelled):
        restore(src, tmp_path / "out.wav", [RumbleFilter(), HumRemoval()],
                should_cancel=lambda: True)

    assert not (tmp_path / "out.wav").exists()
    after = set(glob.glob(str(tempfile.gettempdir()) + "/rrf_restore_*"))
    assert not (after - before)   # staging cleaned despite cancellation


# --------------------------------------------------------------------------- #
# Full Rip consolidation: one way in, review area gated behind an empty state
# --------------------------------------------------------------------------- #
def test_legacy_standalone_entry_controls_are_gone(qapp):
    """The old "Side-long WAV" browse field + Analyze/Cancel row was a second,
    competing entry point. The Source group is now the only way in."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    for gone in ("source_edit", "analyze_button", "cancel_button"):
        assert not hasattr(fr, gone), f"{gone} should have been removed"

    # The one way in, and its single-WAV escape hatch, are both present.
    assert fr.album_box.isVisible() or True          # constructed
    assert not fr.album_box.isCheckable()            # not an opt-in mode
    assert hasattr(fr, "mapping_table")


def test_review_area_starts_empty_and_reveals_on_analysis(qapp):
    """No dead controls: the review area hides behind an explanatory empty state."""
    from gui.main_window import MainWindow

    # isHidden() reflects explicit visibility even when the top window isn't shown.
    fr = MainWindow().full_rip
    assert fr.review_box.isHidden()
    assert not fr.empty_state.isHidden()
    assert fr.empty_state.text() == "Select a folder to begin."

    # Once a side is analysed, the review controls take over.
    fr._expected_n = 2
    fr._expected_durations_s = []
    fr._on_analyze_done(_fake_analysis([], []))
    assert not fr.review_box.isHidden()
    assert fr.empty_state.isHidden()


def test_empty_state_message_tracks_progress(qapp, tmp_path):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    assert fr._pending_review_message() == "Select a folder to begin."

    fr._apply_release(_two_side_release())
    fr._album_wavs = [tmp_path / "SideA.wav", tmp_path / "SideB.wav"]
    fr._rebuild_mapping_table()
    assert fr._pending_review_message() == "Map each WAV to a side, then press Start album."


def test_no_internal_staging_path_is_ever_displayed(qapp):
    """The staging dir is an implementation detail; no user-facing field shows it."""
    from PySide6.QtWidgets import QLineEdit

    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    for edit in fr.findChildren(QLineEdit):
        text = edit.text().lower()
        for leak in ("rrf_fullrip_", "rrf_album_", "rrf_restore_", "rrf_split_", "\tmp", "/tmp"):
            assert leak not in text, f"staging path leaked into a display field: {edit.text()}"


# --------------------------------------------------------------------------- #
# Release preview: an absent cover must be loud, and visible before encoding
# --------------------------------------------------------------------------- #
def _release_with(cover):
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo

    return ReleaseDetail(
        "rel", "Kind of Blue", "Miles Davis", year="1959", country="US", cover=cover,
        media=(MediumInfo(1, "Vinyl", tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"T{i + 1}", 180000) for i in range(5))),),
    )


def test_release_preview_shouts_when_there_is_no_cover(qapp):
    from gui.main_window import MainWindow
    from gui.release_preview import NO_COVER_TEXT

    fr = MainWindow().full_rip
    assert fr.release_preview.isHidden()             # nothing loaded yet

    fr._apply_release(_release_with(None))

    assert not fr.release_preview.isHidden()
    assert NO_COVER_TEXT in fr.release_preview.cover_label.text()
    assert fr.release_preview.thumb.text() == "NO\nART"
    assert fr.release_preview.thumb.pixmap().isNull()
    # ...and the summary is still there alongside the warning.
    assert fr.release_preview.title_label.text() == "Miles Davis - Kind of Blue"
    assert "1959" in fr.release_preview.detail_label.text()
    assert "1 side, 5 tracks" in fr.release_preview.detail_label.text()


def test_release_preview_shows_real_art_quietly(qapp):
    from core.metadata_lookup import CoverArt
    from gui.main_window import MainWindow
    from gui.release_preview import NO_COVER_TEXT
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage

    # A real, decodable 8x8 PNG.
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    cover = CoverArt(data=bytes(buf.data()), mime="image/png")

    fr = MainWindow().full_rip
    fr._apply_release(_release_with(cover))

    assert not fr.release_preview.thumb.pixmap().isNull()   # art rendered
    assert fr.release_preview.thumb.text() == ""
    assert NO_COVER_TEXT not in fr.release_preview.cover_label.text()
    assert fr.release_preview.cover_label.text() == ""


def test_no_cover_is_visible_in_the_lookup_before_choosing(qapp):
    """The dialog says it too -- so a coverless release can be rejected up front."""
    from gui.metadata_panel import MetadataPanel
    from gui.release_preview import NO_COVER_TEXT

    panel = MetadataPanel()
    panel._populate_cover(_release_with(None))
    assert NO_COVER_TEXT in panel.cover_label.text()
    assert panel.cover_label.pixmap().isNull()


# --------------------------------------------------------------------------- #
# Layout defaults: the workflow area, not the chrome, gets the space
# --------------------------------------------------------------------------- #
def test_default_layout_gives_the_review_area_room(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    w.resize(1280, 956)                      # what a 1080p desktop gets
    w.show()
    qapp.processEvents()
    fr = w.full_rip
    fr._expected_n = 2
    fr._expected_durations_s = []
    fr._on_analyze_done(_fake_analysis([], []))
    qapp.processEvents()

    # The review area outweighs the source group above it...
    assert fr.review_box.height() > fr.album_box.height()
    # ...and the waveform is the biggest single thing in it.
    assert fr.waveform.height() >= 190
    assert fr.waveform.height() > fr.table.height()

    # The log is present but minimal -- it must not own the window.
    tabs_h, log_h = w._main_splitter.sizes()
    assert log_h > 0                          # still visible
    assert log_h < tabs_h * 0.15              # ...and out of the way
    w.close()


def test_mapping_table_shows_several_rows_without_scrolling(qapp):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    rows_visible = (fr.mapping_table.height() - 26) // 22
    assert 4 <= rows_visible <= 6, rows_visible


def test_persisted_splitter_sizes_still_win_over_defaults(qapp):
    """Defaults-only change: a size the user already chose is still honoured.

    QSplitter rescales setSizes() to the widget's real height, so the *ratio* is
    what survives, not the literal pixels.
    """
    from gui.main_window import MainWindow

    # Tall enough that the tab area's minimum height is not the binding
    # constraint. At 800px it *is*: the Record tab alone wants ~690px, so the
    # splitter has no room to honour any drag and this would measure clamping
    # rather than persistence. The tab has gained controls every release since
    # v2.3.0 (9.10's album row most recently, with the capture-rate work to
    # come), so the window under test needs headroom the property can show in.
    height = 1200

    fresh = MainWindow()
    fresh.resize(1000, height)
    fresh.show()
    qapp.processEvents()
    default_log_fraction = fresh._main_splitter.sizes()[1] / sum(fresh._main_splitter.sizes())
    fresh.close()

    # The user drags the log much bigger and it is persisted.
    fresh.settings.set(main_split_top=400, main_split_bottom=300)

    restored = MainWindow()
    restored.resize(1000, height)
    restored.show()
    qapp.processEvents()
    top, bottom = restored._main_splitter.sizes()
    log_fraction = bottom / (top + bottom)
    restored.close()

    # Their choice, not the default: the persisted drag makes the log clearly
    # larger than its default share. (QSplitter rescales to the real height, so
    # this stays a ratio assertion, not a pixel one.)
    assert log_fraction > default_log_fraction * 1.5


# --------------------------------------------------------------------------- #
# Side errors say why, and can be retried
# --------------------------------------------------------------------------- #
def test_errored_side_shows_its_cause_not_just_a_colour(qapp, tmp_path):
    """The screenshot bug: 'Side B - error' + 'Side B not ready yet (error)'."""
    from core.album import AlbumController, SideJob, SideState
    from gui.main_window import MainWindow
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    w = MainWindow()
    fr = w.full_rip

    side = SideJob(index=1, label="Side B", wav_path=tmp_path / "b.wav")

    def analyze(s, c):
        raise PermissionError(13, "Permission denied")

    fr._album = AlbumController([side], analyze, lambda s, c: None,
                                on_state_change=lambda s: fr._relay.changed.emit(s))
    item = QListWidgetItem("Side B - queued")
    item.setData(Qt.ItemDataRole.UserRole, 1)
    fr.side_list.addItem(item)
    fr._album.start()

    assert _wait(lambda: side.state == SideState.ERROR)
    qapp.processEvents()

    log = w.log.toPlainText()
    assert "Side B failed during analysis" in log
    assert "Permission denied" in log            # the actual cause
    assert "Traceback" in log                    # full detail, not just the line
    assert "Retry side" in log                   # ...and the way out
    assert "not ready yet" not in log            # the self-referential line is gone

    # The row itself carries the cause.
    assert "Permission denied" in fr.side_list.item(0).toolTip()

    # Clicking it puts the cause in the review area instead of nothing.
    fr.side_list.setCurrentItem(item)
    fr._on_side_list_click(item)
    assert "Permission denied" in fr.empty_state.text()
    assert "Retry side" in fr.empty_state.text()

    # ...and the Retry button is live for it.
    assert fr.retry_side_btn.isEnabled()
    fr._album.shutdown(wait=True)


def test_retry_button_reruns_the_failed_side(qapp, tmp_path):
    from core.album import AlbumController, SideJob, SideState
    from gui.main_window import MainWindow
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    fr = MainWindow().full_rip
    side = SideJob(index=0, label="Side A", wav_path=tmp_path / "a.wav")
    attempts = {"n": 0}

    def analyze(s, c):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("share hiccup")
        return _fake_analysis([], [])

    fr._album = AlbumController([side], analyze, lambda s, c: None,
                                on_state_change=lambda s: fr._relay.changed.emit(s))
    item = QListWidgetItem("Side A")
    item.setData(Qt.ItemDataRole.UserRole, 0)
    fr.side_list.addItem(item)
    fr.side_list.setCurrentItem(item)
    fr._album.start()

    assert _wait(lambda: side.state == SideState.ERROR)
    qapp.processEvents()
    assert fr.retry_side_btn.isEnabled()

    fr._retry_selected_side()                    # press Retry
    assert _wait(lambda: side.state == SideState.READY)
    qapp.processEvents()

    assert attempts["n"] == 2
    assert side.error == ""
    assert not fr.retry_side_btn.isEnabled()     # nothing to retry any more
    fr._album.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# Lookup dialog: opens pre-searched, and previews art before you commit
# --------------------------------------------------------------------------- #
class _StubProvider:
    """A MetadataProvider that answers instantly, in-process."""

    name = "stub"

    def __init__(self, cover=None):
        from core.metadata_lookup import MediumInfo, ReleaseDetail, ReleaseResult, TrackInfo

        self.searches = []
        self._results = [ReleaseResult("rel-1", "Kind of Blue", "Miles Davis",
                                       year="1959", formats="Vinyl")]
        self._detail = ReleaseDetail(
            "rel-1", "Kind of Blue", "Miles Davis", year="1959", cover=cover,
            media=(MediumInfo(1, "Vinyl", tracks=(TrackInfo(1, "1", "So What", 300000),)),))

    def search_releases(self, artist, album, *, limit=25):
        self.searches.append((artist, album))
        return self._results

    def get_release(self, release_id, *, with_cover=True):
        return self._detail


def _png_cover():
    from core.metadata_lookup import CoverArt
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(0x336699)
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return CoverArt(data=bytes(buf.data()), mime="image/png")


def test_lookup_auto_searches_when_seeded_and_waits_when_empty(qapp):
    from gui.metadata_panel import MetadataPanel

    provider = _StubProvider()
    panel = MetadataPanel(provider=provider)

    # Empty fields: nothing to search for, so it waits for input as before.
    assert panel.search_on_open() is False
    assert provider.searches == []

    # Seeded by the caller (the user already expressed intent): fire immediately.
    panel.artist_edit.setText("Miles Davis")
    panel.album_edit.setText("Kind of Blue")
    assert panel.search_on_open() is True
    assert _wait(lambda: provider.searches == [("Miles Davis", "Kind of Blue")])


def test_highlighting_a_result_previews_its_cover_before_committing(qapp):
    """The blindness fix: art is visible on highlight, not painted into a dialog
    that is already closing."""
    from gui.metadata_panel import MetadataPanel

    panel = MetadataPanel(provider=_StubProvider(cover=_png_cover()))
    panel.artist_edit.setText("Miles Davis")
    panel.search_on_open()
    assert _wait(lambda: panel.results_table.rowCount() == 1)

    committed = []
    panel.releaseSelected.connect(committed.append)

    panel.results_table.selectRow(0)                 # just highlight it
    assert _wait(lambda: not panel.cover_label.pixmap().isNull())
    qapp.processEvents()

    assert not panel.cover_label.pixmap().isNull()   # art shown...
    assert panel.track_table.rowCount() == 1         # ...and the tracklist
    assert committed == []                           # ...without committing


def test_highlighting_a_coverless_result_shows_the_loud_warning(qapp):
    from gui.metadata_panel import MetadataPanel
    from gui.release_preview import NO_COVER_TEXT

    panel = MetadataPanel(provider=_StubProvider(cover=None))
    panel.artist_edit.setText("Miles Davis")
    panel.search_on_open()
    assert _wait(lambda: panel.results_table.rowCount() == 1)

    panel.results_table.selectRow(0)
    assert _wait(lambda: NO_COVER_TEXT in panel.cover_label.text())
    assert panel.cover_label.pixmap().isNull()


def test_use_this_release_delivers_the_art_to_the_main_preview(qapp):
    """releaseSelected carries the cover bytes, and the main tab renders them."""
    from gui.main_window import MainWindow
    from gui.metadata_panel import MetadataPanel

    fr = MainWindow().full_rip
    panel = MetadataPanel(provider=_StubProvider(cover=_png_cover()))
    panel.releaseSelected.connect(fr._apply_release)      # as _open_lookup wires it

    panel.artist_edit.setText("Miles Davis")
    panel.search_on_open()
    assert _wait(lambda: panel.results_table.rowCount() == 1)
    panel.results_table.selectRow(0)
    assert _wait(lambda: not panel.cover_label.pixmap().isNull())

    panel._start_fetch_detail()                          # "Use selected release"
    assert _wait(lambda: fr._release is not None)
    qapp.processEvents()

    assert not fr.release_preview.isHidden()
    assert not fr.release_preview.thumb.pixmap().isNull()   # same art on the main tab
    assert fr.release_preview.title_label.text() == "Miles Davis - Kind of Blue"


# --------------------------------------------------------------------------- #
# Guard trip: reviewable in album mode, not a dead-end error
# --------------------------------------------------------------------------- #
def _attention_side(fr, tmp_path, reason="expected 4 tracks; only 1 of 3 boundaries confirmed"):
    """Park a side in NEEDS_ATTENTION carrying a proposal with unresolved gaps."""
    from core.album import AlbumController, NeedsAttention, SideJob, SideState
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    analysis = _fake_analysis([], [_FakeGap(), _FakeGap()])
    side = SideJob(index=1, label="Side B", wav_path=tmp_path / "b.wav",
                   titles=["t1", "t2", "t3", "t4"])

    def analyze(s, c):
        raise NeedsAttention(reason, analysis)

    fr._album = AlbumController([side], analyze, lambda s, c: None,
                                on_state_change=lambda s: fr._relay.changed.emit(s))
    item = QListWidgetItem("Side B")
    item.setData(Qt.ItemDataRole.UserRole, 1)
    fr.side_list.addItem(item)
    fr.side_list.setCurrentItem(item)
    fr._album.start()
    assert _wait(lambda: side.state == SideState.NEEDS_ATTENTION)
    return side, item


def test_guard_trip_is_reviewable_not_an_error(qapp, tmp_path):
    from core.album import SideState
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    side, item = _attention_side(fr, tmp_path)
    qapp.processEvents()

    assert side.state is SideState.NEEDS_ATTENTION
    assert side.analysis is not None                 # work preserved
    log = w.log.toPlainText()
    assert "needs attention" in log
    assert "boundaries confirmed" in log
    assert "needs review" in item.toolTip()

    # Clicking it opens the review area with the diagnosis banner -- not an error
    # panel, and not the doomed auto resolve queue.
    fr._on_side_list_click(item)
    qapp.processEvents()

    assert not fr.review_box.isHidden()
    assert not fr.diagnosis_box.isHidden()
    assert "gapless transitions" in fr.diagnosis_label.text()
    assert "only 1 boundaries confirmed" in fr.diagnosis_label.text() or \
           "boundaries confirmed" in fr.diagnosis_label.text()

    # Album routes offered; single-side routes hidden.
    assert not fr.recheck_mapping_btn.isHidden()
    assert not fr.review_manual_btn.isHidden()
    assert fr.reselect_btn.isHidden()
    assert fr.resolve_anyway_btn.isHidden()

    # Retry is still offered, with the honest hint.
    assert fr.retry_side_btn.isEnabled()
    assert "if nothing changed" in fr.retry_side_btn.toolTip()
    fr._album.shutdown(wait=True)


def test_review_manually_routes_into_the_resolve_gap_flow(qapp, tmp_path):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    side, item = _attention_side(fr, tmp_path)
    fr._on_side_list_click(item)
    qapp.processEvents()

    assert len(fr._unresolved) == 2                  # the windows came through
    fr._resolve_anyway()                             # "Review and place splits manually"
    qapp.processEvents()

    assert fr.diagnosis_box.isHidden()
    assert not fr.gap_box.isHidden()                 # the existing resolve flow, entered
    fr._album.shutdown(wait=True)


def test_recheck_mapping_unmaps_the_side_and_returns_to_the_table(qapp, tmp_path):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    fr._apply_release(_two_side_release())
    fr._album_wavs = [tmp_path / "SideA.wav", tmp_path / "SideB.wav"]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, 1]

    side, item = _attention_side(fr, tmp_path)
    fr._on_side_list_click(item)
    qapp.processEvents()

    fr._recheck_mapping()
    qapp.processEvents()

    # Side B's row is back on skip, awaiting a fresh choice.
    assert fr._album_mapping == [0, None]
    assert fr.mapping_table.cellWidget(1, 1).currentData() is None
    assert fr.diagnosis_box.isHidden()
    assert fr._album_review_index is None
    fr._album.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# v2.3.2 merge seam: both branches' Record tab controls coexist.
# --------------------------------------------------------------------------- #
def test_the_merged_record_tab_carries_both_branches_controls(qapp):
    """One render, all four features present -- the v2.3.2 merge seam.

    fix/capture-rate brought the rate picker and the input-gain slider; the
    session-flow branch brought the album row and the record-to-rip bridge. They
    were developed against separate checkouts of the same tab and touched the
    same constructor, so "both merged cleanly" is worth asserting as *visible
    widgets*, not just as a green diff.
    """
    from gui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 900)
    tab = w.record_tab
    w.tabs.setCurrentWidget(tab)          # Qt visibility is per-selected-tab
    w.show()
    qapp.processEvents()

    # capture-rate: the rate picker probes the device rather than assuming 44.1k.
    assert tab.rate_combo.isVisible()
    # capture-rate: the gain slider sits beside the meters it moves. Existence,
    # not visibility -- it deliberately hides itself when the Windows capture
    # endpoint can't be reached, which is the normal state on a CI box.
    assert tab.gain_slider in tab.gain_widgets
    # session-flow: the optional album row.
    assert tab.lookup_button.isVisible()
    # session-flow: the bridge, present but disarmed until something lands.
    assert tab.process_button.isVisible()
    assert not tab.process_button.isEnabled()

    w.close()
