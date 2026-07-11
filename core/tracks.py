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
    mb_track_id: str = ""                   # MUSICBRAINZ_TRACKID (recording MBID)

    def __post_init__(self) -> None:
        self.track_num = int(self.track_num)
        self.track_wav_loc = Path(self.track_wav_loc)

    def filename(self) -> str:
        """Output filename: ``[NN] - name.flac`` with 2-digit zero padding.

        The title is sanitized for the filesystem (so a pasted title like
        ``"Intro / Outro"`` cannot produce an invalid path); the *tags* keep the
        original, unsanitized title. Falls back to ``Track NN`` if sanitizing
        leaves nothing.
        """
        name = sanitize_filename_component(self.track_name)
        if not name:
            name = f"Track {self.track_num:02d}"
        return f"[{self.track_num:02d}] - {name}.flac"

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
            "musicbrainz_trackid": self.mb_track_id,
        }
        return {k: str(v) for k, v in candidates.items()
                if v is not None and str(v).strip() != ""}

    def __str__(self) -> str:
        return self.filename()
