"""Build restoration/splitting objects from a :class:`core.config.Config`.

Kept separate from :mod:`core.config` (imported everywhere) so the heavy DSP
imports stay lazy -- they happen inside these functions, only when a job is
actually being assembled. The restoration chain order is fixed here to the
reviewed default: rumble -> hum -> noise -> declick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Config


def build_stages(cfg: "Config") -> list:
    """Enabled restoration stages, in the fixed chain order."""
    from core.restoration import Declick, HumRemoval, NoiseReduction, RumbleFilter

    stages: list = []
    if cfg.rumble_enabled:
        stages.append(RumbleFilter(cutoff_hz=cfg.rumble_cutoff_hz, order=cfg.rumble_order))
    if cfg.hum_enabled:
        stages.append(HumRemoval(base_freq=cfg.hum_base_freq, harmonics=cfg.hum_harmonics,
                                 quality=cfg.hum_quality))
    if cfg.noise_enabled:
        stages.append(NoiseReduction(strength=cfg.noise_strength,
                                     profile_start=cfg.noise_profile_start,
                                     profile_duration=cfg.noise_profile_duration))
    if cfg.declick_enabled:
        stages.append(Declick())
    return stages


def build_policy(cfg: "Config"):
    """OutputPolicy from the (only) user-facing headroom field."""
    from core.restoration import OutputPolicy

    return OutputPolicy(headroom_target_dbfs=cfg.headroom_target_dbfs)


def build_silence_params(cfg: "Config"):
    """SilenceParams from every splitting tunable in the config."""
    from core.splitting import SilenceParams

    return SilenceParams(
        silence_threshold_db=cfg.silence_threshold_db,
        min_silence=cfg.min_silence,
        min_track_length=cfg.min_track_length,
        frame_ms=cfg.frame_ms,
        hop_ms=cfg.hop_ms,
        db_floor_eps=cfg.db_floor_eps,
        depth_ref_db=cfg.depth_ref_db,
        duration_ref_s=cfg.duration_ref_s,
        quality_depth_weight=cfg.quality_depth_weight,
        confidence_round_digits=cfg.confidence_round_digits,
        proximity_weight=cfg.proximity_weight,
        post_miss_penalty=cfg.post_miss_penalty,
    )
