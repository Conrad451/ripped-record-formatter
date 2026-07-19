"""The album job journal: what was being worked on when the lights went out.

Turntable time is unrepeatable. A crash or a restart part-way through a record
used to cost the whole session -- the mapping, the release, which sides were
done, which one was mid-review -- and the only way back was to set it all up
again from memory.

So the job writes down what it is doing as it does it. One row per album
attempt, rewritten at every state transition: cheap single-row updates, on the
GUI thread, never in the audio path.

**What a journal is not.** It is not a snapshot of work in progress that can be
restored intact. Staging is a ``mkdtemp`` and never survives a restart, so the
restored intermediates simply are not there. Resume therefore means *re-prepare
from the WAVs*, which is honest and says so; it never pretends to hand back
analysis it does not have. The WAVs on disk are the real state -- this is the
note that says which ones, in what order, and as what.

Per-side it also records the restoration stages **as applied**, with their
parameters. That is what lets a re-do offer the settings a side was actually
made with, rather than whatever Settings happens to say weeks later.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: A job that was live when we last wrote. What resume offers.
RUNNING = "running"
#: Concluded normally. Never offered again.
DONE = "done"
#: The user said no. Never offered again.
DISCARDED = "discarded"

_OPEN_STATES = (RUNNING,)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def describe_stages(stages) -> list[dict]:
    """Restoration stages as applied, with their parameters.

    Records the class as well as the display name, because the display name is
    for people and the class is what a later run would have to rebuild.
    Anything that will not serialise is dropped rather than failing the write --
    a journal that refuses to record is worse than one recording a little less.
    """
    out: list[dict] = []
    for stage in stages or ():
        entry = {"stage": type(stage).__name__,
                 "name": getattr(stage, "name", type(stage).__name__)}
        try:
            if dataclasses.is_dataclass(stage):
                entry["params"] = json.loads(json.dumps(dataclasses.asdict(stage)))
        except (TypeError, ValueError):
            pass
        out.append(entry)
    return out


def begin(store, *, artist: str = "", album: str = "", release_mbid: str = "",
          destination: str = "", wavs=(), mapping=(), sides=()) -> int | None:
    """Open a journal row for a starting job. Returns its id, or None.

    Never raises into the caller: a job that cannot be journalled is still a
    job, and losing resilience must not cost someone their rip.
    """
    if store is None:
        return None
    try:
        with store.write() as connection:
            cursor = connection.execute(
                "INSERT INTO sessions(state, release_mbid, artist, album, "
                "destination, wavs, mapping, sides, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (RUNNING, release_mbid or "", artist or "", album or "",
                 destination or "", json.dumps([str(w) for w in wavs]),
                 json.dumps(list(mapping)), json.dumps(list(sides)),
                 _now(), _now()))
            return int(cursor.lastrowid)
    except Exception as exc:
        log.info("Session journal: could not open a row (%s).", exc)
        return None


def update(store, session_id, **fields) -> None:
    """Rewrite part of a journal row. Unknown fields are ignored."""
    if store is None or session_id is None:
        return
    columns = {"state", "release_mbid", "artist", "album", "destination"}
    encoded = {"wavs", "mapping", "sides"}
    sets, values = [], []
    for key, value in fields.items():
        if key in columns:
            sets.append(f"{key}=?")
            values.append(value if value is not None else "")
        elif key in encoded:
            sets.append(f"{key}=?")
            values.append(json.dumps(value if isinstance(value, list) else list(value)))
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([_now(), session_id])
    try:
        with store.write() as connection:
            connection.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", values)
    except Exception as exc:
        log.info("Session journal: could not update %s (%s).", session_id, exc)


def finish(store, session_id) -> None:
    """The job concluded. It stops being something to resume."""
    update(store, session_id, state=DONE)


def discard(store, session_id) -> None:
    """The user declined to resume. Closed out, never offered again."""
    update(store, session_id, state=DISCARDED)


def interrupted(store) -> dict | None:
    """The most recent job that was still open when we last wrote, if any.

    Returns a plain dict with the JSON columns decoded. A row that cannot be
    decoded is skipped rather than raising: an unreadable journal must not stop
    the app from starting.
    """
    if store is None:
        return None
    try:
        rows = store.read().execute(
            "SELECT * FROM sessions WHERE state IN "
            f"({','.join('?' * len(_OPEN_STATES))}) ORDER BY id DESC",
            _OPEN_STATES).fetchall()
    except Exception as exc:
        log.info("Session journal: could not be read (%s).", exc)
        return None

    for row in rows:
        try:
            return {
                "id": row["id"], "state": row["state"],
                "release_mbid": row["release_mbid"], "artist": row["artist"],
                "album": row["album"], "destination": row["destination"],
                "wavs": json.loads(row["wavs"]),
                "mapping": json.loads(row["mapping"]),
                "sides": json.loads(row["sides"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
        except Exception:
            continue
    return None


def close_all_open(store) -> int:
    """Mark every open row discarded. Returns how many.

    Called once a resume decision has been made, so a stack of rows from
    repeated crashes cannot pile up and re-offer forever.
    """
    if store is None:
        return 0
    try:
        with store.write() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET state=?, updated_at=? WHERE state=?",
                (DISCARDED, _now(), RUNNING))
            return int(cursor.rowcount or 0)
    except Exception:
        return 0


def unfinished_side(journal: dict) -> dict | None:
    """The side a resumed job should pick up on: the first not yet done.

    "Done" is the only state whose work survives a restart, because its files
    are on disk. Everything else -- queued, analysing, ready, mid-review -- has
    to be prepared again, so they are all equally unfinished from here.
    """
    for side in journal.get("sides", ()):
        if side.get("state") != "done":
            return side
    return None


def describe(journal: dict) -> str:
    """One line for the user, in their vocabulary rather than ours.

    Names the record and the consequence -- the side has to be prepared again
    before it can be reviewed -- rather than the operation we will perform to
    get there. The precise version ("staging gone, re-analysing ...") belongs
    in the log, where the audience is different.
    """
    album = journal.get("album") or "an album"
    side = unfinished_side(journal)
    if side is None:
        return f"You were working on {album}."
    label = side.get("label") or "a side"
    return (f"You were working on {album} — {label} needs to be prepared "
            "again before review.")
