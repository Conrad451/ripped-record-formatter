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

Three selection modes, the caller's choice (anchored > count > threshold):

* **Duration-anchored** (best, :func:`propose_splits_anchored`) -- given expected
  per-track durations (e.g. from MusicBrainz), predict roughly where each gap
  falls, search a window around each prediction for the true gap, and re-anchor
  the next prediction on the confirmed position so error never compounds.
* **Track-count-constrained** (:func:`propose_splits`, ``track_count=N``) --
  return the ``N-1`` highest-confidence gaps. Survives the trap where a quiet
  passage inside a track would fool a raw threshold.
* **Raw threshold** (fallback, ``track_count=None``) -- return every gap that
  clears the tunables. Honest but eager: it will split the trap.

Why anchored is worth the extra input: durations only *approximately* locate
gaps. Three errors stack -- unknown inter-track deadspace, CD-sourced durations
that don't match the pressing, and turntable speed error (+/-1-2%, cumulative).
So durations define *search windows*, energy analysis finds the true gap inside
each, and re-anchoring on every confirmed gap keeps the +/-1% speed drift from
compounding across a side. A window with no qualifying dip (a genuine crossfade)
becomes an explicit :class:`UnresolvedGap` marker instead of aborting the search.
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


@dataclass(frozen=True)
class SilenceParams:
    """Every tunable for split proposal, defaults chosen for 16/44.1 vinyl.

    This is the whole settings contract for the splitter -- detection *and*
    confidence scoring -- so a GUI can expose each field directly. No behavioural
    constant is hidden in a function body. Durations are in seconds.
    """

    # --- energy / silence detection ---
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

    db_floor_eps: float = 1e-10
    """Floor added before the log so a truly-zero frame reads ~-200 dBFS, not -inf."""

    # --- confidence scoring (all modes) ---
    depth_ref_db: float = 20.0
    """Dip this far below the threshold to saturate the depth half of the score."""

    duration_ref_s: float = 2.0
    """Last this long beyond ``min_silence`` to saturate the duration half."""

    quality_depth_weight: float = 0.5
    """Weight of depth vs duration within a gap's quality score (0..1)."""

    confidence_round_digits: int = 4
    """Decimal places the reported confidence is rounded to."""

    # --- anchored mode only ---
    proximity_weight: float = 0.5
    """Weight of proximity-to-prediction vs gap quality in anchored confidence (0..1)."""

    post_miss_penalty: float = 0.8
    """Confidence multiplier for the first gap confirmed after a missed one."""


@dataclass(frozen=True)
class SplitPoint:
    """One proposed cut."""

    timestamp: float
    """Seconds from the start of the rip."""

    confidence: float
    """0..1 -- higher means more clearly an inter-track gap."""

    sample: int
    """``round(timestamp * samplerate)`` -- the sample-accurate cut index."""


@dataclass(frozen=True)
class UnresolvedGap:
    """A predicted gap the energy pass could not confirm inside its window.

    Emitted only by anchored mode. Carries enough for a UI to zoom to the
    window and ask the user to place the cut by hand; the search does not stop
    on one of these -- the next prediction re-anchors on the expected position.
    """

    track_index: int
    """Which gap this is: the boundary after track ``track_index``."""

    expected_ts: float
    """Predicted gap location (seconds) the window was centered on."""

    window_start: float
    window_end: float
    """The searched span (seconds), clamped to the file."""


