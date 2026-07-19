"""FLAC->MP3 export for portable devices -- one-directional, tags and art intact.

The library is FLAC. This module exists because an iPod is not, and never will
be, a FLAC player. It reads finished FLACs and writes MP3s beside them in a
separate tree; nothing here ever writes a FLAC, reads an MP3 as a source, or
touches the originals. That asymmetry is deliberate and load-bearing -- MP3 is a
*delivery* format for a device, not a second library format, so there is no
import path, no MP3-first workflow, and no round trip back.

What carries over
-----------------
Everything the FLAC knows that ID3 can express:

* Vorbis comments -> their ID3v2 equivalents (see :data:`TEXT_FRAMES` and the
  README mapping table). ``TRACKNUMBER``/``TRACKTOTAL`` collapse into one
  ``TRCK`` of the form ``N/T``, and disc numbers likewise into ``TPOS``.
* The MusicBrainz IDs and the ``RRF_*`` provenance fields, as ``TXXX`` frames --
  ID3 has no native home for either, and ``TXXX`` is exactly the "user-defined
  text" escape hatch they are for. A device ignores them; a tagger reading the
  MP3 later still knows which release it came from and how it was made.
* The embedded FLAC ``Picture`` -> an ``APIC`` front cover, so album art shows up
  on the device.

The absent-writes-nothing rule from the FLAC tagger holds here unchanged: a field
the source FLAC does not have produces no frame at all, never an empty one.

The mapping
-----------
====================================  ==================================  ==========
FLAC (Vorbis comment)                 MP3 (ID3v2.4 frame)                 Notes
====================================  ==================================  ==========
``TITLE``                             ``TIT2``
``ARTIST``                            ``TPE1``
``ALBUM``                             ``TALB``
``ALBUMARTIST``                       ``TPE2``
``DATE``                              ``TDRC``                            year as-is
``TRACKNUMBER`` + ``TRACKTOTAL``      ``TRCK``                            ``N/T``
``DISCNUMBER`` + ``DISCTOTAL``        ``TPOS``                            ``N/T``
``MUSICBRAINZ_ALBUMID``               ``TXXX:MusicBrainz Album Id``
``MUSICBRAINZ_ARTISTID``              ``TXXX:MusicBrainz Artist Id``
``MUSICBRAINZ_RECORDINGID``           ``TXXX:MusicBrainz Recording Id``
``MUSICBRAINZ_TRACKID``               ``TXXX:MusicBrainz Release Track Id``
``RRF_VERSION``                       ``TXXX:RRF_VERSION``
``RRF_RESTORATION``                   ``TXXX:RRF_RESTORATION``
embedded ``Picture`` (front cover)    ``APIC`` type 3, desc "front cover"
====================================  ==================================  ==========

``TRACKTOTAL``/``DISCTOTAL`` are also read as ``TOTALTRACKS``/``TOTALDISCS``,
which is how some other taggers spell them -- the source folder is whatever the
user points at, not necessarily something this app wrote. A total with no
corresponding number is dropped rather than written as ``/5``.

How it is done
--------------
Encoding is a direct ffmpeg subprocess per track (libmp3lame), not pydub. pydub
would decode the FLAC to a raw in-memory buffer and pipe it back out, which for
an album's worth of 24-bit sides is a lot of RAM to spend on something ffmpeg
does natively in one pass. Going direct also means the quality setting is an
explicit, inspectable argv (:func:`encode_args`) rather than something assembled
inside a library.

Tags are then written in a second pass with mutagen -- the same shape as
:mod:`core.converter`'s FLAC path, and for the same reason: the encoder's own tag
writing is a lowest-common-denominator affair, so we let ffmpeg move the audio
and let mutagen own the metadata.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from core import proc
from core.batch import run_batch

# on_progress(current, total, name) -- current is 1-based and counts tracks that
# have *completed*, matching core.converter's callback exactly so the GUI can
# route an export through the same progress plumbing as a conversion.
ProgressCallback = Callable[[int, int, str], None]


class Mp3EncoderUnavailable(RuntimeError):
    """Raised when the resolved ffmpeg cannot encode MP3 (no libmp3lame)."""


# --- quality -----------------------------------------------------------------
# The three settings the app offers, and the ffmpeg arguments each becomes.
# V0 is the default: transparent for practical purposes and meaningfully smaller
# than 320 CBR. 320 CBR exists for devices and car decks that get unhappy with
# VBR headers; V2 exists for when the device is small and the commute is long.
QUALITY_V0 = "V0"
QUALITY_320 = "320"
QUALITY_V2 = "V2"

DEFAULT_QUALITY = QUALITY_V0

QUALITY_ARGS: dict[str, list[str]] = {
    QUALITY_V0: ["-q:a", "0"],          # VBR ~245 kbps
    QUALITY_320: ["-b:a", "320k"],      # CBR 320 kbps
    QUALITY_V2: ["-q:a", "2"],          # VBR ~190 kbps
}

QUALITY_LABELS: dict[str, str] = {
    QUALITY_V0: "V0 (VBR ~245 kbps, default)",
    QUALITY_320: "320 kbps CBR",
    QUALITY_V2: "V2 (VBR ~190 kbps)",
}


# --- tag mapping -------------------------------------------------------------
# Vorbis comment -> ID3v2 text frame, for the fields that map one to one.
# TRACKNUMBER/TRACKTOTAL and DISCNUMBER/DISCTOTAL are deliberately absent: ID3
# packs each pair into a single "N/T" frame, so they are handled in
# :func:`_id3_frames` rather than here.
TEXT_FRAMES: dict[str, str] = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "date": "TDRC",
}

# Vorbis comment -> TXXX description. ID3 has no standard frame for any of
# these. The MusicBrainz descriptions are Picard's, so a Picard/beets user
# reading the exported MP3 sees the IDs where they expect them; the RRF ones keep
# their Vorbis spelling, since they are ours and nothing else looks for them.
TXXX_FRAMES: dict[str, str] = {
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_recordingid": "MusicBrainz Recording Id",
    "musicbrainz_trackid": "MusicBrainz Release Track Id",
    "rrf_version": "RRF_VERSION",
    "rrf_restoration": "RRF_RESTORATION",
}

# Vorbis spells totals two ways in the wild. Files this app wrote use the first
# of each pair; files from other taggers may use the second. We read both because
# the source folder is whatever the user points us at, not necessarily ours.
_TRACK_TOTAL_KEYS = ("tracktotal", "totaltracks")
_DISC_TOTAL_KEYS = ("disctotal", "totaldiscs")


@dataclass
class ExportOutcome:
    """What happened to a single FLAC during an export."""

    source: Path
    output_path: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Display name for progress/log lines -- the source's bare filename."""
        return self.source.name


