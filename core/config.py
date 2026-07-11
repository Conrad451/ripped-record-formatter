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
    """User-facing persistent settings.

    Flat by design: :func:`load` fills missing keys with these defaults and
    ignores unknown ones, so the schema can grow without breaking old files.
    Empty string means "unset / ask". Stage objects / params are *not* built
    here (that would drag numpy/scipy into a widely-imported module) -- see
    :mod:`core.job_settings`.
    """

    # --- directories + last-used metadata ---
    source_dir: str = ""
    output_dir: str = ""
    last_artist: str = ""
    last_album: str = ""

    # --- restoration: per-stage enable toggles (chain order is fixed:
    #     rumble -> hum -> noise -> declick) ---
    rumble_enabled: bool = True
    hum_enabled: bool = True
    noise_enabled: bool = True
    declick_enabled: bool = True

    # --- RumbleFilter ---
    rumble_cutoff_hz: float = 25.0
    rumble_order: int = 4

    # --- HumRemoval ---
    hum_base_freq: float = 60.0
    hum_harmonics: int = 4
    hum_quality: float = 30.0

    # --- NoiseReduction ---
    noise_strength: float = 0.5
    noise_profile_start: float = 0.0
    noise_profile_duration: float = 2.0

    # --- OutputPolicy ---
    headroom_target_dbfs: float = -0.1

    # --- splitting: SilenceParams ---
    silence_threshold_db: float = -40.0
    min_silence: float = 1.0
    min_track_length: float = 20.0
    frame_ms: float = 20.0
    hop_ms: float = 10.0
    db_floor_eps: float = 1e-10
    depth_ref_db: float = 20.0
    duration_ref_s: float = 2.0
    quality_depth_weight: float = 0.5
    confidence_round_digits: int = 4
    proximity_weight: float = 0.5
    post_miss_penalty: float = 0.8

    # --- anchored-search windows ---
    window_s: float = 15.0
    speed_tolerance: float = 0.02


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
