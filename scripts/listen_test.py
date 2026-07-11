"""Disposable stakeholder A/B tool: render restoration variants of one WAV.

Usage:
    python scripts/listen_test.py <input.wav> <output_dir>

Writes five files into <output_dir> for side-by-side listening:
    <stem>__original.wav   (untouched copy)
    <stem>__hum_only.wav
    <stem>__noise_0.5.wav
    <stem>__noise_0.8.wav
    <stem>__full_chain.wav (hum + noise 0.5 + declick)
"""

import shutil
import sys
from pathlib import Path

# Allow `python scripts/listen_test.py` from the repo root to import `core`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.restoration import Declick, HumRemoval, NoiseReduction, restore


def _progress(name: str, idx: int, total: int) -> None:
    print(f"    [{idx}/{total}] {name}")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python scripts/listen_test.py <input.wav> <output_dir>")
        return 2

    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    if not src.is_file():
        print(f"Input not found: {src}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = src.stem
    variants = {
        f"{stem}__hum_only.wav": [HumRemoval()],
        f"{stem}__noise_0.5.wav": [NoiseReduction(strength=0.5)],
        f"{stem}__noise_0.8.wav": [NoiseReduction(strength=0.8)],
        f"{stem}__full_chain.wav": [HumRemoval(), NoiseReduction(strength=0.5), Declick()],
    }

    original = out_dir / f"{stem}__original.wav"
    shutil.copy2(src, original)
    print(f"Copied original -> {original}")

    for filename, stages in variants.items():
        dest = out_dir / filename
        print(f"Rendering {filename}:")
        restore(src, dest, stages, on_progress=_progress)

    print(f"\nDone. Five files for A/B comparison are in: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
