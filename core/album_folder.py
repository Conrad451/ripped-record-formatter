"""Reading an album folder the app itself could have written.

**The output format is the contract.** A folder of ``[NN] - Title.flac`` with
continuous numbering is operable whether this app made it, an earlier version
made it, or a hand-rolled script made it in 2024 -- because the only thing the
operation needs is to know which file holds which album position, and the name
says so. There is deliberately no journal lookup, no database check and no
native-versus-legacy distinction: making provenance matter would strand exactly
the libraries this is for.

A folder that does not conform is not failed at; it is redirected. Re-tag exists
to put a folder into this shape, so "run Re-tag first" is a complete answer
rather than a dead end.

What *is* checked is the part the operation depends on:

* every audio file carries a leading ``[NN]`` position,
* those positions are unique,
* they run continuously from 1.

Gaps are refused rather than tolerated. A folder numbered 1,2,4 is either
missing a track or numbered by something other than album position, and both
mean "the number does not identify the file" -- which is the one assumption the
replacement is entitled to make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: ``[13] - Letter to My 13 Year Old Self ERROR.flac`` -> position 13.
#: The side-lettered form (``[A01]``) is deliberately *not* matched: it numbers
#: within a side, so it cannot identify an album position on its own.
_POSITION_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*-\s*(.+)$")

AUDIO_SUFFIXES = {".flac"}


@dataclass(frozen=True)
class AlbumTrack:
    """One file, and the album position its name claims."""

    position: int
    title: str
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class AlbumFolder:
    """A folder read as an album, or the reason it cannot be."""

    path: Path
    tracks: tuple[AlbumTrack, ...] = ()
    problem: str = ""

    @property
    def conforms(self) -> bool:
        return not self.problem

    @property
    def count(self) -> int:
        return len(self.tracks)

    def at(self, position: int) -> AlbumTrack | None:
        return next((t for t in self.tracks if t.position == position), None)

    def at_positions(self, positions) -> list[AlbumTrack]:
        """The tracks at ``positions``, in album order, skipping any absent."""
        wanted = set(positions)
        return [t for t in self.tracks if t.position in wanted]

    def titles(self) -> list[str]:
        return [t.title for t in self.tracks]


#: What to tell someone holding a folder this cannot operate on. Names the fix,
#: because the fix exists and is one tab away.
REDIRECT = ("This folder is not in the app's format, so a side cannot be "
            "swapped into it by track number. Re-tag it first — that renames "
            "everything to [01] - Title.flac and gives it proper tags — then "
            "come back.")


def read(folder) -> AlbumFolder:
    """Read ``folder`` as an album. Never raises; a problem is a field."""
    path = Path(folder)
    try:
        if not path.is_dir():
            return AlbumFolder(path, problem=f"{path} is not a folder.")
        entries = sorted(p for p in path.iterdir()
                         if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
    except OSError as exc:
        return AlbumFolder(path, problem=f"{path} could not be read ({exc}).")

    if not entries:
        return AlbumFolder(path, problem=f"There are no FLACs in {path}.")

    tracks: list[AlbumTrack] = []
    unnumbered: list[str] = []
    for entry in entries:
        match = _POSITION_RE.match(entry.stem)
        if match is None:
            unnumbered.append(entry.name)
            continue
        tracks.append(AlbumTrack(position=int(match.group(1)),
                                 title=match.group(2).strip(), path=entry))

    if unnumbered:
        shown = ", ".join(unnumbered[:3])
        more = f" (and {len(unnumbered) - 3} more)" if len(unnumbered) > 3 else ""
        return AlbumFolder(path, problem=(
            f"{len(unnumbered)} file(s) here have no [NN] track number: "
            f"{shown}{more}. {REDIRECT}"))

    positions = [t.position for t in tracks]
    duplicates = sorted({p for p in positions if positions.count(p) > 1})
    if duplicates:
        return AlbumFolder(path, problem=(
            f"Two or more files share track number(s) "
            f"{', '.join(str(d) for d in duplicates)}, so a number does not "
            f"identify one file. {REDIRECT}"))

    tracks.sort(key=lambda t: t.position)
    expected = list(range(1, len(tracks) + 1))
    if positions != expected and sorted(positions) != expected:
        missing = sorted(set(expected) - set(positions))
        return AlbumFolder(path, problem=(
            f"The track numbers here are not continuous from 1 — "
            f"{'missing ' + ', '.join(str(m) for m in missing) if missing else 'they jump'}. "
            f"A number has to mean album position for this to be safe. {REDIRECT}"))

    return AlbumFolder(path, tracks=tuple(tracks))


def describe_replacement(tracks) -> str:
    """The sentence the confirmation shows. Names files, not counts.

    "This will replace 2 files" is not consent -- the user has to see *which*
    two, because the whole risk of this operation is replacing the wrong side.
    """
    if not tracks:
        return "Nothing would be replaced."
    listed = "\n".join(f"    {t.name}" for t in tracks)
    return f"This will replace:\n{listed}"
