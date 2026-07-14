"""The :class:`Tracks` value object: one audio track's metadata + source path.

Ported from the original ``v2/Tracks.py``. Two defects from that version are
fixed here:

* ``track_album`` / ``track_artist`` were declared *both* as instance attributes
  and as no-arg methods that returned themselves -- the method definitions were
  dead (the attributes shadowed them) and misleading. They are gone.
* Filename generation was hidden inside ``__str__`` with an ``if num < 10``
  branch. It now lives in an explicit :meth:`filename` method using ``:02d``
  zero-padding, which is equivalent for tracks 1-99 and well-defined beyond.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Characters Windows forbids in a filename component.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def track_filename(track_name: str, track_num: int, *, file_index: int | None = None,
                   side_letter: str = "", use_side_letters: bool = False) -> str:
    """``[NN] - name.flac``, with 2-digit zero padding.

    Three numbering styles, chosen by the arguments given -- all of them
    *filename-only*, none of them visible in the tags:

    * ``use_side_letters`` + a ``side_letter`` -> ``[A01]``, ``[B01]`` -- the
      per-side number, prefixed by its side. Unique across an album because the
      letter disambiguates.
    * ``file_index`` set -> ``[01]``..``[NN]`` continuing across sides. This is
      what album jobs use: every side lands in one flat folder, so the number has
      to be album-wide even though TRACKNUMBER stays per-side.
    * neither -> ``[NN]`` from ``track_num``. The single-side default.

    A free function rather than only a method, because an album job has to know
    what it is about to write *before* it has cut any segments -- that is how it
    can say "6 files already exist in the destination" instead of finding out by
    overwriting them.

    The title is sanitized for the filesystem (so a pasted title like
    ``"Intro / Outro"`` cannot produce an invalid path); the *tags* keep the
    original, unsanitized title. Falls back to ``Track NN`` if sanitizing leaves
    nothing.
    """
    name = sanitize_filename_component(track_name)
    if not name:
        name = f"Track {int(track_num):02d}"

    if use_side_letters and side_letter:
        number = f"{side_letter}{int(track_num):02d}"
    elif file_index is not None:
        number = f"{int(file_index):02d}"
    else:
        number = f"{int(track_num):02d}"
    return f"[{number}] - {name}.flac"


def sanitize_filename_component(name: str) -> str:
    """Make ``name`` safe as a Windows filename component.

    Replaces the forbidden characters ``\\ / : * ? " < > |`` with a space,
    collapses runs of whitespace, and strips leading/trailing spaces and dots
    (Windows also disallows trailing dots/spaces). May return ``""`` -- callers
    must decide on a fallback.
    """
    cleaned = _INVALID_FILENAME_CHARS.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.strip(" .")


@dataclass
class Tracks:
    """A single track's metadata and the on-disk location of its source audio.

    ``track_wav_loc`` names the *source* file (a WAV when converting, an existing
    FLAC when re-tagging); it is coerced to :class:`~pathlib.Path`.
    """

    track_num: int
    track_name: str
    track_album: str
    track_artist: str
    track_wav_loc: Path

    # Optional richer metadata (from a release lookup). Every field is optional:
    # absent data writes no tag at all -- never an empty string. Existing call
    # sites that construct only the five positional fields are unaffected.
    album_artist: str = ""
    date: str = ""                         # release year, e.g. "1993"
    track_total: int | None = None         # tracks on this side (see README)
    disc_number: int | None = None         # side/medium position
    disc_total: int | None = None
    mb_album_id: str = ""                   # MUSICBRAINZ_ALBUMID (release MBID)
    mb_artist_id: str = ""                  # MUSICBRAINZ_ARTISTID
    mb_recording_id: str = ""               # MUSICBRAINZ_RECORDINGID (recording MBID)
    mb_track_id: str = ""                   # MUSICBRAINZ_TRACKID (release-track MBID)

    # --- filename-only numbering (never touches tags) ---
    # Album jobs write every side into one flat folder, so the *filename* needs a
    # number unique across the whole album while TRACKNUMBER stays per-side. These
    # three fields drive :meth:`filename` and nothing else -- :meth:`vorbis_tags`
    # ignores them entirely.
    file_index: int | None = None           # album-wide 1-based number; None -> use track_num
    side_letter: str = ""                   # "A", "B", ... for the [A01] style
    use_side_letters: bool = False          # switch filename style to [A01]/[B01]

    def __post_init__(self) -> None:
        self.track_num = int(self.track_num)
        self.track_wav_loc = Path(self.track_wav_loc)

    def filename(self) -> str:
        """Output filename: ``[NN] - name.flac`` with 2-digit zero padding.

        See :func:`track_filename` for the numbering styles. This is a thin
        delegation on purpose: an album job needs to know what filenames it is
        *about* to write before it has cut a single segment (to warn about
        overwriting), and a second implementation of this would drift.
        """
        return track_filename(
            self.track_name, self.track_num,
            file_index=self.file_index,
            side_letter=self.side_letter,
            use_side_letters=self.use_side_letters,
        )

    def tags(self) -> dict[str, str]:
        """Metadata tags for the encoder.

        The track number comes from :attr:`track_num` -- the single source of
        truth -- rather than a separate running counter as in the old code.
        """
        return {
            "artist": self.track_artist,
            "album": self.track_album,
            "title": self.track_name,
            "track": str(self.track_num),
        }

    def vorbis_tags(self) -> dict[str, str]:
        """FLAC Vorbis comments to write, omitting every field we don't have.

        Empty/absent values are dropped entirely (no empty-string tags). This is
        the authoritative set the converter writes via mutagen; the base four
        (artist/album/title/tracknumber) are included when present, the richer
        release fields only when supplied.
        """
        candidates = {
            "artist": self.track_artist,
            "album": self.track_album,
            "title": self.track_name,
            "tracknumber": str(self.track_num),
            "albumartist": self.album_artist,
            "date": self.date,
            "tracktotal": self.track_total,
            "discnumber": self.disc_number,
            "disctotal": self.disc_total,
            "musicbrainz_albumid": self.mb_album_id,
            "musicbrainz_artistid": self.mb_artist_id,
            "musicbrainz_recordingid": self.mb_recording_id,
            "musicbrainz_trackid": self.mb_track_id,
        }
        return {k: str(v) for k, v in candidates.items()
                if v is not None and str(v).strip() != ""}

    def __str__(self) -> str:
        return self.filename()
