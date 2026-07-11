"""WAV->FLAC conversion and FLAC re-tagging -- pure logic, no terminal I/O.

Callers pass track data plus an output directory and receive progress through an
``on_progress(current, total, track_name)`` callback (fired *after* each track
finishes) and a structured :class:`BatchResult`. Nothing here prints or prompts.

Fixes carried over from the original ``v2/wav_to_flac.py``:

* Paths are built with :mod:`pathlib`, not hardcoded ``"\\"`` concatenation.
* Progress is reported *after* a track is written, not before.
* Re-tagging never deletes a source file when the source and destination resolve
  to the same path -- it is skipped with a warning instead of destroying data.
* The ``track`` tag comes from :meth:`Tracks.tags` (i.e. ``track.track_num``),
  not a separate running counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from core.tracks import Tracks

if TYPE_CHECKING:
    from core.metadata_lookup import CoverArt

# on_progress(current, total, track_name) -- current is 1-based and counts
# tracks that have *completed*.
ProgressCallback = Callable[[int, int, str], None]


def _embed_cover(flac_path: Path, cover: "CoverArt") -> None:
    """Embed ``cover`` as the FLAC's front-cover picture (idempotent).

    Existing pictures are cleared first so re-tagging replaces rather than
    accumulates art. Raises on failure; callers turn that into a warning so
    cover art never fails a whole batch.
    """
    from mutagen.flac import FLAC, Picture

    picture = Picture()
    picture.type = 3            # ID3 APIC type 3 = front cover
    picture.desc = "front cover"
    picture.mime = cover.mime
    picture.data = cover.data

    flac = FLAC(str(flac_path))
    flac.clear_pictures()
    flac.add_picture(picture)
    flac.save()


@dataclass
class TrackOutcome:
    """What happened to a single track during a batch operation."""

    track: Tracks
    output_path: Path
    source_deleted: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    """Aggregate outcome of a conversion or re-tag batch."""

    outcomes: list[TrackOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def warnings(self) -> list[str]:
        return [w for o in self.outcomes for w in o.warnings]

    def summary(self) -> str:
        """A short human-readable line (convenience for CLI callers)."""
        msg = f"Operations Complete - {self.total} track(s) processed"
        if self.warnings:
            msg += f", {len(self.warnings)} warning(s)"
        return msg


def _prepare_output_dir(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _run_batch(tracks, work, on_progress, max_workers, should_cancel) -> BatchResult:
    """Run ``work(track)`` over ``tracks``, serially or on a bounded thread pool.

    Each track's export/tag/cover is independent, so with ``max_workers > 1`` they
    run concurrently (the encode is a per-track ffmpeg subprocess). ``on_progress``
    fires once per *completed* track with ``(completed_count, total, name)`` --
    order-independent "N of M", never "track K". ``should_cancel`` is polled
    before each submission; in-flight tasks finish and a partial result returns.
    Outcomes keep original track order regardless of completion order.
    """
    tracks = list(tracks)
    total = len(tracks)
    outcomes: list = [None] * total

    if max_workers and max_workers > 1 and total > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for i, track in enumerate(tracks):
                if should_cancel is not None and should_cancel():
                    break
                futures[pool.submit(work, track)] = i
            completed = 0
            for future in as_completed(futures):
                outcome = future.result()
                outcomes[futures[future]] = outcome
                completed += 1
                if on_progress is not None:
                    on_progress(completed, total, outcome.track.track_name)
    else:
        completed = 0
        for i, track in enumerate(tracks):
            if should_cancel is not None and should_cancel():
                break
            outcomes[i] = work(track)
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, outcomes[i].track.track_name)

    result = BatchResult()
    result.outcomes = [o for o in outcomes if o is not None]
    return result


def convert_wavs_to_flacs(
    tracks: Iterable[Tracks],
    output_dir: str | Path,
    on_progress: ProgressCallback | None = None,
    *,
    configure: bool = True,
    cover: "CoverArt | None" = None,
    max_workers: int = 1,
    should_cancel=None,
) -> BatchResult:
    """Convert each track's source WAV to a tagged FLAC in ``output_dir``.

    Source files are left in place. ``on_progress`` fires once per completed
    track (``max_workers > 1`` encodes several at once; progress is "N of M").
    Set ``configure=False`` to skip ffmpeg setup when the caller has already
    configured pydub. If ``cover`` is given it is embedded as the front cover of
    every track; a failure to embed becomes a per-track warning and never fails
    the batch. ``should_cancel`` is polled before each track is submitted.
    """
    if configure:
        from core.ffmpeg_locator import configure_pydub

        configure_pydub()
    from pydub import AudioSegment

    out_dir = _prepare_output_dir(output_dir)

    def work(track):
        dest = out_dir / track.filename()
        audio = AudioSegment.from_wav(str(track.track_wav_loc))
        audio.export(str(dest), format="flac", tags=track.tags())
        outcome = TrackOutcome(track=track, output_path=dest)
        if cover is not None:
            try:
                _embed_cover(dest, cover)
            except Exception as exc:  # art never fails a batch
                outcome.warnings.append(f"Could not embed cover art: {exc}")
        return outcome

    return _run_batch(tracks, work, on_progress, max_workers, should_cancel)


def retag_flacs(
    tracks: Iterable[Tracks],
    output_dir: str | Path,
    on_progress: ProgressCallback | None = None,
    *,
    delete_source: bool = False,
    configure: bool = True,
    cover: "CoverArt | None" = None,
    max_workers: int = 1,
    should_cancel=None,
) -> BatchResult:
    """Re-export each source FLAC into ``output_dir`` with fresh tags.

    ``delete_source`` defaults to ``False`` -- deleting the user's originals is
    opt-in. When it is true the original file is removed after a
    successful write -- *except* when the source and destination resolve to the
    same path, in which case deletion is skipped and a warning is recorded so we
    never destroy the file we just produced. If ``cover`` is given it is embedded
    as the front cover; embed failures become per-track warnings, never batch
    failures. ``max_workers``/``should_cancel`` behave as in
    :func:`convert_wavs_to_flacs`.
    """
    if configure:
        from core.ffmpeg_locator import configure_pydub

        configure_pydub()
    from pydub import AudioSegment

    out_dir = _prepare_output_dir(output_dir)

    def work(track):
        source = Path(track.track_wav_loc)
        dest = out_dir / track.filename()
        audio = AudioSegment.from_file(str(source), "flac")
        audio.export(str(dest), format="flac", tags=track.tags())
        outcome = TrackOutcome(track=track, output_path=dest)
        if cover is not None:
            try:
                _embed_cover(dest, cover)
            except Exception as exc:  # art never fails a batch
                outcome.warnings.append(f"Could not embed cover art: {exc}")
        same_file = source.resolve() == dest.resolve()
        if delete_source and same_file:
            outcome.warnings.append(
                f"Source and destination are the same file "
                f"({dest}); keeping it instead of deleting."
            )
        elif delete_source:
            source.unlink(missing_ok=True)
            outcome.source_deleted = True
        return outcome

    return _run_batch(tracks, work, on_progress, max_workers, should_cancel)