@dataclass
class ExportResult:
    """Aggregate outcome of an export batch.

    Deliberately the same surface as :class:`core.converter.BatchResult`
    (``total``/``warnings``/``summary()``), so the window's existing
    finished-handler renders an export exactly like a conversion.
    """

    outcomes: list[ExportOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def warnings(self) -> list[str]:
        return [w for o in self.outcomes for w in o.warnings]

    def summary(self) -> str:
        msg = f"Export Complete - {self.total} track(s) exported to MP3"
        if self.warnings:
            msg += f", {len(self.warnings)} warning(s)"
        return msg


def encode_args(ffmpeg: str | Path, source: Path, dest: Path, quality: str) -> list[str]:
    """The exact ffmpeg argv for one FLAC->MP3 encode.

    A separate, pure function so the quality setting is testable without running
    an encoder: given a quality, you can assert precisely which flags ffmpeg is
    handed. ``-vn`` drops any embedded picture from the *audio* pass -- cover art
    is re-attached properly as an ``APIC`` frame by the mutagen pass, and letting
    ffmpeg copy the FLAC picture through produces a video stream that some
    players choke on.

    Raises :class:`ValueError` for an unknown quality rather than silently
    falling back, so a typo in a caller cannot quietly ship 128 kbps.
    """
    if quality not in QUALITY_ARGS:
        raise ValueError(
            f"Unknown MP3 quality {quality!r}. "
            f"Expected one of: {', '.join(QUALITY_ARGS)}."
        )
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(source),
        "-vn",
        "-codec:a", "libmp3lame",
        *QUALITY_ARGS[quality],
        str(dest),
    ]


def has_libmp3lame(ffmpeg: str | Path) -> bool:
    """Whether this ffmpeg build exposes the libmp3lame encoder.

    Asked by running ``ffmpeg -encoders`` rather than trusting the build we
    happen to ship today: the bundled binary is a pinned third-party build
    (see ``scripts/fetch_ffmpeg.py``), and "the essentials build has always had
    lame in it" is exactly the kind of assumption that breaks silently three
    bundles from now. A build without it is a clear error, not a mystery.
    """
    try:
        completed = proc.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return "libmp3lame" in (completed.stdout or "") + (completed.stderr or "")


