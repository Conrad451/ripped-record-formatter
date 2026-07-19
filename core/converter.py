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

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from core.batch import run_batch
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


def _write_vorbis_tags(flac_path: Path, tags: dict) -> None:
    """Write ``tags`` as the FLAC's Vorbis comments (authoritative, no empties).

    Existing comments (e.g. whatever pydub/ffmpeg wrote on export) are cleared
    first, so only the fields we actually have are present -- never an empty
    string. Pictures are untouched (handled by :func:`_embed_cover`).
    """
    from mutagen.flac import FLAC

    flac = FLAC(str(flac_path))
    flac.delete()
    for key, value in tags.items():
        flac[key] = value
    flac.save()


def _rrf_tags(restoration_stages, resample=None) -> dict:
    """Provenance Vorbis comments for a freshly *encoded* FLAC.

    Every FLAC the app encodes carries a signature of how it was made:

    * ``RRF_VERSION`` -- the app version that did the encoding.
    * ``RRF_RESTORATION`` -- a stable, parseable summary of the restoration
      actually applied to the audio (:func:`core.restoration.format_restoration`),
      or the literal ``none`` when it was encoded without restoration.

    ``restoration_stages`` is the actual list of restoration ``Stage`` objects
    applied to this audio -- an empty list means "encoded, no restoration"
    (renders ``none``). ``None`` means the caller genuinely does not know how the
    audio was made, so nothing is written -- the same absent-writes-nothing rule
    as every other field.

    ``resample`` is an optional ``(src_rate, dst_rate)`` pair when the encode
    resampled the audio. It is appended additively per the RRF v1 format as a
    ``resample(<src>-><dst>)`` token -- a resample is an encode-time fact, not a
    restoration Stage, so it is assembled here rather than in ``format_restoration``.
    A resample replaces the ``none`` placeholder (a resample *is* provenance).
    """
    if restoration_stages is None:
        return {}
    from core.restoration import format_restoration
    from core.version import __version__

    restoration = format_restoration(restoration_stages)
    if resample is not None:
        token = f"resample({int(resample[0])}->{int(resample[1])})"
        restoration = token if restoration == "none" else f"{restoration};{token}"
    return {
        "rrf_version": __version__,
        "rrf_restoration": restoration,
    }


def _target_rate(output_sample_rate) -> int | None:
    """Parse the ``output_sample_rate`` setting into a target Hz, or None to keep
    the source rate ("source"/empty/unparseable all mean keep source)."""
    if output_sample_rate in (None, "", "source"):
        return None
    try:
        return int(output_sample_rate)
    except (TypeError, ValueError):
        return None


def _read_rrf(flac_path: Path) -> dict:
    """Existing ``RRF_*`` provenance on a FLAC, lower-cased key -> value.

    Re-tagging edits metadata; it does not re-process audio, so it must never
    touch provenance -- whatever the original encode stamped has to survive a
    re-tag untouched (:func:`_write_vorbis_tags` clears all comments before
    rewriting, so the re-tag path reads these first and carries them forward).
    Returns ``{}`` for a file with no RRF fields (e.g. a pre-app rip), so
    re-tagging such a file adds none and leaves it honestly un-stamped.
    """
    from mutagen.flac import FLAC

    flac = FLAC(str(flac_path))
    preserved = {}
    for key in flac.keys():
        if key.lower().startswith("rrf_"):
            values = flac[key]
            preserved[key.lower()] = values[0] if values else ""
    return preserved


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


def _place_audio(source: Path, dest: Path) -> None:
    """Put ``source``'s bytes at ``dest``, without decoding them.

    Three cases, and the middle one is the reason this exists.

    * **Same file.** Nothing to move; the tag write that follows edits it in
      place, which is what re-tagging a folder in place should mean.
    * **Same folder, new name.** The file is *renamed*, not copied. Copying left
      the old generation sitting beside the new one -- 28 files in a folder that
      holds 14 -- and a re-tag is supposed to update a library, not fork it.
    * **Different folder.** Copied, leaving the source untouched, because the
      user asked for the result somewhere else and did not ask to lose the
      original.

    ``copy2``/``replace`` preserve the bytes exactly; no decoder is involved.
    """
    if source.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.parent.resolve() == dest.parent.resolve():
        # Atomic within a filesystem: the destination either is the old file or
        # is the new one, never a half-written third thing.
        source.replace(dest)
        return
    shutil.copy2(source, dest)


