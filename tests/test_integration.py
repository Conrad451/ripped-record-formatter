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
    assert labels == ["Full Rip", "Convert", "Re-tag", "Metadata", "Settings"]


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


def test_full_rip_single_track_warns(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    fr._expected_n = 1
    fr._warn_single_track()
    assert "Single track" in w.log.toPlainText()


def test_full_rip_two_step_encode_gate(qapp, tmp_path):
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    assert not fr.encode_button.isEnabled()          # nothing split yet
    seg = tmp_path / "track_01.wav"
    seg.write_bytes(b"x")
    fr._expected_titles = ["First", "Second"]
    fr._on_split_done([tmp_path / "track_01.wav", tmp_path / "track_02.wav"])
    assert fr.encode_button.isEnabled()
    assert fr.encode_button.text() == "Encode 2 tracks"
    assert [r.title for r in fr.model.rows()] == ["First", "Second"]


def test_waveform_click_to_place_lands_at_click(qapp):
    from PySide6.QtCore import QPointF, Qt
    from gui.waveform import WaveformView

    view = WaveformView()

    class _Click:
        def double(self):
            return False

        def button(self):
            return Qt.MouseButton.LeftButton

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
