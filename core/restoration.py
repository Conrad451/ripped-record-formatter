"""GUI-agnostic audio restoration pipeline for vinyl rips.

Design notes
------------
* **numpy-native.** Audio is read/written with :mod:`soundfile` and processed as
  ``float32`` numpy arrays. This module deliberately does **not** touch pydub --
  pydub stays confined to convert/tag duty in :mod:`core.converter`.
* **No silent resampling.** Every stage works at the source sample rate; numpy
  stages preserve the source bit depth (soundfile ``subtype``), and the declick
  stage selects a matching PCM codec so ffmpeg doesn't quietly requantise.
* **File-based job staging.** Sources typically live on a network share, so the
  orchestrator first copies the input into a local temp staging dir; every stage
  reads and writes files *there*; only the final result is written back to the
  requested output path. The staging dir is always removed, even on failure or
  Ctrl-C.
* **Stages are objects.** The caller builds an ordered list of configured stage
  objects (``[HumRemoval(...), NoiseReduction(...), Declick()]``) and passes it
  to :func:`restore`. Each stage reads one working WAV and writes the next.

Progress is reported per stage via ``on_progress(stage_name, stage_idx,
total_stages)`` -- intentionally coarse, since this callback will later cross a
Qt thread boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf
from noisereduce import reduce_noise
from scipy.signal import filtfilt, iirnotch

from core.ffmpeg_locator import ensure_ffmpeg

# on_progress(stage_name, stage_idx_1based, total_stages)
ProgressCallback = Callable[[str, int, int], None]


# --------------------------------------------------------------------------- #
# soundfile helpers -- keep sample rate + bit depth stable across numpy stages
# --------------------------------------------------------------------------- #
def _read(path: Path) -> tuple[np.ndarray, int, str]:
    """Read audio as ``float32`` shaped ``(frames, channels)`` + (sr, subtype)."""
    data, samplerate = sf.read(str(path), dtype="float32", always_2d=True)
    subtype = sf.info(str(path)).subtype
    return data, samplerate, subtype


def _write(path: Path, data: np.ndarray, samplerate: int, subtype: str) -> None:
    sf.write(str(path), data, samplerate, subtype=subtype)


_SUBTYPE_TO_PCM_CODEC = {
    "PCM_16": "pcm_s16le",
    "PCM_24": "pcm_s24le",
    "PCM_32": "pcm_s32le",
    "PCM_U8": "pcm_u8",
    "FLOAT": "pcm_f32le",
    "DOUBLE": "pcm_f64le",
}


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
class Stage(ABC):
    """A single restoration step: read ``in_path`` (WAV), write ``out_path``."""

    name: str = "stage"

    @abstractmethod
    def apply(self, in_path: Path, out_path: Path) -> None:  # pragma: no cover
        ...


@dataclass
class HumRemoval(Stage):
    """Remove mains hum with zero-phase IIR notch filters.

    A notch is placed at ``base_freq`` and each harmonic up to ``harmonics``
    (default 60/120/180/240 Hz; set ``base_freq=50`` for 50 Hz regions). ``quality``
    is the notch Q -- higher means narrower, so bass content is barely touched
    (Q=30 at 60 Hz is a ~2 Hz-wide notch). :func:`scipy.signal.filtfilt` makes it
    zero-phase (no group-delay smearing).
    """

    base_freq: float = 60.0
    harmonics: int = 4
    quality: float = 30.0
    name: str = field(default="Hum removal", init=False)

    def apply(self, in_path: Path, out_path: Path) -> None:
        data, samplerate, subtype = _read(in_path)
        nyquist = samplerate / 2.0
        out = data
        for harmonic in range(1, self.harmonics + 1):
            freq = self.base_freq * harmonic
            if freq >= nyquist:
                break
            b, a = iirnotch(freq, self.quality, samplerate)
            out = filtfilt(b, a, out, axis=0)
        _write(out_path, out.astype(np.float32, copy=False), samplerate, subtype)


@dataclass
class NoiseReduction(Stage):
    """Spectral-gating noise reduction profiled from a quiet region.

    Lead-in-groove assumption: by default the noise profile is taken from the
    first ``profile_duration`` seconds starting at ``profile_start`` -- on a
    vinyl rip that is the lead-in groove, which carries only surface/system
    noise and no programme material. Point ``profile_start``/``profile_duration``
    at any other quiet stretch if the lead-in isn't representative.

    ``strength`` (0.0-1.0) maps to noisereduce's ``prop_decrease``; the default
    of 0.5 is deliberately conservative to avoid gurgling artefacts. Runs in
    stationary mode (a fixed profile), per channel.
    """

    strength: float = 0.5
    profile_start: float = 0.0
    profile_duration: float = 2.0
    name: str = field(default="Noise reduction", init=False)

    def apply(self, in_path: Path, out_path: Path) -> None:
        data, samplerate, subtype = _read(in_path)
        frames = data.shape[0]
        start = max(0, int(self.profile_start * samplerate))
        end = min(frames, start + int(self.profile_duration * samplerate))
        if end <= start:  # degenerate region -> fall back to a small head slice
            start, end = 0, min(frames, samplerate)

        out = np.empty_like(data)
        for channel in range(data.shape[1]):
            signal = data[:, channel]
            noise = signal[start:end]
            out[:, channel] = reduce_noise(
                y=signal,
                sr=samplerate,
                y_noise=noise,
                stationary=True,
                prop_decrease=float(self.strength),
            )
        _write(out_path, out.astype(np.float32, copy=False), samplerate, subtype)


@dataclass
class Declick(Stage):
    """Impulse-noise (click/pop) removal via ffmpeg's ``adeclick`` filter.

    This is the one stage that leaves the numpy world: it hands the working WAV
    to ffmpeg (resolved through :mod:`core.ffmpeg_locator`) and reads back a new
    WAV in the same staging dir. The output PCM codec is chosen from the source
    subtype so bit depth is preserved rather than defaulting to 16-bit.
    """

    name: str = field(default="Declick", init=False)

    def apply(self, in_path: Path, out_path: Path) -> None:
        ffmpeg_path, _ = ensure_ffmpeg()
        subtype = sf.info(str(in_path)).subtype
        codec = _SUBTYPE_TO_PCM_CODEC.get(subtype, "pcm_s16le")
        result = subprocess.run(
            [
                str(ffmpeg_path), "-hide_banner", "-nostdin", "-y",
                "-i", str(in_path),
                "-af", "adeclick",
                "-c:a", codec,
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg adeclick failed (exit {result.returncode}):\n{result.stderr}"
            )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class RestorationResult:
    output_path: Path
    samplerate: int
    channels: int
    subtype: str
    stages_applied: list[str] = field(default_factory=list)


def restore(
    input_path: str | Path,
    output_path: str | Path,
    stages: list[Stage],
    on_progress: ProgressCallback | None = None,
) -> RestorationResult:
    """Run ``stages`` in order over ``input_path``, writing ``output_path``.

    The source is copied into a local temp staging dir first; every stage reads
    and writes there; the final working file is copied to ``output_path``. The
    staging dir is removed in a ``finally`` block, so it never leaks -- including
    on stage failure or ``KeyboardInterrupt``.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    staging = Path(tempfile.mkdtemp(prefix="rrf_restore_"))
    try:
        current = staging / f"source{input_path.suffix or '.wav'}"
        shutil.copy2(input_path, current)

        total = len(stages)
        applied: list[str] = []
        for index, stage in enumerate(stages, start=1):
            if on_progress is not None:
                on_progress(stage.name, index, total)
            nxt = staging / f"stage_{index:02d}.wav"
            stage.apply(current, nxt)
            current = nxt
            applied.append(stage.name)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, output_path)

        info = sf.info(str(output_path))
        return RestorationResult(
            output_path=output_path,
            samplerate=info.samplerate,
            channels=info.channels,
            subtype=info.subtype,
            stages_applied=applied,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
