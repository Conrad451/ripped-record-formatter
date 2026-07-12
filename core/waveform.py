"""Peak-envelope computation for waveform display -- pure, GUI-agnostic.

A waveform view can only draw a few thousand columns, but a 25-minute side is
tens of millions of samples. The honest way to decimate *for display* is a
min/max peak envelope: split the samples into ``num_buckets`` contiguous buckets
and keep the true minimum and maximum of each. Naive striding (every k-th
sample) is wrong -- a one-sample click or a quiet transient between the sampled
points simply vanishes, which for a vinyl rip is exactly the content you most
want to see.

This is a *display* decimation only: the audio itself is never touched. The
envelope is computed in O(n) via :func:`numpy.minimum.reduceat` /
``maximum.reduceat`` and cached per file so re-opening a side is instant.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class WaveformEnvelope:
    """Per-bucket min/max peaks plus the axes needed to plot them."""

    mins: np.ndarray          # shape (num_buckets,)
    maxs: np.ndarray          # shape (num_buckets,)
    times: np.ndarray         # bucket-center time in seconds, shape (num_buckets,)
    duration: float           # seconds
    samplerate: int
    num_buckets: int


def peak_envelope(mono: np.ndarray, num_buckets: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(mins, maxs)`` of ``mono`` reduced to ``num_buckets`` buckets.

    Each bucket keeps the true min and max of its samples -- never a stride -- so
    a single-sample spike is preserved in the bucket that contains it. Buckets
    are contiguous and cover every sample.
    """
    mono = np.asarray(mono).reshape(-1)
    n = mono.shape[0]
    if n == 0:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty
    num_buckets = max(1, min(int(num_buckets), n))
    # Strictly-increasing bucket start indices in [0, n); with num_buckets <= n
    # each start is distinct, which reduceat requires.
    starts = (np.arange(num_buckets) * n) // num_buckets
    mins = np.minimum.reduceat(mono, starts).astype(np.float32, copy=False)
    maxs = np.maximum.reduceat(mono, starts).astype(np.float32, copy=False)
    return mins, maxs


def _compute(mono: np.ndarray, samplerate: int, num_buckets: int) -> WaveformEnvelope:
    n = mono.shape[0]
    duration = n / samplerate if samplerate else 0.0
    mins, maxs = peak_envelope(mono, num_buckets)
    buckets = mins.shape[0]
    # Time-stamp each bucket at its center.
    edges = (np.arange(buckets + 1) * n) // max(1, buckets)
    centers = (edges[:-1] + edges[1:]) / 2.0
    times = (centers / samplerate).astype(np.float64) if samplerate else centers
    return WaveformEnvelope(
        mins=mins, maxs=maxs, times=times,
        duration=duration, samplerate=samplerate, num_buckets=buckets,
    )


# --------------------------------------------------------------------------- #
# Per-file cache (keyed by path + mtime + size + bucket count)
# --------------------------------------------------------------------------- #
_CACHE_LIMIT = 8
_cache: "OrderedDict[tuple, WaveformEnvelope]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(path: Path, num_buckets: int) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), stat.st_mtime_ns, stat.st_size, num_buckets)


def load_peak_envelope(wav_path: str | Path, num_buckets: int = 4000) -> WaveformEnvelope:
    """Read ``wav_path`` (channels averaged) and return its cached envelope.

    Safe to call from a worker thread. The result is cached per
    (path, mtime, size, num_buckets), so re-opening the same file is instant;
    editing the file (new mtime/size) invalidates its entry.
    """
    path = Path(wav_path)
    key = _cache_key(path, num_buckets)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit

    data, samplerate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    envelope = _compute(mono, samplerate, num_buckets)

    with _cache_lock:
        _cache[key] = envelope
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return envelope


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
