"""Swapping a freshly-encoded side into an album folder, safely.

The operation the stakeholder needs: an 18-track deluxe pressing where two
tracks on one side came out garbled, and the alternative to this feature is
re-recording all eight sides.

**The ordering is the whole design.** Encode first, verify the new files decode,
and only then touch the old ones. At no point does a failure leave the album
worse than it started: if the encode fails there is nothing to clean up, and if
the new files do not decode the old ones are still there. The condemned files
are removed last, deliberately, as the final act of a run that has already
succeeded.

**Deleting is the default, and that is not a contradiction of the raw-WAV
doctrine.** That doctrine protects *masters* -- the recordings everything else
is derived from. These are derived outputs that have already been judged wrong;
keeping them by default would mean every replacement leaves litter in the album
folder for someone to clean up later, having to work out which of two files with
similar names is the good one. Archiving to ``.replaced/`` is one checkbox away
for anyone who wants the belt.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Where archived originals go, inside the album folder. Dot-prefixed so it
#: sorts out of the way and reads as machinery rather than as music.
ARCHIVE_DIRNAME = ".replaced"


@dataclass
class SwapPlan:
    """What a replacement is about to do, before it does any of it."""

    album: Path
    positions: tuple[int, ...]
    condemned: tuple[Path, ...] = ()
    archive: bool = False

    @property
    def describes_nothing(self) -> bool:
        return not self.condemned

    def summary(self) -> str:
        from core.album_folder import describe_replacement

        class _Named:
            def __init__(self, path):
                self.name = path.name

        return describe_replacement([_Named(p) for p in self.condemned])


@dataclass
class SwapResult:
    """What actually happened."""

    replaced: list[Path] = field(default_factory=list)
    archived: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True


def plan(album_folder, positions, *, archive: bool = False) -> SwapPlan:
    """Work out which existing files a replacement would condemn.

    Identified by **album position**, never by name collision: the new files may
    be named differently from the old ones (a corrected title, a stripped ERROR
    suffix), and matching on name would either miss them or, worse, match the
    wrong ones.
    """
    from core import album_folder as reader

    folder = (album_folder if hasattr(album_folder, "tracks")
              else reader.read(album_folder))
    condemned = tuple(t.path for t in folder.at_positions(positions))
    return SwapPlan(album=folder.path, positions=tuple(sorted(positions)),
                    condemned=condemned, archive=archive)


def retire(plan: SwapPlan, *, keep: set[Path] | None = None) -> SwapResult:
    """Remove or archive the condemned files. Call only after a good encode.

    ``keep`` names paths the new encode happens to have written to -- if a
    replacement track landed on exactly the same filename as the one it
    replaces, that file is now the *new* one and must not be deleted. This is
    the one case where name matters, and it matters in the direction of safety.
    """
    result = SwapResult()
    keep = keep or set()
    archive_dir = plan.album / ARCHIVE_DIRNAME

    for path in plan.condemned:
        try:
            if path.resolve() in {p.resolve() for p in keep if p.exists()}:
                # The new encode wrote over it; nothing to retire.
                continue
            if not path.exists():
                continue
            if plan.archive:
                archive_dir.mkdir(parents=True, exist_ok=True)
                destination = archive_dir / path.name
                if destination.exists():
                    destination.unlink()
                shutil.move(str(path), str(destination))
                result.archived.append(destination)
            else:
                path.unlink()
                result.removed.append(path)
        except OSError as exc:
            # A file that will not budge is a warning, not a disaster: the new
            # side is already written and correct.
            result.warnings.append(
                f"Could not {'archive' if plan.archive else 'remove'} "
                f"{path.name}: {exc}")
            result.ok = False
    return result


def verify_encoded(paths) -> list[str]:
    """Decode every newly written file. Returns problems, empty when all good.

    The v3.0.1 invariant, applied at the one moment it decides whether anything
    gets deleted. A side that encoded but does not decode must never be the
    reason the old files go away.
    """
    from core.audio_export import verify_output

    problems: list[str] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            problems.append(f"{path.name} was not written.")
            continue
        problem = verify_output(path)
        if problem:
            problems.append(problem)
    return problems