def assert_libmp3lame(ffmpeg: str | Path) -> None:
    """Raise :class:`Mp3EncoderUnavailable` if MP3 encoding is not possible.

    The message names the binary and says what to do about it, because the person
    hitting this is holding a build someone else made and has no reason to know
    what "libmp3lame" is.
    """
    if not has_libmp3lame(ffmpeg):
        raise Mp3EncoderUnavailable(
            f"This ffmpeg cannot encode MP3: the libmp3lame encoder is missing "
            f"from {ffmpeg}. MP3 export needs an ffmpeg built with "
            f"--enable-libmp3lame (the bundled build is). Replace the binary "
            f"with a full/essentials build, or re-run "
            f"`python scripts/fetch_ffmpeg.py` to restore the pinned one."
        )


def _first(tags, keys) -> str:
    """First non-empty value among ``keys`` in a mutagen FLAC tag mapping."""
    for key in keys:
        values = tags.get(key)
        if values:
            value = str(values[0]).strip()
            if value:
                return value
    return ""


def _paired(number: str, total: str) -> str:
    """``"N/T"`` when a total is known, plain ``"N"`` when it is not, ``""`` when
    there is no number at all.

    ID3's TRCK/TPOS carry the total in the same frame after a slash. A total with
    no number is meaningless and is dropped -- absent writes nothing.
    """
    if not number:
        return ""
    return f"{number}/{total}" if total else number


def read_flac_metadata(flac_path: str | Path) -> tuple[dict[str, str], object | None]:
    """Read one FLAC's Vorbis comments (lower-cased) and its front-cover picture.

    Returns ``(tags, picture)`` where ``picture`` is the mutagen ``Picture`` to
    use as the front cover, or ``None``. A FLAC may carry several pictures; we
    prefer the one typed as front cover (type 3) and otherwise fall back to the
    first, which is what a file with a single untyped picture actually means.
    """
    from mutagen.flac import FLAC

    flac = FLAC(str(flac_path))
    # mutagen's Vorbis comment block iterates as (key, value) pairs and a key may
    # legitimately repeat, so fold rather than dict-comprehend (which would keep
    # only the last of a repeated field).
    tags: dict[str, list[str]] = {}
    for key, value in (flac.tags or []):
        tags.setdefault(key.lower(), []).append(str(value))

    picture = None
    for candidate in flac.pictures:
        if candidate.type == 3:          # front cover
            picture = candidate
            break
    if picture is None and flac.pictures:
        picture = flac.pictures[0]
    return tags, picture


def _id3_frames(tags: dict[str, list[str]]):
    """Build the ID3 frames for one track from its Vorbis comments.

    Yields mutagen frame objects. Every field is conditional: a Vorbis comment
    that is absent, empty, or whitespace produces no frame, so an MP3 exported
    from a sparsely-tagged FLAC is honestly sparse rather than full of empty
    strings. See the README mapping table for the full correspondence.
    """
    from mutagen import id3

    for vorbis_key, frame_id in TEXT_FRAMES.items():
        value = _first(tags, (vorbis_key,))
        if value:
            yield getattr(id3, frame_id)(encoding=3, text=[value])

    track = _paired(_first(tags, ("tracknumber",)), _first(tags, _TRACK_TOTAL_KEYS))
    if track:
        yield id3.TRCK(encoding=3, text=[track])

    disc = _paired(_first(tags, ("discnumber",)), _first(tags, _DISC_TOTAL_KEYS))
    if disc:
        yield id3.TPOS(encoding=3, text=[disc])

    for vorbis_key, description in TXXX_FRAMES.items():
        value = _first(tags, (vorbis_key,))
        if value:
            yield id3.TXXX(encoding=3, desc=description, text=[value])


def write_id3(mp3_path: str | Path, tags: dict[str, list[str]], picture=None) -> None:
    """Write the mapped ID3 tag onto a freshly encoded MP3 (replacing any).

    Any tag ffmpeg wrote is deleted first, so the result is exactly the mapping
    and nothing else -- the same "authoritative, no empties" stance
    :func:`core.converter._write_vorbis_tags` takes for FLAC.

    ID3v2.4 is written, not v2.3: the mapping uses ``TDRC`` for the date, which
    is a v2.4 frame with no honest v2.3 spelling (v2.3 would split it across
    ``TYER``/``TDAT``). Everything shipped in the last two decades reads v2.4.
    """
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError

    try:
        tag = ID3(str(mp3_path))
        tag.delete()
    except ID3NoHeaderError:
        tag = ID3()

    for frame in _id3_frames(tags):
        tag.add(frame)

    if picture is not None:
        tag.add(APIC(
            encoding=3,
            mime=picture.mime,
            type=3,                     # front cover
            desc="front cover",
            data=picture.data,
        ))

    tag.save(str(mp3_path), v2_version=4)


