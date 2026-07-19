"""The state database: schema, migrations, and the settings move.

Doctrine under test as much as behaviour: rrf.db is recoverable state and a
ledger, never the source of truth for the library. Delete it and the app must
come up on defaults with every FLAC on disk untouched.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core import config as core_config
from core.store import SCHEMA_VERSION, SchemaTooNew, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rrf.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def unbound_config():
    """core.config is process-global; never leave it pointed at a test db."""
    yield
    core_config.use_store(None)


# --------------------------------------------------------------------------- #
# Schema and versioning
# --------------------------------------------------------------------------- #
def test_a_fresh_file_is_created_at_the_current_schema(store):
    assert store.schema_version() == SCHEMA_VERSION
    tables = {r["name"] for r in store.read().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "settings", "sessions", "releases", "collection"} <= tables


def test_the_file_is_in_wal_mode(store):
    """The GUI thread journals while pool threads work; a reader must never
    block behind a writer."""
    mode = store.read().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_opening_an_existing_file_does_not_re_migrate(tmp_path):
    first = Store(tmp_path / "rrf.db")
    first.put_settings({"output_dir": "Z:/rips"})
    first.close()

    second = Store(tmp_path / "rrf.db")
    try:
        assert second.schema_version() == SCHEMA_VERSION
        assert second.get_settings()["output_dir"] == "Z:/rips"
    finally:
        second.close()


def test_a_file_from_the_future_is_refused_not_guessed_at(tmp_path):
    """Forward-only means forward-only. An older build meeting newer state
    stops rather than rewriting it into a shape it half understands."""
    path = tmp_path / "rrf.db"
    seed = Store(path)
    seed.schema_version()
    with seed.write() as connection:
        connection.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    seed.close()

    later = Store(path)
    try:
        with pytest.raises(SchemaTooNew) as raised:
            later.schema_version()
        assert "99" in str(raised.value)
    finally:
        later.close()


def test_the_migration_list_matches_the_declared_version():
    """A migration added without bumping the number would silently not run."""
    from core.store import _MIGRATIONS

    assert len(_MIGRATIONS) == SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Settings round-trip
# --------------------------------------------------------------------------- #
def test_settings_keep_their_types(store):
    """JSON values, so a bool stays a bool and a list stays a list."""
    store.put_settings({"s": "text", "n": 4, "b": True, "xs": [1, 2], "none": None})
    back = store.get_settings()

    assert back == {"s": "text", "n": 4, "b": True, "xs": [1, 2], "none": None}


def test_writing_a_setting_twice_updates_rather_than_duplicates(store):
    store.put_settings({"output_dir": "A"})
    store.put_settings({"output_dir": "B"})

    assert store.get_settings()["output_dir"] == "B"
    row = store.read().execute("SELECT COUNT(*) AS n FROM settings").fetchone()
    assert row["n"] == 1


def test_one_corrupt_row_does_not_cost_every_other_preference(store):
    store.put_settings({"good": "kept", "also_good": 3})
    with store.write() as connection:
        connection.execute("INSERT INTO settings(key, value) VALUES('bad', '{oops')")

    back = store.get_settings()

    assert back["good"] == "kept"
    assert back["also_good"] == 3
    assert "bad" not in back


# --------------------------------------------------------------------------- #
# The migration from settings.json
# --------------------------------------------------------------------------- #
def test_a_legacy_settings_file_moves_into_the_database(tmp_path, store):
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({
        "output_dir": "Z:/rips",
        "default_source_dir": "Z:/WAVs",
        "encode_workers": 3,
        "filename_side_letters": True,
    }), encoding="utf-8")

    assert core_config.migrate_json_to_store(store, legacy) is True

    core_config.use_store(store)
    cfg = core_config.load()
    assert cfg.output_dir == "Z:/rips"
    assert cfg.default_source_dir == "Z:/WAVs"
    assert cfg.encode_workers == 3
    assert cfg.filename_side_letters is True


def test_the_old_file_is_renamed_not_deleted(tmp_path, store):
    """At that instant it is the only copy of the user's preferences."""
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({"output_dir": "Z:/rips"}), encoding="utf-8")

    core_config.migrate_json_to_store(store, legacy)

    assert not legacy.exists()
    moved = tmp_path / "settings.json.migrated"
    assert moved.exists()
    assert json.loads(moved.read_text(encoding="utf-8"))["output_dir"] == "Z:/rips"


def test_the_migration_runs_once_and_never_overwrites_newer_state(tmp_path, store):
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({"output_dir": "OLD"}), encoding="utf-8")
    assert core_config.migrate_json_to_store(store, legacy) is True

    core_config.use_store(store)
    cfg = core_config.load()
    cfg.output_dir = "NEW"
    core_config.save(cfg)

    # A stale JSON reappearing must not clobber what the db now holds.
    legacy.write_text(json.dumps({"output_dir": "OLD AGAIN"}), encoding="utf-8")
    assert core_config.migrate_json_to_store(store, legacy) is False
    assert core_config.load().output_dir == "NEW"


def test_a_missing_or_corrupt_legacy_file_is_a_no_op(tmp_path, store):
    assert core_config.migrate_json_to_store(store, tmp_path / "absent.json") is False

    broken = tmp_path / "settings.json"
    broken.write_text("{not json", encoding="utf-8")
    assert core_config.migrate_json_to_store(store, broken) is False
    assert broken.exists(), "an unreadable file was moved as though it had migrated"


def test_unknown_keys_in_a_legacy_file_are_dropped(tmp_path, store):
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({
        "output_dir": "Z:/rips", "from_a_future_build": "???"}), encoding="utf-8")

    core_config.migrate_json_to_store(store, legacy)

    assert "from_a_future_build" not in store.get_settings()
    core_config.use_store(store)
    assert core_config.load().output_dir == "Z:/rips"


# --------------------------------------------------------------------------- #
# The public API is unchanged, and the app survives without a store
# --------------------------------------------------------------------------- #
def test_callers_never_learn_where_the_bytes_went(tmp_path, store):
    """The whole contract of item 1: load() and save(), same as ever."""
    core_config.use_store(store)

    cfg = core_config.load()
    cfg.last_artist = "Miles Davis"
    core_config.save(cfg)

    assert core_config.load().last_artist == "Miles Davis"


def test_with_no_store_the_json_path_still_works(tmp_path):
    """State is recoverable, so nothing may *require* the database."""
    core_config.use_store(None)
    path = tmp_path / "settings.json"

    cfg = core_config.load(path)
    cfg.last_album = "Kind of Blue"
    core_config.save(cfg, path)

    assert core_config.load(path).last_album == "Kind of Blue"
    assert path.exists()


def test_an_unopenable_store_degrades_to_defaults(tmp_path):
    """Deleting rrf.db costs preferences, never the library."""
    class Broken:
        path = tmp_path / "rrf.db"

        def get_settings(self):
            raise sqlite3.OperationalError("disk I/O error")

        def has_settings(self):
            raise sqlite3.OperationalError("disk I/O error")

    core_config.use_store(Broken())
    cfg = core_config.load()

    assert cfg.output_dir == core_config.Config().output_dir
    assert core_config.migrate_json_to_store(Broken()) is False
