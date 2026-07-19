"""One file of durable state: ``rrf.db``, beside the settings it replaces.

**Doctrine, and it is load-bearing: this database is recoverable state and a
ledger. It is never the source of truth for the library.** The FLACs and the
tags inside them are that. Anything here that describes the world -- a folder
path, a finished album, a side's output -- is a *claim about the filesystem*,
and when the two disagree the filesystem wins and the row is corrected or
marked missing. Nothing may require this file to exist in order to work: delete
it and the app comes up with defaults, an empty ledger, and every FLAC on disk
still perfectly intact.

That is why it is one file. As a desktop engineer you would rather move one
file than a folder: back it up, copy it to the new machine, done. It sits in the
platform config directory next to where ``settings.json`` used to live.

Stdlib ``sqlite3``, no ORM, no new dependencies. WAL mode, because the GUI
thread writes journal rows while pool threads are working and a reader must
never block behind a writer.

Schema is versioned from the first release. :data:`SCHEMA_VERSION` is the
current number, ``meta.schema_version`` is what the file on disk says, and
migrations are forward-only functions in :data:`_MIGRATIONS` applied in order.
There is no downgrade path: an older build meeting a newer file refuses to
migrate rather than guessing, and says so.

Threading: :class:`Store` opens a connection per thread (``check_same_thread``
stays on, deliberately -- a shared connection across threads is how sqlite
misuse starts). Writes are small and single-row; nothing here runs in the audio
path.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "RippedRecordFormatter"
DB_FILENAME = "rrf.db"

#: Bump when adding a migration. Migrations run from the file's version to here.
SCHEMA_VERSION = 1


def db_path() -> Path:
    """Full path to the state database in the per-user config directory."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / DB_FILENAME


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Settings: the backing store for core.config. Values are JSON so a bool stays
-- a bool and a list stays a list; the config layer's public API is unchanged.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The album job journal. One row per album attempt, rewritten as it progresses.
-- 'state' is the job's own status; 'sides' is the per-side detail as JSON,
-- including each side's stage parameters *as applied*, which is what lets a
-- re-do offer the settings the side was actually made with rather than
-- whatever Settings happens to say later.
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    state         TEXT NOT NULL,
    release_mbid  TEXT,
    artist        TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    destination   TEXT NOT NULL DEFAULT '',
    wavs          TEXT NOT NULL DEFAULT '[]',
    mapping       TEXT NOT NULL DEFAULT '[]',
    sides         TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions(state);

-- Fetched releases, keyed by MusicBrainz ID. An optimisation only: every read
-- path falls through to the network when a row is absent, unusable or stale in
-- shape. 'complete' records whether the payload had everything worth caching --
-- a partial fetch is not written, so a cache hit is always a full answer.
CREATE TABLE IF NOT EXISTS releases (
    mbid        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    cover       BLOB,
    complete    INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL
);

-- The collection ledger. Albums land here automatically when one finishes, and
-- by hand for records owned but not yet ripped. 'destination' is a claim about
-- the filesystem and is reconciled against it on open, never trusted.
CREATE TABLE IF NOT EXISTS collection (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    release_mbid TEXT,
    status       TEXT NOT NULL DEFAULT 'wanted',
    destination  TEXT NOT NULL DEFAULT '',
    ripped_at    TEXT,
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS collection_status ON collection(status);
"""


def _migrate_to_1(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA_V1)


#: Forward-only, applied in order. Index i takes the file from version i to i+1.
_MIGRATIONS = [_migrate_to_1]


class SchemaTooNew(RuntimeError):
    """The file was written by a newer build than this one."""


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class Store:
    """A connection factory plus the small set of operations the app needs.

    Cheap to construct and safe to keep for the life of the process. The file is
    created and migrated on first use rather than in ``__init__``, so merely
    holding a Store never touches the disk.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else db_path()
        self._local = threading.local()
        self._ready = False
        self._ready_lock = threading.Lock()

    # -- connection ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
        return connection

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            connection = self._connect()
            version = self._read_version(connection)
            if version > SCHEMA_VERSION:
                raise SchemaTooNew(
                    f"{self.path.name} is schema v{version}; this build "
                    f"understands v{SCHEMA_VERSION}. Update the app rather than "
                    "letting an older build rewrite newer state.")
            for step in _MIGRATIONS[version:]:
                step(connection)
            if version != SCHEMA_VERSION:
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),))
            connection.commit()
            self._ready = True

    @staticmethod
    def _read_version(connection: sqlite3.Connection) -> int:
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0                      # no meta table: a fresh file
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    @contextmanager
    def write(self):
        """A transaction. Commits on success, rolls back on anything else."""
        self._ensure_schema()
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def read(self) -> sqlite3.Connection:
        self._ensure_schema()
        return self._connect()

    def schema_version(self) -> int:
        self._ensure_schema()
        return self._read_version(self._connect())

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    # -- settings -----------------------------------------------------------
    def get_settings(self) -> dict:
        """Every stored setting, JSON-decoded. Unreadable values are skipped.

        A single corrupt row must not cost the user every other preference, so
        this drops what it cannot parse rather than raising.
        """
        out: dict = {}
        for row in self.read().execute("SELECT key, value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return out

    def put_settings(self, values: dict) -> None:
        with self.write() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v)) for k, v in values.items()])

    def has_settings(self) -> bool:
        row = self.read().execute("SELECT COUNT(*) AS n FROM settings").fetchone()
        return bool(row["n"])
