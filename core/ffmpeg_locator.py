"""Single source of truth for locating (and, if needed, downloading) ffmpeg.

The rest of the app must never reach for ffmpeg directly. It calls
:func:`configure_pydub` (or :func:`ensure_ffmpeg`) here, and this module decides
where the binary lives. Packaging (Task 6) then only has to change this one file
-- e.g. to point at a binary bundled inside the frozen app instead of the
per-user download.

Strategy
--------
Resolution order:

1. A copy managed by the ``ffmpeg-downloader`` package (a static build unpacked
   into the per-user data dir). This is the preferred path: no admin rights, no
   system PATH pollution, and it foreshadows the bundling story.
2. An ``ffmpeg`` already on the system ``PATH`` (developer convenience / CI).

If neither is present and ``auto_download`` is set, we shell out to
``python -m ffmpeg_downloader install -y`` to fetch one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import ffmpeg_downloader as ffdl


class FFmpegNotAvailable(RuntimeError):
    """Raised when ffmpeg cannot be located and could not be downloaded."""


def _managed_ffmpeg() -> tuple[Path | None, Path | None]:
    """Paths to the ffmpeg-downloader-managed binaries, or ``(None, None)``."""
    if ffdl.installed():
        return Path(ffdl.ffmpeg_path), Path(ffdl.ffprobe_path)
    return None, None


def _path_ffmpeg() -> tuple[Path | None, Path | None]:
    """Paths to an ffmpeg already on the system ``PATH``, or ``(None, None)``."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return (Path(ffmpeg) if ffmpeg else None,
            Path(ffprobe) if ffprobe else None)


def find_ffmpeg() -> tuple[Path | None, Path | None]:
    """Locate ffmpeg/ffprobe *without* downloading.

    Returns ``(ffmpeg_path, ffprobe_path)``; either element may be ``None``.
    """
    ffmpeg, ffprobe = _managed_ffmpeg()
    if ffmpeg is not None:
        return ffmpeg, ffprobe
    return _path_ffmpeg()


def download_ffmpeg() -> None:
    """Fetch a static ffmpeg build via ffmpeg-downloader (needs network access)."""
    subprocess.run(
        [sys.executable, "-m", "ffmpeg_downloader", "install", "-y"],
        check=True,
    )


def ensure_ffmpeg(auto_download: bool = True) -> tuple[Path, Path]:
    """Return existing ``(ffmpeg, ffprobe)`` paths, downloading on first use.

    Raises :class:`FFmpegNotAvailable` if ffmpeg is missing and could not be
    obtained. ``ffprobe`` is best-effort and may equal ``None`` only when found
    on PATH without a sibling ffprobe; the managed build always ships both.
    """
    ffmpeg, ffprobe = find_ffmpeg()
    if ffmpeg is None and auto_download:
        download_ffmpeg()
        ffmpeg, ffprobe = find_ffmpeg()
    if ffmpeg is None:
        raise FFmpegNotAvailable(
            "ffmpeg could not be located or downloaded. Install it with "
            "`python -m ffmpeg_downloader install` or put ffmpeg on your PATH."
        )
    return ffmpeg, ffprobe


def configure_pydub(auto_download: bool = True) -> Path:
    """Point pydub at the resolved ffmpeg/ffprobe and return the ffmpeg path.

    pydub is imported lazily so that merely importing this module stays cheap
    and free of side effects. We both set ``AudioSegment.converter`` explicitly
    (used for encoding) and prepend the binary's directory to ``PATH`` for this
    process (pydub discovers ffprobe via a PATH lookup when probing media).
    """
    from pydub import AudioSegment

    ffmpeg, ffprobe = ensure_ffmpeg(auto_download=auto_download)

    bin_dir = str(ffmpeg.parent)
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    AudioSegment.converter = str(ffmpeg)
    if ffprobe is not None:
        AudioSegment.ffprobe = str(ffprobe)
    return ffmpeg