def _prepare_output_dir(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _run_batch(tracks, work, on_progress, max_workers, should_cancel) -> BatchResult:
    """Batch a per-track job into a :class:`BatchResult`.

    The batching itself lives in :func:`core.batch.run_batch`, shared with the
    MP3 export path; this wrapper only supplies the track-name accessor and the
    result type.
    """
    result = BatchResult()
    result.outcomes = run_batch(
        tracks, work, name_of=lambda o: o.track.track_name,
        on_progress=on_progress, max_workers=max_workers,
        should_cancel=should_cancel)
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
    restoration_stages=None,
    output_sample_rate=None,
) -> BatchResult:
    """Convert each track's source WAV to a tagged FLAC in ``output_dir``.

    Source files are left in place. ``on_progress`` fires once per completed
    track (``max_workers > 1`` encodes several at once; progress is "N of M").
    Set ``configure=False`` to skip ffmpeg setup when the caller has already
    configured pydub. If ``cover`` is given it is embedded as the front cover of
    every track; a failure to embed becomes a per-track warning and never fails
    the batch. ``should_cancel`` is polled before each track is submitted.

    ``restoration_stages`` carries provenance (see :func:`_rrf_tags`): the actual
    ``Stage`` objects applied to this audio (empty list -> ``RRF_RESTORATION`` is
    ``none``), or ``None`` (the default) to write no ``RRF_*`` fields at all.

    ``output_sample_rate`` ("source"/"44100"/"48000", or None to keep source) is
    the FLAC sample rate. When it differs from a track's source rate, the encode
    resamples (ffmpeg ``-ar``, high-quality swresample) and RRF_RESTORATION gains
    a ``resample(<src>-><dst>)`` token. Per-track, because Convert-tab sources may
    differ in rate.
    """
    if configure:
        from core.ffmpeg_locator import configure_pydub

        configure_pydub()
    from pydub import AudioSegment

    out_dir = _prepare_output_dir(output_dir)
    target = _target_rate(output_sample_rate)

    def work(track):
        dest = out_dir / track.filename()
        audio = AudioSegment.from_wav(str(track.track_wav_loc))
        src_rate = int(audio.frame_rate)
        params, resample = None, None
        if target is not None and target != src_rate:
            params = ["-ar", str(target)]       # ffmpeg SRC to the library rate
            resample = (src_rate, target)
        audio.export(str(dest), format="flac", tags=track.tags(), parameters=params)
        outcome = TrackOutcome(track=track, output_path=dest)
        rrf = _rrf_tags(restoration_stages, resample=resample)
        try:
            _write_vorbis_tags(dest, {**track.vorbis_tags(), **rrf})
        except Exception as exc:
            outcome.warnings.append(f"Could not write tags: {exc}")
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

    Re-tagging never touches ``RRF_*`` provenance: it does not re-process audio,
    so any ``RRF_VERSION``/``RRF_RESTORATION`` the original encode wrote is read
    from the source and carried forward unchanged. A source with no RRF fields
    stays un-stamped.
    """
    # Deliberately no pydub, and no ffmpeg: re-tagging does not decode audio,
    # so it does not need an audio library. ``configure`` is accepted for
    # signature compatibility with convert_wavs_to_flacs and is now a no-op.
    out_dir = _prepare_output_dir(output_dir)

    def work(track):
        source = Path(track.track_wav_loc)
        dest = out_dir / track.filename()
        # Read provenance from the source *before* anything is written (dest may
        # resolve to the same path). Best-effort: a read failure just means no
        # RRF is carried, never a failed re-tag.
        try:
            preserved_rrf = _read_rrf(source)
        except Exception:
            preserved_rrf = {}

        # The audio is *copied*, never decoded and re-encoded.
        #
        # This used to run the file through AudioSegment.from_file() and
        # .export(), which rebuilds the entire container: it re-compresses at
        # pydub's default level, discards the source's own layout, and puts an
        # audio codec in the path of an operation that has no business touching
        # audio. A field incident produced FLACs that Windows Media Player would
        # not open at all (0xC00D36C4) -- and even where the result was
        # readable, the file was wholly rewritten, so every byte was exposed to
        # whatever the encode path did.
        #
        # A re-tag changes metadata. Copying the bytes and rewriting the tag
        # blocks makes the audio bit-identical by construction rather than by
        # luck, and removes the encoder from the operation entirely.
        _place_audio(source, dest)
        outcome = TrackOutcome(track=track, output_path=dest)
        try:
            _write_vorbis_tags(dest, {**track.vorbis_tags(), **preserved_rrf})
        except Exception as exc:
            outcome.warnings.append(f"Could not write tags: {exc}")
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
