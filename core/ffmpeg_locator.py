"""Single source of truth for locating (and, if needed, downloading) ffmpeg.

The rest of the app must never reach for ffmpeg directly. It calls
:func:`configure_pydub` (or :func:`ensure_ffmpeg`) here, and this module decides
where the binary lives. Packaging (Task 6) then only has to change this one file
-- e.g. to point at a binary bundled inside the frozen app instead of the
per-user download.

Strategy
--------
Resolution order:

0. **A copy bundled inside the frozen app** -- ``ffmpeg/ffmpeg.exe`` beside the
   executable (or under ``sys._MEIPASS``). Only ever present in a PyInstaller
   build, and deliberately checked *first*: the packaged app must convert end to
   end on a machine with no ffmpeg installed and no network, and it must not be
   at the mercy of whatever stale ffmpeg happens to be on the user's PATH.
1. A copy managed by the ``ffmpeg-downloader`` package (a static build unpacked
   into the per-user data dir). This is the path a developer running from source
   gets, and the packaging story was written on top of it.
2. An ``ffmpeg`` already on the system ``PATH`` (developer convenience / CI).

If none is present and ``auto_download`` is set, we shell out to
``python -m ffmpeg_downloader install -y`` to fetch one. A frozen app never gets
that far -- step 0 always wins -- which is what makes it network-free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import ffmpeg_downloader as ffdl
except Exception:            # not shipped in the frozen bundle -- it is not needed
    ffdl = None


class FFmpegNotAvailable(RuntimeError):
    """Raised when ffmpeg cannot be located and could not be downloaded."""


def _bundle_roots() -> list[Path]:
    """Where a frozen build might have put its bundled ffmpeg.

    Empty when running from source, which is what keeps dev behaviour identical:
    :func:`find_ffmpeg` falls straight through to the downloader/PATH lookup.
    """
    if not getattr(sys, "frozen", False):
        return []
    roots: list[Path] = []
    # onedir: next to the executable. onefile: extracted under _MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(sys.executable).parent)
    # PyInstaller onedir >=6 puts collected data under _internal/.
    roots.extend([root / "_internal" for root in list(roots)])
    return roots


def _bundled_ffmpeg() -> tuple[Path | None, Path | None]:
    """ffmpeg shipped inside the frozen app, or ``(None, None)`` from source."""
    for root in _bundle_roots():
        candidate = root / "ffmpeg" / "ffmpeg.exe"
        if not candidate.exists():
            candidate = root / "ffmpeg" / "ffmpeg"      # non-Windows
        if candidate.exists():
            probe = candidate.with_name(
                "ffprobe.exe" if candidate.suffix == ".exe" else "ffprobe")
            return candidate, (probe if probe.exists() else None)
    return None, None


def _managed_ffmpeg() -> tuple[Path | None, Path | None]:
    """Paths to the ffmpeg-downloader-managed binaries, or ``(None, None)``."""
    if ffdl is None:
        return None, None
    try:
        if ffdl.installed():
            return Path(ffdl.ffmpeg_path), Path(ffdl.ffprobe_path)
    except Exception:
        pass
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
    The bundled copy wins when frozen -- see the module docstring.
    """
    ffmpeg, ffprobe = _bundled_ffmpeg()
    if ffmpeg is not None:
        return ffmpeg, ffprobe
    ffmpeg, ffprobe = _managed_ffmpeg()
    if ffmpeg is not None:
        return ffmpeg, ffprobe
    return _path_ffmpeg()


def download_ffmpeg() -> None:
    """Fetch a static ffmpeg build via ffmpeg-downloader (needs network access).

    Never reached in a frozen build: the bundled copy resolves first.
    """
    if ffdl is None:
        raise FFmpegNotAvailable(
            "ffmpeg is not bundled and ffmpeg-downloader is unavailable.")
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


def prime_path(auto_download: bool = False) -> Path | None:
    """Put the resolved ffmpeg's directory on ``PATH`` -- *before* pydub is imported.

    pydub probes for ffmpeg at **import time** and, finding none on ``PATH``,
    prints ``RuntimeWarning: Couldn't find ffmpeg or avconv``. In a frozen build
    that warning is a lie -- ffmpeg is right there inside the bundle -- and it
    appears two lines above the smoke harness reporting that the bundled ffmpeg
    resolved fine. Contradicting yourself in your own output is how you train
    people to ignore warnings, so we pre-empt it rather than filter it: resolve
    ffmpeg first, put it on PATH, and pydub simply finds it.

    Safe to call before anything else; returns ``None`` (silently) when no ffmpeg
    can be found, leaving the existing error paths to complain properly later.
    """
    try:
        ffmpeg, _ = find_ffmpeg()
        if ffmpeg is None and auto_download:
            download_ffmpeg()
            ffmpeg, _ = find_ffmpeg()
        if ffmpeg is None:
            return None
    except Exception:
        return None

    bin_dir = str(ffmpeg.parent)
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    return ffmpeg


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
