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

from dataclasses import dataclass
from pathlib import Path


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

    def __post_init__(self) -> None:
        self.track_num = int(self.track_num)
        self.track_wav_loc = Path(self.track_wav_loc)

    def filename(self) -> str:
        """Output filename: ``[NN] - name.flac`` with 2-digit zero padding."""
        return f"[{self.track_num:02d}] - {self.track_name}.flac"

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

    def __str__(self) -> str:
        return self.filename()
