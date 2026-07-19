"""Export a FLAC to whatever a device needs, one path for every format.

The generalisation of the MP3 exporter. What differs between formats is the
ffmpeg arguments, the container, and how metadata is written -- and all three
live in :mod:`core.export_profiles` as data. What does *not* differ is
everything that matters for correctness: sources are never touched, the encoder
is verified once before a batch rather than discovered mid-way, a failure on one
track is that track's warning rather than the batch's death, and the result is
decoded before it is called a success.

That last one is structural, not conventional. :func:`export_audio` verifies
every file it writes through the shared invariant, keyed off the profile, with
no per-profile hook that could opt out -- so a new format cannot skip the check
by forgetting to call it. The corruption incident that produced that invariant
happened because a writing path was verified by reading its *tags* back, and
tags read fine off unusable audio.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable

from core import export_profiles, proc
from core.export_profiles import TAG_ID3, TAG_MP4, TAG_NONE, ExportProfile
from core.mp3_export import (
    ExportOutcome,
    ExportResult,
    _run_batch,
    read_flac_metadata,
    write_id3,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


class EncoderUnavailable(RuntimeError):
    """The resolved ffmpeg cannot produce this format."""


# --------------------------------------------------------------------------- #
# Encoder verification
# --------------------------------------------------------------------------- #
def available_encoders(ffmpeg: str | Path) -> set[str]:
    """Every encoder this ffmpeg exposes, by name.

    Asked of the binary rather than assumed from the build we happen to ship
    today: the bundled ffmpeg is a pinned third-party build, and "the essentials
    build has always had that in it" is exactly the assumption that breaks
    silently three bundles from now.
    """
    try:
        completed = proc.run([str(ffmpeg), "-hide_banner", "-encoders"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return set()
    names: set[str] = set()
    for line in ((completed.stdout or "") + (completed.stderr or "")).splitlines():
        parts = line.split()
        # Encoder lines start with a flags column, then the name.
        if len(parts) >= 2 and line[:1] in (" ", "\t") and parts[0].strip("-"):
            names.add(parts[1])
    return names


def assert_encoder(ffmpeg: str | Path, profile: ExportProfile) -> None:
    """Raise if ``profile`` cannot be produced, naming the missing encoder.

    The person who hits this is holding a build somebody else made and has no
    reason to know what "libopus" is, so the message says which format is
    affected, which component is missing, and what to do -- and never quietly
    substitutes a different encoder, because an export that silently changes
    format is worse than one that refuses.
    """
    if not profile.encoder:
        return
    if profile.encoder in available_encoders(ffmpeg):
        return
    raise EncoderUnavailable(
        f"This ffmpeg cannot export {profile.label}: the {profile.encoder} "
        f"encoder is missing from {ffmpeg}. Replace the binary with a full or "
        f"essentials build, or re-run `python scripts/fetch_ffmpeg.py` to "
        f"restore the pinned one (which has it).")


# --------------------------------------------------------------------------- #
# Tag strategies
# --------------------------------------------------------------------------- #
#: Vorbis comment -> MP4 atom, for the fields that map one to one.
MP4_ATOMS: dict[str, str] = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "albumartist": "aART",
    "album": "\xa9alb",
    "date": "\xa9day",
}

#: Vorbis comment -> MP4 freeform atom name. MP4 has no standard place for any
#: of these, so they go in ----:com.apple.iTunes:<name>. The MusicBrainz names
#: are Picard's, so a Picard or beets user reading the exported file finds them
#: where they expect.
MP4_FREEFORM: dict[str, str] = {
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_recordingid": "MusicBrainz Track Id",
    "musicbrainz_trackid": "MusicBrainz Release Track Id",
    "rrf_version": "RRF_VERSION",
    "rrf_restoration": "RRF_RESTORATION",
}


def _text(tags: dict, key: str) -> str:
    """One string for ``key``, whatever shape the reader handed back.

    Mutagen's mappings give lists; some callers pass plain strings. Normalising
    here keeps every strategy from having to know which it got.
    """
    value = tags.get(key)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def _int_pair(tags: dict, number_key: str, total_key: str):
    """``[(n, total)]`` for trkn/disk, or None when there is no number."""
    number = _text(tags, number_key)
    if not number:
        return None
    try:
        n = int(number.split("/")[0])
    except (TypeError, ValueError):
        return None
    total = _text(tags, total_key)
    try:
        t = int(total.split("/")[0]) if total else 0
    except (TypeError, ValueError):
        t = 0
    return [(n, t)]


def write_mp4(path: str | Path, tags: dict, picture=None) -> None:
    """Write MP4 atoms onto an M4A. Mirrors :func:`write_id3` for ID3.

    Track and disc numbers are packed as ``trkn``/``disk`` pairs, which is how
    MP4 stores them -- the same shape ID3 uses for ``TRCK``, expressed
    differently. A field the source does not have produces no atom at all,
    never an empty one.
    """
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(str(path))
    audio.delete()

    for source_key, atom in MP4_ATOMS.items():
        value = _text(tags, source_key)
        if value:
            audio[atom] = [value]

    track = _int_pair(tags, "tracknumber", "tracktotal")
    if track:
        audio["trkn"] = track
    disc = _int_pair(tags, "discnumber", "disctotal")
    if disc:
        audio["disk"] = disc

    for source_key, name in MP4_FREEFORM.items():
        value = _text(tags, source_key)
        if value:
            audio[f"----:com.apple.iTunes:{name}"] = [
                MP4FreeForm(value.encode("utf-8"))]

    if picture is not None and getattr(picture, "data", None):
        mime = (getattr(picture, "mime", "") or "").lower()
        fmt = (MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(picture.data, imageformat=fmt)]

    audio.save()


def _apply_tags(profile: ExportProfile, dest: Path, source: Path) -> list[str]:
    """Write ``source``'s metadata onto ``dest``. Returns warnings, never raises.

    Metadata is best-effort on top of a good encode: a tag failure leaves a
    playable file and a warning, never a lost track.
    """
    if profile.tag_strategy == TAG_NONE:
        return []
    try:
        tags, picture = read_flac_metadata(source)
    except Exception as exc:
        return [f"Could not read tags from {source.name}: {exc}"]
    try:
        if profile.tag_strategy == TAG_ID3:
            write_id3(dest, tags, picture)
        elif profile.tag_strategy == TAG_MP4:
            write_mp4(dest, tags, picture)
    except Exception as exc:
        return [f"Could not write tags to {dest.name}: {exc}"]
    return []


# --------------------------------------------------------------------------- #
# The export
# --------------------------------------------------------------------------- #
def build_args(ffmpeg, source: Path, dest: Path, profile: ExportProfile,
               variant: str = "") -> list[str]:
    """The exact ffmpeg argv for one file. Pure, so it is testable without one."""
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source),
        *profile.encode_args(variant),
        str(dest),
    ]


def verify_output(path: Path) -> str:
    """Decode what was written. Returns a warning, or "" when it is real audio.

    The enforcement point for the v3.0.1 invariant, applied here rather than
    per profile so a new format inherits it by existing. A file that does not
    decode is reported as a failure even though ffmpeg exited 0 -- which is
    precisely the case that shipped corruption once.
    """
    try:
        import soundfile as sf
    except Exception:
        return ""
    if path.suffix.lower() in {".m4a", ".mp3", ".aac", ".opus"}:
        # libsndfile does not read these containers; fall back to a decode pass.
        from core.ffmpeg_locator import find_ffmpeg

        ffmpeg, _ = find_ffmpeg()
        if ffmpeg is None:
            return ""
        completed = proc.run(
            [str(ffmpeg), "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            return (f"{path.name} was written but does not decode: "
                    f"{detail[-1] if detail else 'unknown error'}")
        return ""
    try:
        with sf.SoundFile(str(path)) as handle:
            if len(handle) <= 0:
                return f"{path.name} was written but contains no audio."
    except Exception as exc:
        return f"{path.name} was written but does not decode: {exc}"
    return ""


def export_audio(
    flac_paths: Iterable[str | Path],
    output_dir: str | Path,
    *,
    profile: str | ExportProfile = export_profiles.DEFAULT_PROFILE,
    variant: str = "",
    on_progress: ProgressCallback | None = None,
    configure: bool = True,
    max_workers: int = 1,
    should_cancel=None,
) -> ExportResult:
    """Export each FLAC to ``profile``'s format in ``output_dir``.

    Sources are never modified, moved or deleted -- an export is a copy for
    somewhere else, and the FLAC remains the library.

    The encoder is verified once, before anything is written, so a build that
    cannot produce this format fails immediately rather than after eleven
    tracks. Every output is decoded before it counts as a success.
    """
    from core.ffmpeg_locator import ensure_ffmpeg

    chosen = (profile if isinstance(profile, ExportProfile)
              else export_profiles.get(profile))
    chosen.encode_args(variant)          # validates the variant up front

    ffmpeg, _ = ensure_ffmpeg(auto_download=configure)
    assert_encoder(ffmpeg, chosen)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def work(flac_path):
        source = Path(flac_path)
        dest = out_dir / chosen.output_name(source)
        outcome = ExportOutcome(source=source, output_path=dest)

        completed = proc.run(build_args(ffmpeg, source, dest, chosen, variant),
                             capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            outcome.warnings.append(
                f"Could not encode {source.name}: "
                f"{detail[-1] if detail else f'ffmpeg exited {completed.returncode}'}")
            return outcome

        problem = verify_output(dest)
        if problem:
            outcome.warnings.append(problem)
            return outcome

        outcome.warnings.extend(_apply_tags(chosen, dest, source))
        return outcome

    return _run_batch(flac_paths, work, on_progress, max_workers, should_cancel)
