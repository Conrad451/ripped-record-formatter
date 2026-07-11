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
