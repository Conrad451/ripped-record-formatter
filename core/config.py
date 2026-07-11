"""Persistent user settings, stored as JSON in the platform config directory.

Remembers the last-used source/output directories and artist/album so the UI can
pre-fill them. Pure data + file I/O -- no prompts, no printing. On first run (or
if the file is missing/corrupt) sane defaults are returned instead of raising.

The public API is intentionally tiny::

    cfg = config.load()          # -> Config (defaults if absent/corrupt)
    cfg.last_artist = "Miles"
    config.save(cfg)             # writes JSON, creating dirs as needed

Both :func:`load` and :func:`save` accept an optional ``path`` override, mainly
so tests can round-trip without touching the real user config.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "RippedRecordFormatter"
CONFIG_FILENAME = "settings.json"


@dataclass
class Config:
    """User-facing persistent settings. Empty string means "unset / ask"."""

    source_dir: str = ""
    output_dir: str = ""
    last_artist: str = ""
    last_album: str = ""


def config_path() -> Path:
    """Full path to the settings file in the per-user config directory."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / CONFIG_FILENAME


def load(path: str | Path | None = None) -> Config:
    """Load settings, returning defaults if the file is missing or unreadable.

    Unknown keys in the file are ignored and missing keys fall back to defaults,
    so the format can evolve without breaking old or new config files.
    """
    path = Path(path) if path is not None else config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return Config()

    known = {f.name for f in fields(Config)}
    filtered = {k: v for k, v in raw.items() if k in known}
    return Config(**filtered)


def save(config: Config, path: str | Path | None = None) -> Path:
    """Write settings as pretty JSON, creating the config directory if needed."""
    path = Path(path) if path is not None else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    return path
