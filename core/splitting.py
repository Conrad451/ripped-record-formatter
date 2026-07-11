"""Propose (and, on request, perform) track splits for side-long vinyl rips.

A "side-long rip" is a single WAV holding a whole record side -- several tracks
back to back, separated by short near-silent gaps that sit on the record's noise
floor (not digital zero). This module finds where those gaps are and proposes
cut points; it never cuts on its own.

Two things live here, kept deliberately separate:

* :func:`propose_splits` -- analysis only. Reads the WAV, runs an energy/silence
  pass, and returns timestamps with confidence scores. It writes nothing.
* :func:`execute_split` -- action. Given timestamps the *caller* has chosen, it
  performs sample-accurate cuts, staging through a local temp directory because
  rips typically live on a slow network share.

Design center is 16-bit / 44.1 kHz stereo, read as float32 via ``soundfile`` --
the same convention as the rest of the DSP layer. No terminal I/O: progress is
delivered through an ``on_progress(current, total, name)`` callback, matching
:mod:`core.converter`.

Detection, briefly
------------------
The signal is reduced to a mono energy envelope (per-frame RMS in dBFS, computed
in O(n) via a cumulative sum of squares). Frames below ``silence_threshold_db``
that persist for at least ``min_silence`` seconds form *candidate gaps*. Each gap
gets a confidence from how far below threshold it dips and how long it lasts, so
a true inter-track gap (deep, long) outranks a quiet passage inside a track
(shallow, short).

Two selection modes:

* **Track-count-constrained** (primary) -- given expected track count ``N``,
  return the ``N-1`` highest-confidence gaps. This is what survives the trap
  where a quiet passage inside a track would fool a raw threshold.
* **Raw threshold** (fallback, ``track_count=None``) -- return every gap that
  clears the tunables. Honest but eager: it will split the trap.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

# on_progress(current, total, name) -- 1-based, fired *after* each segment is
# written, mirroring core.converter's ProgressCallback.
ProgressCallback = Callable[[int, int, str], None]

# Confidence normalization references. A gap that dips DEPTH_REF dB below the
# threshold, or lasts DUR_REF s beyond the minimum, saturates that half of the
# score. Chosen for typical vinyl: inter-track gaps fall tens of dB and last
# 1-2 s, intra-track dips are shallower and briefer.
_DEPTH_REF_DB = 20.0
_DUR_REF_S = 2.0

# Floor for the log so a truly-zero frame becomes ~-200 dBFS, not -inf.
_DB_EPS = 1e-10


@dataclass(frozen=True)
class SilenceParams:
    """Tunables for the energy/silence pass. All durations in seconds."""

    silence_threshold_db: float = -40.0
    """Frames quieter than this (dBFS, full scale = 1.0) count as silent."""

    min_silence: float = 1.0
    """A gap must stay silent at least this long to be a candidate."""

    min_track_length: float = 20.0
    """No proposed cut lands within this of another cut or of either end."""

    frame_ms: float = 20.0
    """RMS analysis window length."""

    hop_ms: float = 10.0
    """Stride between successive RMS windows (envelope resolution)."""


@dataclass(frozen=True)
class SplitPoint:
    """One proposed cut."""

    timestamp: float
    """Seconds from the start of the rip."""

    confidence: float
    """0..1 -- higher means more clearly an inter-track gap."""

    sample: int
    """``round(timestamp * samplerate)`` -- the sample-accurate cut index."""


@dataclass
class SplitProposal:
    """Result of :func:`propose_splits`. Carries no file handles or audio."""

    split_points: list[SplitPoint] = field(default_factory=list)
    samplerate: int = 0
    duration: float = 0.0
    mode: str = "threshold"
    """``"count"`` (N-constrained) or ``"threshold"`` (raw fallback)."""

    def timestamps(self) -> list[float]:
        """Just the cut times, in order -- ready to hand to :func:`execute_split`."""
        return [p.timestamp for p in self.split_points]


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def _load_mono(wav_path: str | Path) -> tuple[np.ndarray, int]:
    """Read the whole file as a mono float32 envelope source.

    Channels are averaged. For a 20-minute stereo side that is ~200 MB of
    float32 held only for the duration of the analysis, which is acceptable;
    :func:`execute_split` streams instead of loading whole.
    """
    data, samplerate = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    return mono, samplerate


def _frame_rms_db(
    mono: np.ndarray, samplerate: int, params: SilenceParams
) -> tuple[np.ndarray, np.ndarray, float]:
    """Return (frame_center_times, rms_dbfs, hop_seconds).

    RMS per window is computed from a prefix sum of squares, so the whole
    envelope costs one pass regardless of window/hop overlap.
    """
    frame = max(1, int(round(samplerate * params.frame_ms / 1000.0)))
    hop = max(1, int(round(samplerate * params.hop_ms / 1000.0)))
    n = mono.shape[0]

    if n < frame:
        # Too short to frame: treat the whole clip as a single window.
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64)))) if n else 0.0
        db = np.array([20.0 * np.log10(rms + _DB_EPS)], dtype=np.float64)
        times = np.array([n / (2.0 * samplerate)], dtype=np.float64)
        return times, db, hop / samplerate

    sq = np.square(mono, dtype=np.float64)
    prefix = np.concatenate(([0.0], np.cumsum(sq)))
    starts = np.arange(0, n - frame + 1, hop)
    window_energy = prefix[starts + frame] - prefix[starts]
    rms = np.sqrt(window_energy / frame)
    db = 20.0 * np.log10(rms + _DB_EPS)
    # Time-stamp each window at its center.
    times = (starts + frame / 2.0) / samplerate
    return times, db, hop / samplerate


@dataclass(frozen=True)
class _Gap:
    """An internal candidate silence run before it becomes a SplitPoint."""

    start: float
    end: float
    min_db: float

    @property
    def center(self) -> float:
        return (self.start + self.end) / 2.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def confidence(self, threshold_db: float, min_silence: float) -> float:
        depth = max(0.0, threshold_db - self.min_db)
        depth_conf = min(1.0, depth / _DEPTH_REF_DB)
        dur_conf = min(1.0, max(0.0, self.duration - min_silence) / _DUR_REF_S)
        return 0.5 * depth_conf + 0.5 * dur_conf


def _detect_gaps(
    times: np.ndarray, rms_db: np.ndarray, params: SilenceParams
) -> list[_Gap]:
    """Find maximal runs of sub-threshold frames lasting >= min_silence."""
    silent = rms_db < params.silence_threshold_db
    if not silent.any():
        return []

    # Boundaries of contiguous True runs via a padded diff.
    padded = np.concatenate(([False], silent, [False]))
    edges = np.diff(padded.astype(np.int8))
    run_starts = np.flatnonzero(edges == 1)
    run_ends = np.flatnonzero(edges == -1) - 1  # inclusive last index

    gaps: list[_Gap] = []
    for i0, i1 in zip(run_starts, run_ends):
        start_t = float(times[i0])
        end_t = float(times[i1])
        if end_t - start_t < params.min_silence:
            continue
        gaps.append(_Gap(start=start_t, end=end_t, min_db=float(rms_db[i0 : i1 + 1].min())))
    return gaps


def _select(
    gaps: list[_Gap],
    duration: float,
    params: SilenceParams,
    max_points: int | None,
) -> list[_Gap]:
    """Greedily accept gaps by confidence, keeping cuts well separated.

    A gap is accepted only if its center is at least ``min_track_length`` from
    every already-accepted cut and from both ends of the rip. With
    ``max_points`` set (count mode) selection stops once that many are chosen;
    the strongest gaps are considered first, so the weak trap is left out.
    """
    ranked = sorted(
        gaps,
        key=lambda g: (-g.confidence(params.silence_threshold_db, params.min_silence), g.center),
    )
    chosen: list[_Gap] = []
    mtl = params.min_track_length
    for gap in ranked:
        if max_points is not None and len(chosen) >= max_points:
            break
        c = gap.center
        if c < mtl or c > duration - mtl:
            continue
        if any(abs(c - other.center) < mtl for other in chosen):
            continue
        chosen.append(gap)
    chosen.sort(key=lambda g: g.center)
    return chosen


def propose_splits(
    wav_path: str | Path,
    *,
    track_count: int | None = None,
    params: SilenceParams | None = None,
) -> SplitProposal:
    """Propose cut points for ``wav_path``. Reads the file; writes nothing.

    Pass ``track_count=N`` (primary mode) to get the ``N-1`` highest-confidence
    gaps -- robust against a quiet passage inside a track. Leave it ``None``
    (fallback) to get every gap that clears the tunables, which is eager and may
    over-split.
    """
    params = params or SilenceParams()
    mono, samplerate = _load_mono(wav_path)
    duration = mono.shape[0] / samplerate if samplerate else 0.0

    times, rms_db, _hop_s = _frame_rms_db(mono, samplerate, params)
    gaps = _detect_gaps(times, rms_db, params)

    if track_count is not None and track_count >= 1:
        selected = _select(gaps, duration, params, max_points=track_count - 1)
        mode = "count"
    else:
        selected = _select(gaps, duration, params, max_points=None)
        mode = "threshold"

    points = [
        SplitPoint(
            timestamp=g.center,
            confidence=round(g.confidence(params.silence_threshold_db, params.min_silence), 4),
            sample=int(round(g.center * samplerate)),
        )
        for g in selected
    ]
    return SplitProposal(
        split_points=points,
        samplerate=samplerate,
        duration=duration,
        mode=mode,
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _read_dtype(subtype: str) -> str:
    """Pick a numpy dtype that reproduces the source samples bit-for-bit.

    Reading integer PCM back as its native integer width avoids the
    float32<->int round-trip entirely, so the cut is lossless as well as
    sample-accurate. Anything unrecognized falls back to float32.
    """
    if subtype.startswith("PCM_16"):
        return "int16"
    if subtype in ("PCM_24", "PCM_32", "PCM_S8", "PCM_U8"):
        return "int32"
    if subtype in ("FLOAT",):
        return "float32"
    if subtype in ("DOUBLE",):
        return "float64"
    return "float32"


def _segment_bounds(timestamps, samplerate: int, total_frames: int) -> list[tuple[int, int]]:
    """Turn cut times into ``(start, stop)`` sample ranges covering the file.

    Times are rounded to the nearest sample, clamped inside the file, sorted,
    and de-duplicated. ``k`` in-range cuts yield ``k+1`` segments.
    """
    cuts = sorted(
        {
            min(max(int(round(t * samplerate)), 1), total_frames - 1)
            for t in timestamps
            if 0 < t * samplerate < total_frames
        }
    )
    bounds: list[tuple[int, int]] = []
    prev = 0
    for cut in cuts:
        bounds.append((prev, cut))
        prev = cut
    bounds.append((prev, total_frames))
    return bounds


def execute_split(
    wav_path: str | Path,
    timestamps,
    output_dir: str | Path,
    on_progress: ProgressCallback | None = None,
    *,
    name_template: str = "track_{:02d}.wav",
) -> list[Path]:
    """Cut ``wav_path`` at ``timestamps`` into WAV segments in ``output_dir``.

    ``timestamps`` are the caller's chosen cut times in seconds (typically
    ``SplitProposal.timestamps()``); ``k`` cuts produce ``k+1`` files. Cuts are
    sample-accurate and lossless -- samples are read in their native format and
    written back with the source's samplerate and subtype.

    Because rips usually sit on a network share, the work is staged locally: the
    source is copied into a temp dir, every segment is cut and written there,
    each finished segment is moved out to ``output_dir``, and the temp dir is
    removed in a ``finally`` no matter what. ``on_progress`` fires after each
    segment is written, 1-based.
    """
    wav_path = Path(wav_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    info = sf.info(str(wav_path))
    samplerate = info.samplerate
    subtype = info.subtype
    read_dtype = _read_dtype(subtype)
    bounds = _segment_bounds(timestamps, samplerate, info.frames)

    tmp = Path(tempfile.mkdtemp(prefix="rrf_split_"))
    outputs: list[Path] = []
    try:
        local_src = tmp / wav_path.name
        shutil.copy2(wav_path, local_src)

        total = len(bounds)
        with sf.SoundFile(str(local_src)) as src:
            for index, (start, stop) in enumerate(bounds, start=1):
                src.seek(start)
                block = src.read(stop - start, dtype=read_dtype, always_2d=True)
                name = name_template.format(index)
                local_out = tmp / name
                sf.write(str(local_out), block, samplerate, subtype=subtype)

                final = output_dir / name
                shutil.move(str(local_out), str(final))
                outputs.append(final)
                if on_progress is not None:
                    on_progress(index, total, name)
        return outputs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
