"""Synthetic sanity tests for the restoration pipeline.

These are engineering sanity checks (does the hum notch bite, does the floor
drop, does staging clean up), not audiophile validation. Signals use fixed
random seeds so the measured numbers are reproducible.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from core import restoration as R

SR = 44100


def _mag_at(x: np.ndarray, freq: float) -> float:
    x = np.asarray(x).reshape(-1)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    return float(spectrum[np.argmin(np.abs(freqs - freq))])


def _db(a: float, b: float) -> float:
    return 20 * np.log10(a / b)


def _staging_dirs() -> set[str]:
    return {str(p) for p in Path(tempfile.gettempdir()).glob("rrf_restore_*")}


def test_hum_removal_attenuates_mains_preserves_reference(tmp_path):
    t = np.arange(int(SR * 4)) / SR
    tone = 0.3 * np.sin(2 * np.pi * 1000 * t)
    hum = (0.25 * np.sin(2 * np.pi * 60 * t)
           + 0.12 * np.sin(2 * np.pi * 120 * t)
           + 0.06 * np.sin(2 * np.pi * 180 * t))
    noise = 0.005 * np.random.default_rng(1).standard_normal(t.size)
    sig = (tone + hum + noise).astype(np.float32)

    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(src), sig, SR, subtype="PCM_16")
    R.HumRemoval(base_freq=60, harmonics=4).apply(src, dst)
    out, _ = sf.read(str(dst))

    drop_60 = _db(_mag_at(sig, 60), _mag_at(out, 60))
    change_1k = abs(_db(_mag_at(out, 1000), _mag_at(sig, 1000)))
    print(f"\n[hum] 60Hz drop = {drop_60:.1f} dB, 1kHz change = {change_1k:.4f} dB")

    assert drop_60 >= 40.0, drop_60          # measured ~59.6 dB
    assert change_1k <= 0.2, change_1k       # measured ~0.001 dB (bass untouched)


def test_noise_reduction_lowers_floor_preserves_tone(tmp_path):
    rng = np.random.default_rng(2)
    lead = 0.02 * rng.standard_normal(int(SR * 2))               # silent lead-in
    body = (0.3 * np.sin(2 * np.pi * 1000 * (np.arange(int(SR * 2)) / SR))
            + 0.02 * rng.standard_normal(int(SR * 2)))
    sig = np.concatenate([lead, body]).astype(np.float32)

    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(src), sig, SR, subtype="PCM_16")
    R.NoiseReduction(strength=0.5, profile_start=0.0, profile_duration=2.0).apply(src, dst)
    out, _ = sf.read(str(dst))

    lead_n = int(SR * 2)
    rms_before = float(np.sqrt(np.mean(sig[:lead_n] ** 2)))
    rms_after = float(np.sqrt(np.mean(out[:lead_n] ** 2)))
    floor_drop = _db(rms_before, rms_after)
    tone_change = abs(_db(_mag_at(out[lead_n:], 1000), _mag_at(body, 1000)))
    print(f"\n[noise] floor RMS {rms_before:.5f} -> {rms_after:.5f} "
          f"({floor_drop:.1f} dB), 1kHz change = {tone_change:.2f} dB")

    assert rms_after < rms_before
    assert floor_drop >= 3.0, floor_drop     # measured ~5.8 dB
    assert tone_change <= 3.0, tone_change   # measured ~1.16 dB (tone preserved)


def test_declick_removes_clicks_preserves_format(tmp_path):
    t = np.arange(int(SR * 3)) / SR
    sig = 0.2 * np.sin(2 * np.pi * 1000 * t)
    rng = np.random.default_rng(3)
    idx = rng.choice(t.size, size=200, replace=False)
    sig[idx] += rng.choice([-1, 1], 200) * 0.9
    sig = np.clip(sig, -1, 1).astype(np.float32)

    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(src), sig, SR, subtype="PCM_16")
    R.Declick().apply(src, dst)
    out, _ = sf.read(str(dst))

    before = int((np.abs(sig) > 0.6).sum())
    after = int((np.abs(out) > 0.6).sum())
    print(f"\n[declick] samples>|0.6|: {before} -> {after}")

    assert after <= before * 0.1              # measured 200 -> 0
    assert sf.info(str(dst)).samplerate == SR
    assert sf.info(str(dst)).subtype == "PCM_16"   # bit depth preserved


def test_restore_pipeline_and_staging_cleanup_success(tmp_path):
    t = np.arange(int(SR * 3)) / SR
    sig = (0.3 * np.sin(2 * np.pi * 1000 * t)
           + 0.2 * np.sin(2 * np.pi * 60 * t)
           + 0.01 * np.random.default_rng(4).standard_normal(t.size)).astype(np.float32)
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(src), sig, SR, subtype="PCM_16")

    before = _staging_dirs()
    progress: list[tuple[str, int, int]] = []
    result = R.restore(
        src, dst,
        [R.HumRemoval(), R.NoiseReduction(), R.Declick()],
        on_progress=lambda name, i, total: progress.append((name, i, total)),
    )

    assert dst.exists()
    assert result.stages_applied == ["Hum removal", "Noise reduction", "Declick"]
    assert progress == [("Hum removal", 1, 3), ("Noise reduction", 2, 3), ("Declick", 3, 3)]
    assert result.samplerate == SR and result.subtype == "PCM_16"
    # no staging dir leaked
    assert not (_staging_dirs() - before)


def test_staging_cleanup_on_induced_failure(tmp_path):
    class ExplodingStage(R.Stage):
        name = "Boom"

        def apply(self, in_path, out_path):
            raise RuntimeError("induced failure")

    t = np.arange(int(SR)) / SR
    sig = (0.3 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(src), sig, SR, subtype="PCM_16")

    before = _staging_dirs()
    import pytest

    with pytest.raises(RuntimeError, match="induced failure"):
        R.restore(src, dst, [R.HumRemoval(), ExplodingStage()])

    assert not dst.exists()                    # never produced
    assert not (_staging_dirs() - before)      # staging still cleaned up
