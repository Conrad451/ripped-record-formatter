"""Encode-time provenance: the RRF_VERSION / RRF_RESTORATION Vorbis comments.

Two layers:

* :func:`core.restoration.format_restoration` -- the stable, parseable summary
  string (each stage type, param rendering, chain order, the ``none`` case).
* The converter seam -- a fresh encode stamps both fields; a re-tag never
  touches them (it carries forward whatever the original encode wrote).
"""

from __future__ import annotations

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from core.converter import convert_wavs_to_flacs, retag_flacs
from core.ffmpeg_locator import configure_pydub
from core.restoration import (
    Declick,
    HumRemoval,
    NoiseReduction,
    RumbleFilter,
    format_restoration,
)
from core.tracks import Tracks
from core.version import __version__

SR = 44100

FULL_DEFAULT_CHAIN = "rumble(25Hz,o4);hum(60Hz,h4);noise(0.5,profile=0.0+2.0s);declick"


# --------------------------------------------------------------------------- #
# format_restoration -- the stable summary string
# --------------------------------------------------------------------------- #
def test_format_none_for_empty_chain():
    assert format_restoration([]) == "none"


def test_format_rumble_default():
    assert format_restoration([RumbleFilter()]) == "rumble(25Hz,o4)"


def test_format_hum_default():
    assert format_restoration([HumRemoval()]) == "hum(60Hz,h4)"


def test_format_noise_default():
    assert format_restoration([NoiseReduction()]) == "noise(0.5,profile=0.0+2.0s)"


def test_format_declick_has_no_params():
    assert format_restoration([Declick()]) == "declick"


def test_format_full_default_chain():
    stages = [RumbleFilter(), HumRemoval(), NoiseReduction(), Declick()]
    assert format_restoration(stages) == FULL_DEFAULT_CHAIN


def test_format_renders_non_default_params():
    stages = [
        RumbleFilter(cutoff_hz=30, order=6),
        HumRemoval(base_freq=50, harmonics=3),
        NoiseReduction(strength=0.8, profile_start=1.5, profile_duration=3.0),
    ]
    assert format_restoration(stages) == (
        "rumble(30Hz,o6);hum(50Hz,h3);noise(0.8,profile=1.5+3.0s)"
    )


def test_format_fractional_frequency_keeps_decimal():
    assert format_restoration([RumbleFilter(cutoff_hz=22.5, order=2)]) == "rumble(22.5Hz,o2)"


def test_format_preserves_chain_order():
    # Order comes from the list, not a fixed sort: reversed in -> reversed out.
    assert format_restoration([Declick(), RumbleFilter()]) == "declick;rumble(25Hz,o4)"


# --------------------------------------------------------------------------- #
# The converter seam
# --------------------------------------------------------------------------- #
def _wav(path):
    sf.write(str(path), np.zeros(SR // 10, dtype=np.float32), SR, subtype="PCM_16")
    return path


def _keys(path):
    return {k.lower() for k in FLAC(str(path)).keys()}


def test_convert_without_restoration_writes_none(tmp_path):
    """A plain Convert (empty stage list) stamps the version and RESTORATION=none."""
    configure_pydub()
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path / "a.wav"))
    res = convert_wavs_to_flacs([t], tmp_path / "out", configure=False, restoration_stages=[])
    assert not res.warnings, res.warnings
    f = FLAC(str(res.outcomes[0].output_path))
    assert f["rrf_version"] == [__version__]
    assert f["rrf_restoration"] == ["none"]


def test_convert_with_stages_writes_formatted_summary(tmp_path):
    """The album-style path stamps the actual stages used, formatted stably."""
    configure_pydub()
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path / "a.wav"))
    res = convert_wavs_to_flacs(
        [t], tmp_path / "out", configure=False,
        restoration_stages=[RumbleFilter(), HumRemoval()],
    )
    f = FLAC(str(res.outcomes[0].output_path))
    assert f["rrf_version"] == [__version__]
    assert f["rrf_restoration"] == ["rumble(25Hz,o4);hum(60Hz,h4)"]


def test_convert_unknown_provenance_writes_nothing(tmp_path):
    """No restoration argument (genuinely unknown) writes no RRF_* at all."""
    configure_pydub()
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path / "a.wav"))
    res = convert_wavs_to_flacs([t], tmp_path / "out", configure=False)
    keys = _keys(res.outcomes[0].output_path)
    assert "rrf_version" not in keys
    assert "rrf_restoration" not in keys


def test_retag_preserves_existing_rrf(tmp_path):
    """Re-tag edits metadata but carries provenance forward untouched."""
    configure_pydub()
    # An app encode that stamps a specific restoration.
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path / "a.wav"))
    res = convert_wavs_to_flacs(
        [t], tmp_path / "enc", configure=False,
        restoration_stages=[NoiseReduction(strength=0.7)],
    )
    encoded = res.outcomes[0].output_path
    assert FLAC(str(encoded))["rrf_restoration"] == ["noise(0.7,profile=0.0+2.0s)"]

    # Re-tag it (changed title) into a different dir; RRF must survive verbatim.
    t2 = Tracks(1, "Song (remaster)", "Album", "Artist", encoded)
    res2 = retag_flacs([t2], tmp_path / "retag", configure=False)
    f = FLAC(str(res2.outcomes[0].output_path))
    assert f["title"] == ["Song (remaster)"]                        # edit landed
    assert f["rrf_version"] == [__version__]                        # provenance kept
    assert f["rrf_restoration"] == ["noise(0.7,profile=0.0+2.0s)"]  # unchanged


def test_retag_of_unstamped_flac_stays_unstamped(tmp_path):
    """Re-tagging a FLAC with no provenance (a pre-app rip) adds none."""
    configure_pydub()
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path / "a.wav"))
    res = convert_wavs_to_flacs([t], tmp_path / "enc", configure=False)  # no RRF
    encoded = res.outcomes[0].output_path
    assert "rrf_version" not in _keys(encoded)

    t2 = Tracks(1, "Song v2", "Album", "Artist", encoded)
    res2 = retag_flacs([t2], tmp_path / "retag", configure=False)
    keys = _keys(res2.outcomes[0].output_path)
    assert "rrf_version" not in keys
    assert "rrf_restoration" not in keys
