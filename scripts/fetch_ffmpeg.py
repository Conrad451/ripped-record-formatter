"""Fetch the pinned ffmpeg build the packaged app ships.

Run once before building:

    python scripts/fetch_ffmpeg.py

It downloads a *specific, immutable* ffmpeg release, verifies its size, and drops
``ffmpeg.exe`` and ``ffprobe.exe`` into ``vendor/ffmpeg/``. The PyInstaller spec
collects from there.

Why not just reuse whatever ``ffmpeg-downloader`` put in the user's data dir?
Because that is "whatever was latest the day the developer first ran the app" --
it is not reproducible, and two machines would produce two different bundles.
Pinning the version here means a clean clone builds the same bytes.

``vendor/`` is gitignored: a 200 MB binary does not belong in a source repo, and
ffmpeg is separately licensed (see build.md).
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# The exact build shipped. Immutable GitHub release asset -- the gyan.dev
# /builds/ URL serves the same bytes but is a rolling path.
FFMPEG_VERSION = "8.1.2-essentials_build"
FFMPEG_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/"
    "ffmpeg-8.1.2-essentials_build.zip"
)
EXPECTED_BYTES = 109_728_040

WANTED = ("ffmpeg.exe", "ffprobe.exe")   # ffplay is not shipped; nothing calls it

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "ffmpeg"


def main() -> int:
    if all((VENDOR / name).exists() for name in WANTED):
        print(f"vendor/ffmpeg already populated ({FFMPEG_VERSION}); nothing to do.")
        return 0

    print(f"Downloading ffmpeg {FFMPEG_VERSION}\n  {FFMPEG_URL}")
    with urllib.request.urlopen(FFMPEG_URL) as response:
        payload = response.read()

    if len(payload) != EXPECTED_BYTES:
        print(f"ERROR: expected {EXPECTED_BYTES} bytes, got {len(payload)}. "
              "The pinned release moved -- do not ship this.", file=sys.stderr)
        return 1

    digest = hashlib.sha256(payload).hexdigest()
    print(f"  {len(payload):,} bytes, sha256 {digest}")

    VENDOR.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.namelist():
            name = Path(member).name
            if name in WANTED:
                with archive.open(member) as src, (VENDOR / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                size = (VENDOR / name).stat().st_size
                print(f"  -> vendor/ffmpeg/{name}  ({size / 1_048_576:.0f} MB)")
                extracted += 1

    if extracted != len(WANTED):
        print(f"ERROR: found {extracted} of {len(WANTED)} expected binaries.",
              file=sys.stderr)
        return 1
    print("Done. Now: python -m PyInstaller RippedRecordFormatter.spec --noconfirm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
