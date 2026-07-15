"""Setup-check logic: rate nudge, double-preamp, no-signal -- messages verbatim.

The double-preamp check must NOT fire on the device-open transient; that guard is
the recorder's grace window, exercised here through the real _LevelStats.
"""

from __future__ import annotations

import numpy as np

from core.recorder import STATS_GRACE_S, _LevelStats
from core.setup_check import OK, WARN, check_sample_rate, check_signal

SR = 44100


# --------------------------------------------------------------------------- #
# Sample-rate nudge
# --------------------------------------------------------------------------- #
def test_rate_check_fires_on_a_192k_stereo_device_verbatim():
    r = check_sample_rate(device_rate=192000, capture_rate=0, max_channels=2)
    assert r.status == WARN
    assert r.fix_key == "set_rate_44100"
    assert r.fix_label == "Use 44100"
    assert r.message == ("This device is set to 192000 in Windows. For vinyl you "
                         "almost certainly want 44,100 Hz.")


def test_rate_check_silent_when_effective_rate_is_44100():
    assert check_sample_rate(device_rate=44100, capture_rate=0, max_channels=2) is None
    # Pinned to 44100 wins even if the device's native rate is 192000.
    assert check_sample_rate(device_rate=192000, capture_rate=44100, max_channels=2) is None


def test_rate_check_ignores_mono_devices():
    # A mono device is a microphone, not a stereo line-in turntable feed.
    assert check_sample_rate(device_rate=48000, capture_rate=0, max_channels=1) is None


# --------------------------------------------------------------------------- #
# Signal: too hot / no signal / all-clear
# --------------------------------------------------------------------------- #
def test_signal_too_hot_verbatim():
    r = check_signal(clip_runs=3, peak_dbfs=-2.0)
    assert r.status == WARN
    assert r.message == ("The signal is much too hot. If your turntable has a "
                         "PHONO/LINE switch, make sure it and your USB box aren't "
                         "both set to amplify — only one should.")


def test_signal_no_signal_verbatim():
    r = check_signal(clip_runs=0, peak_dbfs=-90.0)
    assert r.status == WARN
    assert r.message == ("No signal detected. Is the record playing, and are the "
                         "red/white cables in the USB box's inputs?")


def test_signal_all_clear_verbatim():
    r = check_signal(clip_runs=0, peak_dbfs=-8.0)
    assert r.status == OK
    assert r.message == ("Setup looks good. Play the loudest song on the record "
                         "and adjust the volume until the moving line stays below "
                         "the dashed one, then you're ready to record.")


def test_a_single_stray_clip_is_not_a_verdict():
    assert check_signal(clip_runs=1, peak_dbfs=-3.0).status == OK


# --------------------------------------------------------------------------- #
# Regression: the device-open transient must not read as "too hot"
# --------------------------------------------------------------------------- #
def test_double_preamp_ignores_the_device_open_transient():
    """A full-scale burst inside the grace window is the open transient, excluded
    from clip stats -- so the check sees zero clips and does not cry 'too hot'."""
    stats = _LevelStats(2, SR)
    grace = int(SR * STATS_GRACE_S)
    stats.feed(np.ones((grace // 2, 2), dtype=np.float32))   # burst entirely in grace
    assert stats.clips.runs == 0
    assert check_signal(clip_runs=stats.clips.runs, peak_dbfs=-3.0).status == OK


def test_double_preamp_fires_on_real_clipping_past_the_grace_window():
    stats = _LevelStats(2, SR)
    grace = int(SR * STATS_GRACE_S)
    stats.feed(np.zeros((grace + 1, 2), dtype=np.float32))    # step past grace, silently
    # Two separate runs of full-scale program material.
    stats.feed(np.ones((5, 2), dtype=np.float32))
    stats.feed(np.zeros((5, 2), dtype=np.float32))
    stats.feed(np.ones((5, 2), dtype=np.float32))
    assert stats.clips.runs >= 2
    assert check_signal(clip_runs=stats.clips.runs, peak_dbfs=-1.0).status == WARN
