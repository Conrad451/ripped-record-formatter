"""Tests for core.splitting on synthetic side-long rips.

The fixtures build multi-track WAVs the way a real vinyl side looks: loud tones
separated by short gaps that sit on a *noise floor* (vinyl hiss), never digital
silence. One fixture plants the trap the design calls out -- a brief quiet
passage inside a track that a raw threshold wrongly splits but track-count mode
survives.

All signals are 16-bit / 44.1 kHz, generated with a seeded RNG so runs are
deterministic.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from core.splitting import (
    SilenceParams,
    execute_split,
    propose_splits,
)

SR = 44100
_RNG = np.random.RandomState(1234)


def _dbfs_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _noise(n: int, db: float) -> np.ndarray:
    """Zero-mean white noise whose RMS is approximately ``db`` dBFS."""
    return _RNG.normal(0.0, _dbfs_to_amp(db), n).astype(np.float64)


def _tone(seconds: float, freq: float, db: float) -> np.ndarray:
    n = int(round(seconds * SR))
    t = np.arange(n) / SR
    amp = _dbfs_to_amp(db) * np.sqrt(2.0)  # sine RMS = amp / sqrt(2)
    return amp * np.sin(2.0 * np.pi * freq * t)


def _write(path, signal: np.ndarray) -> None:
    peak = np.max(np.abs(signal))
    if peak >= 1.0:  # keep it inside [-1, 1) so PCM_16 never clips
        signal = signal * (0.999 / peak)
    sf.write(str(path), signal.astype(np.float32), SR, subtype="PCM_16")


def _build_rip(track_secs, gap_secs, floor_db=-55.0, tone_db=-9.0, traps=None):
    """Assemble a rip and return (signal, ground_truth_gap_centers_in_seconds).

    ``traps`` maps a track index -> (offset_seconds, duration_seconds); within
    that track the tone is dropped to a shallow level so it dips below a typical
    threshold without reaching the real noise floor.
    """
    traps = traps or {}
    freqs = [220.0, 330.0, 440.0, 550.0, 660.0, 770.0]
    pieces: list[np.ndarray] = []
    truth: list[float] = []
    cursor = 0.0

    for i, dur in enumerate(track_secs):
        track = _tone(dur, freqs[i % len(freqs)], tone_db)
        if i in traps:
            off, tdur = traps[i]
            s = int(round(off * SR))
            e = int(round((off + tdur) * SR))
            # Replace the loud tone with a much quieter one over the trap span.
            track[s:e] = _tone(tdur, freqs[i % len(freqs)], -45.0)
        pieces.append(track)
        cursor += dur

        if i < len(track_secs) - 1:
            gdur = gap_secs[i]
            pieces.append(np.zeros(int(round(gdur * SR))))
            truth.append(cursor + gdur / 2.0)  # ground-truth cut = gap midpoint
            cursor += gdur

    signal = np.concatenate(pieces)
    signal = signal + _noise(signal.shape[0], floor_db)  # vinyl hiss everywhere
    return signal, truth


def _assert_near(proposed, truth, tol=0.25):
    assert len(proposed) == len(truth), f"count {len(proposed)} != {len(truth)}"
    for p, t in zip(sorted(proposed), sorted(truth)):
        assert abs(p - t) <= tol, f"cut {p:.3f}s not within {tol}s of truth {t:.3f}s"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_count_mode_finds_clean_gaps(tmp_path):
    """Three tracks, two clean gaps -> exactly two cuts near the midpoints."""
    signal, truth = _build_rip(track_secs=[4.0, 4.0, 4.0], gap_secs=[1.5, 1.5])
    wav = tmp_path / "clean.wav"
    _write(wav, signal)

    params = SilenceParams(silence_threshold_db=-40.0, min_silence=0.6, min_track_length=2.0)
    prop = propose_splits(wav, track_count=3, params=params)

    assert prop.mode == "count"
    assert prop.samplerate == SR
    _assert_near(prop.timestamps(), truth)
    # Sample index must agree with the timestamp.
    for pt in prop.split_points:
        assert pt.sample == round(pt.timestamp * SR)
        assert 0.0 <= pt.confidence <= 1.0


def test_threshold_mode_oversplits_the_trap(tmp_path):
    """Raw threshold mode is honest but eager: it splits the intra-track dip."""
    signal, truth = _build_rip(
        track_secs=[4.0, 5.0, 4.0],
        gap_secs=[1.5, 1.5],
        traps={1: (2.0, 0.5)},  # 0.5 s quiet passage 2 s into track 2
    )
    wav = tmp_path / "trap.wav"
    _write(wav, signal)

    params = SilenceParams(silence_threshold_db=-40.0, min_silence=0.4, min_track_length=2.0)
    prop = propose_splits(wav, track_count=None, params=params)

    assert prop.mode == "threshold"
    # Two real gaps + the trap = three cuts. Over-split, as designed.
    assert len(prop.timestamps()) == len(truth) + 1


def test_count_mode_survives_the_trap(tmp_path):
    """Same trap signal: told there are 3 tracks, the trap is outranked."""
    signal, truth = _build_rip(
        track_secs=[4.0, 5.0, 4.0],
        gap_secs=[1.5, 1.5],
        traps={1: (2.0, 0.5)},
    )
    wav = tmp_path / "trap.wav"
    _write(wav, signal)

    params = SilenceParams(silence_threshold_db=-40.0, min_silence=0.4, min_track_length=2.0)
    prop = propose_splits(wav, track_count=3, params=params)

    _assert_near(prop.timestamps(), truth)
    # The real gaps must be the confident ones and the trap absent.
    trap_center = 4.0 + 1.5 + 2.0 + 0.25  # ~7.75 s
    assert all(abs(p - trap_center) > 0.5 for p in prop.timestamps()), (
        "trap should be excluded in count mode"
    )


def test_min_track_length_rejects_boundary_cuts(tmp_path):
    """A gap too close to the start is not a valid track boundary."""
    # First "track" is only 1 s, so its trailing gap sits inside min_track_length.
    signal, truth = _build_rip(track_secs=[1.0, 6.0], gap_secs=[1.5])
    wav = tmp_path / "edge.wav"
    _write(wav, signal)

    params = SilenceParams(silence_threshold_db=-40.0, min_silence=0.6, min_track_length=3.0)
    prop = propose_splits(wav, track_count=2, params=params)
    # The only gap is ~1.75 s in, closer than 3 s to the start -> rejected.
    assert prop.timestamps() == []


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def test_execute_split_is_sample_accurate_and_lossless(tmp_path):
    """Cuts land on the exact sample and concatenating segments restores the source."""
    signal, truth = _build_rip(track_secs=[4.0, 4.0, 4.0], gap_secs=[1.5, 1.5])
    wav = tmp_path / "src.wav"
    _write(wav, signal)

    cuts = truth  # cut at the two gap midpoints
    out = tmp_path / "out"
    calls: list[tuple[int, int, str]] = []
    outputs = execute_split(
        wav, cuts, out, on_progress=lambda c, t, n: calls.append((c, t, n))
    )

    assert len(outputs) == 3  # two cuts -> three tracks
    assert all(p.exists() for p in outputs)
    # Progress fired once per segment, 1-based, after each write.
    assert calls == [(1, 3, "track_01.wav"), (2, 3, "track_02.wav"), (3, 3, "track_03.wav")]

    # Sample-accurate boundaries: each segment is exactly gap-to-gap in length.
    expected_bounds = [round(c * SR) for c in cuts]
    lengths = [sf.info(str(p)).frames for p in outputs]
    assert lengths[0] == expected_bounds[0]
    assert lengths[1] == expected_bounds[1] - expected_bounds[0]

    # Lossless: re-joining the PCM_16 segments reproduces the original file bytes.
    original = sf.read(str(wav), dtype="int16", always_2d=True)[0]
    rejoined = np.concatenate([sf.read(str(p), dtype="int16", always_2d=True)[0] for p in outputs])
    assert rejoined.shape == original.shape
    assert np.array_equal(rejoined, original)


def test_execute_split_no_timestamps_copies_whole_file(tmp_path):
    """Empty cut list is a valid degenerate case: one segment, same audio."""
    signal, _ = _build_rip(track_secs=[3.0, 3.0], gap_secs=[1.0])
    wav = tmp_path / "src.wav"
    _write(wav, signal)

    outputs = execute_split(wav, [], tmp_path / "out")
    assert len(outputs) == 1
    original = sf.read(str(wav), dtype="int16", always_2d=True)[0]
    result = sf.read(str(outputs[0]), dtype="int16", always_2d=True)[0]
    assert np.array_equal(result, original)


def test_proposal_writes_nothing(tmp_path):
    """propose_splits must never create files -- it only reads and reports."""
    signal, _ = _build_rip(track_secs=[4.0, 4.0], gap_secs=[1.5])
    wav = tmp_path / "src.wav"
    _write(wav, signal)

    before = {p.name for p in tmp_path.iterdir()}
    propose_splits(wav, track_count=2)
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after
