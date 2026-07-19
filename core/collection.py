"""The collection ledger: what you own, and what you have actually ripped.

A list. Deliberately and only a list. No playback, no knowledge of the physical
shelf, no Discogs sync -- those are other products, and the value here is the
one question a person with a stack of records actually asks: *have I done this
one yet?*

Two ways in. An album registers itself when a rip finishes, which is the moment
the answer changes and the moment nobody wants to do paperwork. And a record you
own but have not ripped can be added by hand, because the gap between the shelf
and the library is the whole point of keeping the list.

**Reconciled against the filesystem, never trusted over it.** A row's
destination is a claim: the folder was there when the album finished. Folders
get moved, renamed, archived to a NAS, deleted. So every read reconciles what
the row says against what is on disk, and a row whose folder has gone reads as
*missing* rather than as ripped. The database is a ledger; the FLACs are the
truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: Owned, not yet ripped.
WANTED = "wanted"
#: Ripped, and the folder was there when we looked.
RIPPED = "ripped"
#: Ripped once, but the folder is not there now. A ledger entry, not a verdict.
MISSING = "missing"


@dataclass(frozen=True)
class Entry:
    """One row, with its status already reconciled against the filesystem."""

    id: int
    artist: str
    title: str
    release_mbid: str
    status: str
    destination: str
    ripped_at: str
    added_at: str

    @property
    def is_ripped(self) -> bool:
        return self.status == RIPPED

    def display(self) -> str:
        who = self.artist or "Unknown artist"
        what = self.title or "Untitled"
        return f"{who} — {what}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_ripped(store, *, artist: str, title: str, destination: str,
                    release_mbid: str = "") -> int | None:
    """Record a finished album. Idempotent per release where an MBID is known.

    Re-ripping a record it already knows updates that row rather than adding a
    second one -- the ledger answers "do I have this", and two rows for one
    record makes that question harder rather than easier. Without an MBID it
    falls back to matching artist+title, which is the best available identity.
    """
    if store is None:
        return None
    try:
        existing = _find(store, release_mbid=release_mbid, artist=artist, title=title)
        with store.write() as connection:
            if existing is not None:
                connection.execute(
                    "UPDATE collection SET artist=?, title=?, release_mbid=?, "
                    "status=?, destination=?, ripped_at=? WHERE id=?",
                    (artist, title, release_mbid or "", RIPPED, destination,
                     _now(), existing))
                return existing
            cursor = connection.execute(
                "INSERT INTO collection(artist, title, release_mbid, status, "
                "destination, ripped_at, added_at) VALUES(?,?,?,?,?,?,?)",
                (artist, title, release_mbid or "", RIPPED, destination,
                 _now(), _now()))
            return int(cursor.lastrowid)
    except Exception as exc:
        log.info("Collection: could not register %s - %s (%s).", artist, title, exc)
        return None


def add_wanted(store, *, artist: str, title: str, release_mbid: str = "") -> int | None:
    """Add a record you own but have not ripped."""
    if store is None or not (artist or title):
        return None
    try:
        existing = _find(store, release_mbid=release_mbid, artist=artist, title=title)
        if existing is not None:
            return existing                 # already known; do not duplicate
        with store.write() as connection:
            cursor = connection.execute(
                "INSERT INTO collection(artist, title, release_mbid, status, "
                "destination, ripped_at, added_at) VALUES(?,?,?,?,?,?,?)",
                (artist, title, release_mbid or "", WANTED, "", None, _now()))
            return int(cursor.lastrowid)
    except Exception as exc:
        log.info("Collection: could not add %s - %s (%s).", artist, title, exc)
        return None


def remove(store, entry_id: int) -> None:
    if store is None:
        return
    try:
        with store.write() as connection:
            connection.execute("DELETE FROM collection WHERE id=?", (entry_id,))
    except Exception:
        pass


def _find(store, *, release_mbid: str = "", artist: str = "", title: str = ""):
    """An existing row id for this record, by MBID then by artist+title."""
    connection = store.read()
    if release_mbid:
        row = connection.execute(
            "SELECT id FROM collection WHERE release_mbid=?", (release_mbid,)).fetchone()
        if row is not None:
            return row["id"]
    row = connection.execute(
        "SELECT id FROM collection WHERE release_mbid='' AND artist=? AND title=?",
        (artist, title)).fetchone()
    return row["id"] if row is not None else None


def entries(store) -> list[Entry]:
    """Every row, with ``ripped`` demoted to ``missing`` where the folder is gone.

    The reconciliation is the point. A ledger that keeps insisting an album is
    ripped after its folder was moved is worse than no ledger, because it
    answers the one question it exists for incorrectly and confidently.
    """
    if store is None:
        return []
    try:
        rows = store.read().execute(
            "SELECT * FROM collection ORDER BY artist COLLATE NOCASE, "
            "title COLLATE NOCASE").fetchall()
    except Exception as exc:
        log.info("Collection: could not be read (%s).", exc)
        return []

    out: list[Entry] = []
    for row in rows:
        status = row["status"]
        destination = row["destination"] or ""
        if status == RIPPED and not _folder_present(destination):
            status = MISSING
        out.append(Entry(
            id=row["id"], artist=row["artist"] or "", title=row["title"] or "",
            release_mbid=row["release_mbid"] or "", status=status,
            destination=destination, ripped_at=row["ripped_at"] or "",
            added_at=row["added_at"] or ""))
    return out


def _folder_present(destination: str) -> bool:
    if not destination:
        return False
    try:
        return Path(destination).is_dir()
    except OSError:
        return False


def counts(store) -> dict:
    """``{"ripped": n, "wanted": n, "missing": n}`` after reconciliation."""
    tally = {RIPPED: 0, WANTED: 0, MISSING: 0}
    for entry in entries(store):
        tally[entry.status] = tally.get(entry.status, 0) + 1
    return tally
