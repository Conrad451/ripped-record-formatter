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
import os
from dataclasses import asdict, dataclass, field, fields
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

    # --- metadata lookup ---
    metadata_contact: str = ""
    """Contact (email or URL) sent in the MusicBrainz User-Agent, so they can
    reach *you* about your traffic. Empty (default) is allowed: lookups then
    identify the app and explicitly claim no contact. See
    :func:`core.metadata_lookup.user_agent`."""

    # --- output naming ---
    filename_side_letters: bool = False
    """Album jobs write every side into one flat output folder. Off (default):
    filenames are numbered continuously across the album -- ``[01]``..``[NN]``,
    side B carrying on where side A stopped. On: filenames use the per-side
    number prefixed by its side letter instead -- ``[A01]``, ``[B01]``. Either
    way the *tags* are untouched: TRACKNUMBER/TRACKTOTAL stay per-side."""

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

    # --- recording ---
    record_device: str = ""
    """Input device, remembered by NAME -- PortAudio indices shift when devices
    come and go, so an index would silently point at the wrong input."""

    record_samplerate: int = 0
    """0 = use the device's native rate. Set explicitly to pin it (a Realtek line
    input often reports 192000 under WASAPI even though the chain is 44.1k)."""

    record_subtype: str = "PCM_16"
    """PCM_16 (default) or PCM_24."""

    record_output_dir: str = ""
    record_next_file: str = "SideA.wav"

    # --- audition playback ---
    preview_lead_in_s: float = 5.0
    """Preview a cut by playing from this many seconds before it, straight
    through, so the ear judges whether it lands in silence or mid-note."""

    marker_nudge_ms: int = 50
    """Arrow-key step when nudging the selected split marker. Nudge-then-preview
    is meant to be a two-key rhythm, not a mouse dance."""

    # --- review sanity guard ---
    wrong_side_frac: float = 0.5
    """Fraction of a side's expected boundaries that may go unconfirmed before
    the proposal is flagged for review rather than trusted. An N-track side has
    N-1 boundaries; if more than this fraction come back unresolved, the side is
    parked as "needs attention" -- usually the wrong side/release is mapped, but
    a record with genuine gapless segues trips it honestly. Raise it toward 1.0
    to be told less often; lower it to be told sooner."""

    # --- encoding ---
    encode_workers: int = field(default_factory=lambda: min(4, os.cpu_count() or 1))
    """How many tracks to encode in parallel (each is an independent ffmpeg run)."""

    album_analysis_workers: int = 1
    """Sides analysed at once in Album mode. Conservative: each holds a whole
    side in RAM and rips usually stream off a contended network share."""

    # --- window layout (splitter sizes in px; 0 = use default proportion) ---
    main_split_top: int = 0
    main_split_bottom: int = 0
    meta_split_top: int = 0
    meta_split_bottom: int = 0


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