def mp3_name(flac_path: Path) -> str:
    """Output filename: the source's name with a ``.mp3`` extension.

    The FLAC's filename already encodes the numbering the user chose (``[01] -
    Title.flac``), and an export is a copy of the library for a device -- not a
    chance to renumber it. Keeping the stem identical also means the MP3 folder
    sorts the same as the FLAC folder it mirrors.
    """
    return Path(flac_path).with_suffix(".mp3").name


def _run_batch(items, work, on_progress, max_workers, should_cancel) -> ExportResult:
    """Batch a per-file export into an :class:`ExportResult`.

    The batching itself lives in :func:`core.batch.run_batch`, shared with the
    WAV->FLAC path; this wrapper only supplies the name accessor and the result
    type.
    """
    result = ExportResult()
    result.outcomes = run_batch(
        items, work, name_of=lambda o: o.name,
        on_progress=on_progress, max_workers=max_workers,
        should_cancel=should_cancel)
    return result


def export_mp3(
    flac_paths: Iterable[str | Path],
    output_dir: str | Path,
    *,
    quality: str = DEFAULT_QUALITY,
    on_progress: ProgressCallback | None = None,
    configure: bool = True,
    max_workers: int = 1,
    should_cancel=None,
) -> ExportResult:
    """Export each FLAC in ``flac_paths`` to a tagged MP3 in ``output_dir``.

    Sources are never modified, moved, or deleted -- an export is a copy for a
    device, and the FLAC remains the library. Each output is
    ``{output_dir}/{source stem}.mp3``.

    ``quality`` is one of ``"V0"`` (default), ``"320"``, or ``"V2"``; see
    :data:`QUALITY_ARGS`. ``on_progress`` fires once per completed track as
    ``(completed, total, filename)``. ``max_workers`` > 1 encodes several tracks
    at once on a bounded pool, each an independent ffmpeg run. ``should_cancel``
    is polled before each track is submitted; in-flight encodes finish and a
    partial :class:`ExportResult` is returned. Set ``configure=False`` when the
    caller has already resolved ffmpeg.

    Raises :class:`Mp3EncoderUnavailable` up front -- before encoding anything --
    if the resolved ffmpeg has no libmp3lame, and
    :class:`core.ffmpeg_locator.FFmpegNotAvailable` if there is no ffmpeg at all.
    A failure on an individual track is recorded as that track's warning and does
    not abandon the rest of the batch, matching the converter's behaviour.
    """
    from core.ffmpeg_locator import ensure_ffmpeg

    ffmpeg, _ = ensure_ffmpeg(auto_download=configure)
    # Checked once for the batch, not once per track: it is a subprocess launch,
    # and the answer cannot change midway through an export.
    assert_libmp3lame(ffmpeg)

    if quality not in QUALITY_ARGS:
        raise ValueError(
            f"Unknown MP3 quality {quality!r}. "
            f"Expected one of: {', '.join(QUALITY_ARGS)}."
        )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def work(flac_path):
        source = Path(flac_path)
        dest = out_dir / mp3_name(source)
        outcome = ExportOutcome(source=source, output_path=dest)

        completed = proc.run(
            encode_args(ffmpeg, source, dest, quality),
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            outcome.warnings.append(
                f"Could not encode {source.name}: "
                f"{detail[-1] if detail else f'ffmpeg exited {completed.returncode}'}"
            )
            return outcome

        # Metadata is best-effort on top of a good encode: a tag failure leaves
        # you with a playable MP3 and a warning, never a lost track.
        try:
            tags, picture = read_flac_metadata(source)
        except Exception as exc:
            outcome.warnings.append(f"Could not read tags from {source.name}: {exc}")
            return outcome
        try:
            write_id3(dest, tags, picture)
        except Exception as exc:
            outcome.warnings.append(f"Could not write tags to {dest.name}: {exc}")
        return outcome

    return _run_batch(flac_paths, work, on_progress, max_workers, should_cancel)
