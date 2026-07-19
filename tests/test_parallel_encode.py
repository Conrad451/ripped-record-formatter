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


# --------------------------------------------------------------------------- #
# One batch runner, shared (v2.4.0: converter and mp3_export had a copy each)
# --------------------------------------------------------------------------- #
def test_both_pipelines_batch_through_the_same_runner():
    """The duplication the 9.12 report flagged, closed.

    The cost of two copies was never the duplicated lines -- it was that
    cancellation and ordering are exactly the semantics you fix in one and
    forget in the other. Asserting they share the implementation is what stops
    them drifting apart again.
    """
    from core import converter, mp3_export
    from core.batch import run_batch

    assert converter.run_batch is run_batch
    assert mp3_export.run_batch is run_batch


def test_the_shared_runner_keeps_input_order_under_concurrency():
    """Outcomes come back in input order however they finished."""
    import time

    from core.batch import run_batch

    class _Out:
        def __init__(self, n):
            self.name = n

    def work(n):
        time.sleep(0.02 if n == 0 else 0.001)   # first item finishes last
        return _Out(str(n))

    seen = []
    outcomes = run_batch(range(5), work, name_of=lambda o: o.name,
                         on_progress=lambda c, t, n: seen.append((c, t)),
                         max_workers=4)

    assert [o.name for o in outcomes] == ["0", "1", "2", "3", "4"]
    # Progress counted completions, not indices: "N of M", strictly increasing.
    assert [c for c, _ in seen] == [1, 2, 3, 4, 5]
    assert all(t == 5 for _, t in seen)


def test_the_shared_runner_stops_submitting_on_cancel():
    from core.batch import run_batch

    class _Out:
        def __init__(self, n):
            self.name = n

    started = []

    def work(n):
        started.append(n)
        return _Out(str(n))

    outcomes = run_batch(range(10), work, name_of=lambda o: o.name,
                         should_cancel=lambda: len(started) >= 3)

    assert len(started) < 10, "cancellation never took effect"
    assert len(outcomes) == len(started)     # partial result, no None holes