@dataclass
class SplitProposal:
    """Result of any proposal call. Carries no file handles or audio."""

    split_points: list[SplitPoint] = field(default_factory=list)
    samplerate: int = 0
    duration: float = 0.0
    mode: str = "threshold"
    """``"anchored"``, ``"count"`` (N-constrained), or ``"threshold"`` (fallback)."""

    unresolved: list[UnresolvedGap] = field(default_factory=list)
    """Predicted gaps anchored mode could not confirm; empty for other modes."""

    def timestamps(self) -> list[float]:
        """Just the confirmed cut times, in order -- ready for :func:`execute_split`.

        In anchored mode this excludes any :class:`UnresolvedGap`; the caller is
        expected to resolve those first if it wants a complete cut list.
        """
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
        db = np.array([20.0 * np.log10(rms + params.db_floor_eps)], dtype=np.float64)
        times = np.array([n / (2.0 * samplerate)], dtype=np.float64)
        return times, db, hop / samplerate

    sq = np.square(mono, dtype=np.float64)
    prefix = np.concatenate(([0.0], np.cumsum(sq)))
    starts = np.arange(0, n - frame + 1, hop)
    window_energy = prefix[starts + frame] - prefix[starts]
    rms = np.sqrt(window_energy / frame)
    db = 20.0 * np.log10(rms + params.db_floor_eps)
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

    def quality(self, params: SilenceParams) -> float:
        """Gap quality in 0..1: how deep the dip is blended with how long it lasts."""
        depth = max(0.0, params.silence_threshold_db - self.min_db)
        depth_conf = min(1.0, depth / params.depth_ref_db)
        dur_conf = min(1.0, max(0.0, self.duration - params.min_silence) / params.duration_ref_s)
        w = params.quality_depth_weight
        return w * depth_conf + (1.0 - w) * dur_conf


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
        key=lambda g: (-g.quality(params), g.center),
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

    # In count/threshold mode there is no prediction to be near, so confidence is
    # just the gap's quality.
    points = [
        SplitPoint(
            timestamp=g.center,
            confidence=round(g.quality(params), params.confidence_round_digits),
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


def _anchored_confidence(
    gap: _Gap, pred_start: float, half: float, params: SilenceParams
) -> float:
    """Blend gap quality (depth + duration) with agreement to the prediction.

    Quality says "this is a real inter-track gap"; proximity says "and it landed
    where the durations said it would". A deep, long gap right on the predicted
    boundary scores near 1; a shallow one at the window's edge scores low.
    """
    prox_conf = max(0.0, 1.0 - abs(gap.start - pred_start) / half) if half > 0 else 1.0
    w = params.proximity_weight
    return (1.0 - w) * gap.quality(params) + w * prox_conf


def propose_splits_anchored(
    wav_path: str | Path,
    expected_durations_ms: list[int],
    *,
    params: SilenceParams | None = None,
    window_s: float = 15.0,
    speed_tolerance: float = 0.02,
) -> SplitProposal:
    """Propose cuts using expected per-track durations to steer the search.

    ``expected_durations_ms`` is one duration per track (e.g. from MusicBrainz);
    ``N`` tracks imply ``N-1`` internal gaps. For each gap in order, predict its
    position from the *previous confirmed gap* plus this track's duration, search
    a window around that prediction (base ``window_s``, widened by
    ``speed_tolerance`` x the elapsed track time to cover turntable speed drift),
    and take the deepest qualifying silence run found there.

    Re-anchoring on each confirmed gap is the whole point: speed error and
    deadspace are measured fresh from the last real gap every step, so they never
    accumulate down the side. A window with no qualifying dip yields an
    :class:`UnresolvedGap` (with its bounds) and the search continues, re-anchored
    on the predicted position; the next confirmed gap is flagged with reduced
    confidence. Reads the file; writes nothing.
    """
    params = params or SilenceParams()
    mono, samplerate = _load_mono(wav_path)
    duration = mono.shape[0] / samplerate if samplerate else 0.0

    # Same envelope + gap machinery as the other modes -- computed once.
    times, rms_db, _hop_s = _frame_rms_db(mono, samplerate, params)
    gaps = _detect_gaps(times, rms_db, params)

    durations_s = [d / 1000.0 for d in expected_durations_ms]
    n_gaps = max(0, len(durations_s) - 1)

    found: list[SplitPoint] = []
    unresolved: list[UnresolvedGap] = []
    anchor = 0.0  # position of the last confirmed gap (0 = start of side)
    post_miss = False

    for k in range(n_gaps):
        elapsed = durations_s[k]  # nominal length of the track ending at gap k
        pred_start = anchor + elapsed
        half = window_s + speed_tolerance * elapsed
        w_start = max(anchor, pred_start - half)
        w_end = min(duration, pred_start + half)

        # Candidate = a detected gap that begins inside the window.
        cands = [g for g in gaps if w_start <= g.start <= w_end]
        if not cands:
            unresolved.append(
                UnresolvedGap(
                    track_index=k,
                    expected_ts=pred_start,
                    window_start=w_start,
                    window_end=w_end,
                )
            )
            anchor = pred_start  # re-anchor on the prediction; do NOT abort
            post_miss = True
            continue

        # Deepest dip wins; ties break toward the prediction.
        best = min(cands, key=lambda g: (g.min_db, abs(g.start - pred_start)))
        conf = _anchored_confidence(best, pred_start, half, params)
        if post_miss:
            conf *= params.post_miss_penalty
            post_miss = False

        found.append(
            SplitPoint(
                timestamp=best.center,
                confidence=round(conf, params.confidence_round_digits),
                sample=int(round(best.center * samplerate)),
            )
        )
        anchor = best.end  # confirmed: next track starts at this gap's end

    return SplitProposal(
        split_points=found,
        samplerate=samplerate,
        duration=duration,
        mode="anchored",
        unresolved=unresolved,
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
