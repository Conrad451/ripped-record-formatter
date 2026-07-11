"""Parallel (bounded-pool) FLAC encoding."""

from __future__ import annotations

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from core.converter import convert_wavs_to_flacs
from core.ffmpeg_locator import configure_pydub
from core.tracks import Tracks


def _make_tracks(tmp_path, n):
    tracks = []
    for i in range(n):
        wav = tmp_path / f"src_{i}.wav"
        # distinct length per track so a mix-up would be detectable
        sf.write(str(wav), np.zeros(2000 + i * 500, dtype=np.float32), 44100, subtype="PCM_16")
        tracks.append(Tracks(i + 1, f"Track {i + 1}", "Album", "Artist", wav))
    return tracks


def test_parallel_encode_all_outputs_correct(tmp_path):
    configure_pydub()
    tracks = _make_tracks(tmp_path, 5)
    seen = []
    result = convert_wavs_to_flacs(
        tracks, tmp_path / "out",
        on_progress=lambda c, t, name: seen.append((c, t)),
        configure=False, max_workers=3,
    )

    assert result.total == 5
    # Progress counted 1..5 of 5 (order-independent; count is monotone).
    assert [t for _, t in seen] == [5] * 5
    assert sorted(c for c, _ in seen) == [1, 2, 3, 4, 5]

    # Every output exists, in original order, correctly tagged.
    for i, outcome in enumerate(result.outcomes):
        assert outcome.output_path.exists()
        tags = FLAC(str(outcome.output_path))
        assert tags["title"] == [f"Track {i + 1}"]
        assert tags["tracknumber"] == [str(i + 1)]


def test_cancel_stops_further_submissions(tmp_path):
    configure_pydub()
    tracks = _make_tracks(tmp_path, 6)
    # Cancel immediately -> nothing submitted, empty (but valid) result.
    result = convert_wavs_to_flacs(
        tracks, tmp_path / "out", configure=False, max_workers=2,
        should_cancel=lambda: True,
    )
    assert result.total == 0
