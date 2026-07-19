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

import re
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
from scipy.signal import butter, filtfilt, iirnotch, sosfiltfilt

from core import proc
from core.ffmpeg_locator import ensure_ffmpeg

# on_progress(stage_name, stage_idx_1based, total_stages)
ProgressCallback = Callable[[str, int, int], None]


class Cancelled(RuntimeError):
    """Raised out of :func:`restore` when ``should_cancel`` asks it to stop.

    The staging dir is still removed by ``restore``'s ``finally``, so a cancelled
    run leaves nothing behind.
    """


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


# Every stage-boundary WAV in the staging dir uses this subtype, regardless of
# the source's. Float is transparent: zero-phase filters and spectral gating can
# push samples past +/-1.0, and quantising that overshoot to integer PCM at each
# stage boundary would hard-clip it and compound damage across stages. Only the
# final write (in `restore`) quantises back to the source subtype. Declick, the
# one stage that leaves numpy, uses the matching float PCM codec (pcm_f32le).
_INTERMEDIATE_SUBTYPE = "FLOAT"
_INTERMEDIATE_FFMPEG_CODEC = "pcm_f32le"


def _count_clip_runs(data: np.ndarray, level: float, run_len: int) -> int:
    """Count runs of >= ``run_len`` consecutive frames pinned at full scale.

    A cheap "was this clipped at rip time?" detector. A frame counts as clipped
    when *any* channel reaches ``level``; a genuine over-gained rip leaves long
    flat-topped runs, whereas isolated peaks (one or two samples) are ignored.
    """
    if data.size == 0:
        return 0
    clipped = np.any(np.abs(data) >= level, axis=1)
    if not clipped.any():
        return 0
    padded = np.concatenate(([False], clipped, [False]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return int(np.count_nonzero((ends - starts) >= run_len))


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
class RumbleFilter(Stage):
    """Zero-phase high-pass that drains turntable rumble.

    Rumble is broadband *mechanical* low-frequency noise -- bearing and motor
    vibration coupled through the platter -- living below the musical band,
    typically under ~30 Hz. It is distinct from mains hum (:class:`HumRemoval`),
    which is a set of narrow tones at the mains frequency and its harmonics; a
    notch will not touch rumble and a high-pass will not touch hum.

    Recommended chain position is **first**, before hum/noise stages. Subsonic
    energy is often the loudest thing on the record yet carries no music: it eats
    headroom (worsening the overshoot Part B guards against) and inflates the
    noise-reduction profile with content that isn't really the noise floor.
    Draining it first makes every downstream stage better-behaved. Ordering is
    not enforced here -- it stays the caller's choice.

    Implemented as a Butterworth high-pass in second-order-section form
    (``output='sos'``) for numerical stability at these low cutoffs, applied with
    :func:`scipy.signal.sosfiltfilt` so it is zero-phase -- no group-delay smear
    that would blunt transients (drum hits, plucks).
    """

    cutoff_hz: float = 25.0
    order: int = 4
    name: str = field(default="Rumble filter", init=False)

    def apply(self, in_path: Path, out_path: Path) -> None:
        data, samplerate, _subtype = _read(in_path)
        nyquist = samplerate / 2.0
        if not 0.0 < self.cutoff_hz < nyquist:
            raise ValueError(
                f"cutoff_hz must be in (0, {nyquist}); got {self.cutoff_hz}"
            )
        sos = butter(self.order, self.cutoff_hz / nyquist, btype="highpass", output="sos")
        out = sosfiltfilt(sos, data, axis=0)
        _write(out_path, out.astype(np.float32, copy=False), samplerate, _INTERMEDIATE_SUBTYPE)


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
        data, samplerate, _subtype = _read(in_path)
        nyquist = samplerate / 2.0
        out = data
        for harmonic in range(1, self.harmonics + 1):
            freq = self.base_freq * harmonic
            if freq >= nyquist:
                break
            b, a = iirnotch(freq, self.quality, samplerate)
            out = filtfilt(b, a, out, axis=0)
        _write(out_path, out.astype(np.float32, copy=False), samplerate, _INTERMEDIATE_SUBTYPE)


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
        data, samplerate, _subtype = _read(in_path)
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
        _write(out_path, out.astype(np.float32, copy=False), samplerate, _INTERMEDIATE_SUBTYPE)


#: adeclick's one statistic, as printed to stderr at the default log level:
#:
#:     [Parsed_adeclick_0 @ 0000...] Detected clicks in 1015 of 132300 samples (0.767196%).
#:
#: Note carefully what this counts: *samples in which clicks were detected*, not
#: clicks. The two are not interchangeable and the gap is large -- a test signal
#: with 200 injected impulses reports 1015 samples, and the identical audio in
#: stereo reports 2030, because the total is summed across channels. Anything
#: built on this number has to say "samples", or it is lying about the units.
#: ``adeclip`` (a filter we do not run) prints the same line with "clips";
#: accepted here so a future stage swap does not silently stop reporting.
_ADECLICK_STATS = re.compile(
    r"Detected\s+(?:clicks|clips)\s+in\s+(\d+)\s+of\s+(\d+)\s+samples", re.IGNORECASE
)


@dataclass
class Declick(Stage):
    """Impulse-noise (click/pop) removal via ffmpeg's ``adeclick`` filter.

    This is the one stage that leaves the numpy world: it hands the working WAV
    to ffmpeg (resolved through :mod:`core.ffmpeg_locator`) and reads back a new
    WAV in the same staging dir. Like every other stage it emits a float
    intermediate (``pcm_f32le``), so adeclick's output can overshoot without
    being quantise-clipped; ``restore`` handles the final bit-depth conversion.
    """

    name: str = field(default="Declick", init=False)

    #: Samples repaired / samples examined, read back from adeclick's own stderr
    #: by the last :meth:`apply` call. ``None`` when the stage has not run, or
    #: when this ffmpeg build printed no line we recognised -- an unparsed stat
    #: is reported as unknown, never as zero, because "we could not tell" and
    #: "there was nothing to repair" are different claims to make to a user.
    repaired_samples: int | None = field(default=None, init=False, repr=False)
    total_samples: int | None = field(default=None, init=False, repr=False)

    def apply(self, in_path: Path, out_path: Path) -> None:
        ffmpeg_path, _ = ensure_ffmpeg()
        result = proc.run(
            [
                str(ffmpeg_path), "-hide_banner", "-nostdin", "-y",
                "-i", str(in_path),
                "-af", "adeclick",
                "-c:a", _INTERMEDIATE_FFMPEG_CODEC,
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg adeclick failed (exit {result.returncode}):\n{result.stderr}"
            )
        self.repaired_samples, self.total_samples = self._parse_stats(result.stderr)

    @staticmethod
    def _parse_stats(stderr: str) -> tuple[int | None, int | None]:
        """Pull adeclick's repaired/examined sample counts out of its stderr.

        Returns ``(None, None)`` when no recognised line is present, so a build
        that words this differently degrades to silence rather than to a made-up
        figure. The last match wins: one filter graph per run today, but a
        future multi-input graph would print one line per instance.
        """
        matches = _ADECLICK_STATS.findall(stderr or "")
        if not matches:
            return None, None
        repaired, total = matches[-1]
        return int(repaired), int(total)


# --------------------------------------------------------------------------- #
# Provenance -- a stable, parseable summary of the restoration applied
# --------------------------------------------------------------------------- #
def _fmt_g(value: float) -> str:
    """Shortest stable form: integer when whole (``25``), else minimal decimal
    (``0.5``, ``22.5``). Used for frequencies (Hz) and noise strength."""
    return f"{float(value):g}"


def _fmt_secs(value: float) -> str:
    """Seconds, always one decimal place (``0.0``, ``2.0``) -- fixed so a
    profile window renders identically every run."""
    return f"{float(value):.1f}"


def _format_stage(stage: Stage) -> str:
    if isinstance(stage, RumbleFilter):
        return f"rumble({_fmt_g(stage.cutoff_hz)}Hz,o{int(stage.order)})"
    if isinstance(stage, HumRemoval):
        return f"hum({_fmt_g(stage.base_freq)}Hz,h{int(stage.harmonics)})"
    if isinstance(stage, NoiseReduction):
        return (f"noise({_fmt_g(stage.strength)},"
                f"profile={_fmt_secs(stage.profile_start)}+"
                f"{_fmt_secs(stage.profile_duration)}s)")
    if isinstance(stage, Declick):
        return "declick"
    # Unknown/future stage: a param-less, whitespace-free slug of its name so the
    # field never crashes an encode. The four stages above are the documented set.
    return "".join(str(getattr(stage, "name", "stage")).lower().split())


def format_restoration(stages: list[Stage]) -> str:
    """A compact, stable, human-readable summary of restoration provenance.

    This describes *how the audio in a file was made* so a future reader can
    parse it back years later. It is derived from the actual :class:`Stage`
    objects that processed the audio -- never from config, which may have changed
    between analysis and encode.

    STABLE FORMAT (v1) -- do not reorder fields, rename tokens, or change
    separators/number formatting without introducing a new version. Parsers
    depend on it.

    * The value is either the literal string ``none`` (the audio was encoded
      without restoration) or one or more *stage tokens* joined by ``;``
      (semicolon), in the order the stages were applied. There are no spaces
      anywhere in the value.
    * Each stage token is ``name`` or ``name(params)``:

        - ``rumble(<cutoff>Hz,o<order>)``                e.g. ``rumble(25Hz,o4)``
        - ``hum(<freq>Hz,h<harmonics>)``                 e.g. ``hum(60Hz,h4)``
        - ``noise(<strength>,profile=<start>+<duration>s)``
                                            e.g. ``noise(0.5,profile=0.0+2.0s)``
        - ``declick``                                    (no params)

    * Number formatting is fixed: frequencies (Hz) and strength use the shortest
      form -- an integer when whole (``25``, ``60``), else a minimal decimal
      (``0.5``, ``22.5``); profile times (seconds) always carry one decimal place
      (``0.0``, ``2.0``); orders and harmonic counts are plain integers.
    * A full default chain renders as::

        rumble(25Hz,o4);hum(60Hz,h4);noise(0.5,profile=0.0+2.0s);declick
    """
    if not stages:
        return "none"
    return ";".join(_format_stage(stage) for stage in stages)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutputPolicy:
    """Final-write headroom + source-clip diagnostic tunables.

    The whole settings contract for the final quantisation step, so a GUI can
    expose each field. Nothing here limits or compresses -- an overshoot is
    corrected by a single uniform gain, preserving dynamics.
    """

    headroom_target_dbfs: float = -0.1
    """When the processed peak exceeds full scale, scale it down to this peak."""

    clip_ceiling: float = 1.0
    """Processed peak above this (float full scale) triggers the attenuation."""

    source_clip_level: float = 0.999
    """|sample| at or above this counts as full scale for the source-clip check."""

    source_clip_run_len: int = 3
    """This many consecutive full-scale samples make one source clip run."""


@dataclass
class RestorationResult:
    output_path: Path
    samplerate: int
    channels: int
    subtype: str
    stages_applied: list[str] = field(default_factory=list)
    peak_gain_db: float = 0.0
    """Gain applied at the final write to tame an overshoot (<= 0 dB; 0 if none)."""
    source_clip_runs: int = 0
    """Full-scale runs found in the *source* -- a "clipped at rip" indicator."""
    declick_repaired_samples: int | None = None
    declick_total_samples: int | None = None
    """adeclick's own tally: samples it repaired, out of samples it examined.

    ``None`` when declick did not run, and also when it ran but this ffmpeg build
    printed nothing we could read -- callers must treat absence as "unknown", not
    as zero. These are *samples*, summed across channels, not a count of clicks;
    see :data:`_ADECLICK_STATS` for why the distinction matters.
    """
    warnings: list[str] = field(default_factory=list)

    @property
    def declick_percent(self) -> float | None:
        """Repaired share as a percentage, or ``None`` when genuinely unknown.

        A repaired count of zero is *known*, and returns ``0.0`` -- only a
        missing count (declick off, or an unparsed stat) is ``None``. Callers
        deciding whether to show a receipt should test the count, not this.
        """
        if self.declick_repaired_samples is None or not self.declick_total_samples:
            return None
        return 100.0 * self.declick_repaired_samples / self.declick_total_samples


def restore(
    input_path: str | Path,
    output_path: str | Path,
    stages: list[Stage],
    on_progress: ProgressCallback | None = None,
    policy: OutputPolicy | None = None,
    should_cancel=None,
) -> RestorationResult:
    """Run ``stages`` in order over ``input_path``, writing ``output_path``.

    The source is copied into a local temp staging dir first; every stage reads
    and writes float intermediates *there*. The final write -- the only place we
    quantise back to the source subtype -- measures the processed peak and, if it
    overshot full scale, applies one uniform gain bringing it to
    ``policy.headroom_target_dbfs`` (recorded in :attr:`RestorationResult.peak_gain_db`
    with a warning). The untouched source is also scanned for full-scale runs so
    a caller can tell "clipped at rip" apart from "clipped by the pipeline". The
    staging dir is removed in a ``finally`` block, so it never leaks -- including
    on stage failure or ``KeyboardInterrupt``.
    """
    policy = policy or OutputPolicy()
    input_path = Path(input_path)
    output_path = Path(output_path)
    source_subtype = sf.info(str(input_path)).subtype

    # Cheap clipped-at-rip detector, on the untouched source.
    src_data, _sr, _st = _read(input_path)
    source_clip_runs = _count_clip_runs(
        src_data, policy.source_clip_level, policy.source_clip_run_len
    )
    del src_data

    staging = Path(tempfile.mkdtemp(prefix="rrf_restore_"))
    try:
        current = staging / f"source{input_path.suffix or '.wav'}"
        shutil.copy2(input_path, current)

        total = len(stages)
        applied: list[str] = []
        for index, stage in enumerate(stages, start=1):
            if should_cancel is not None and should_cancel():
                raise Cancelled("restoration cancelled")
            if on_progress is not None:
                on_progress(stage.name, index, total)
            nxt = staging / f"stage_{index:02d}.wav"
            stage.apply(current, nxt)
            current = nxt
            applied.append(stage.name)

        # Final write: read the (float) working file, apply the headroom policy,
        # and quantise to the source subtype -- the only quantisation in the run.
        data, samplerate, _st = _read(current)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        warnings: list[str] = []
        peak_gain_db = 0.0
        if peak > policy.clip_ceiling:
            target = 10.0 ** (policy.headroom_target_dbfs / 20.0)
            gain = target / peak
            data = data * gain
            peak_gain_db = 20.0 * np.log10(gain)
            warnings.append(
                f"output attenuated {abs(peak_gain_db):.1f} dB to prevent clipping"
            )
        if source_clip_runs:
            warnings.append(
                f"source appears clipped ({source_clip_runs} full-scale run(s)); "
                "re-rip with lower gain"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write(output_path, data.astype(np.float32, copy=False), samplerate, source_subtype)

        # Stage.apply returns None by contract, so the declick tally is read back
        # off the instance that ran. No Declick in the list -> both stay None,
        # which is exactly the "absent when declick was off" semantics.
        declick_repaired = declick_total = None
        for stage in stages:
            if isinstance(stage, Declick):
                declick_repaired = stage.repaired_samples
                declick_total = stage.total_samples

        info = sf.info(str(output_path))
        return RestorationResult(
            output_path=output_path,
            samplerate=info.samplerate,
            channels=info.channels,
            subtype=info.subtype,
            stages_applied=applied,
            peak_gain_db=peak_gain_db,
            source_clip_runs=source_clip_runs,
            declick_repaired_samples=declick_repaired,
            declick_total_samples=declick_total,
            warnings=warnings,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
