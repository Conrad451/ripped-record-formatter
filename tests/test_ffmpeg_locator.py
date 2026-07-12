"""ffmpeg resolution order: bundled first when frozen, downloader/PATH from source.

This is the contract that makes the packaged app work with no ffmpeg installed
and no network. It is also the one that must NOT change dev behaviour: running
from source has to keep using the ffmpeg-downloader copy exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core import ffmpeg_locator as loc


def _fake_bundle(root: Path) -> Path:
    """A bundle laid out the way PyInstaller onedir does."""
    bin_dir = root / "ffmpeg"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg.exe"
    ffprobe = bin_dir / "ffprobe.exe"
    ffmpeg.write_bytes(b"MZ")          # content is irrelevant; existence is not
    ffprobe.write_bytes(b"MZ")
    return ffmpeg


def test_from_source_the_bundled_lookup_is_inert(monkeypatch, tmp_path):
    """Not frozen => no bundle roots => dev behaviour is untouched."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert loc._bundle_roots() == []
    assert loc._bundled_ffmpeg() == (None, None)


def test_frozen_bundled_ffmpeg_wins_over_downloader_and_path(monkeypatch, tmp_path):
    """The whole point: the bundle's own ffmpeg beats everything else."""
    bundled = _fake_bundle(tmp_path)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    # Both of the other sources are available and must LOSE.
    downloader = tmp_path / "downloader" / "ffmpeg.exe"
    downloader.parent.mkdir()
    downloader.write_bytes(b"MZ")
    monkeypatch.setattr(loc, "_managed_ffmpeg",
                        lambda: (downloader, downloader.with_name("ffprobe.exe")))
    monkeypatch.setattr(loc, "_path_ffmpeg",
                        lambda: (Path("C:/somewhere/on/path/ffmpeg.exe"), None))

    ffmpeg, ffprobe = loc.find_ffmpeg()
    assert ffmpeg == bundled                       # bundled won
    assert ffmpeg != downloader
    assert ffprobe == bundled.with_name("ffprobe.exe")


def test_frozen_onedir_layout_beside_the_executable(monkeypatch, tmp_path):
    """onedir puts data under _internal/ next to the exe, not in _MEIPASS."""
    exe_dir = tmp_path / "RippedRecordFormatter"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    bundled = _fake_bundle(internal)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "RippedRecordFormatter.exe"))
    monkeypatch.setattr(loc, "_managed_ffmpeg", lambda: (None, None))
    monkeypatch.setattr(loc, "_path_ffmpeg", lambda: (None, None))

    ffmpeg, _ = loc.find_ffmpeg()
    assert ffmpeg == bundled


def test_frozen_with_no_bundle_still_falls_back(monkeypatch, tmp_path):
    """A frozen build that somehow shipped without ffmpeg is not a crash."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)   # empty
    fallback = tmp_path / "path-ffmpeg.exe"
    fallback.write_bytes(b"MZ")
    monkeypatch.setattr(loc, "_managed_ffmpeg", lambda: (None, None))
    monkeypatch.setattr(loc, "_path_ffmpeg", lambda: (fallback, None))

    assert loc.find_ffmpeg()[0] == fallback


def test_dev_order_is_downloader_then_path(monkeypatch, tmp_path):
    """Unchanged from before packaging: managed copy beats PATH."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    managed = tmp_path / "managed.exe"
    managed.write_bytes(b"MZ")
    on_path = tmp_path / "onpath.exe"
    on_path.write_bytes(b"MZ")
    monkeypatch.setattr(loc, "_managed_ffmpeg", lambda: (managed, None))
    monkeypatch.setattr(loc, "_path_ffmpeg", lambda: (on_path, None))

    assert loc.find_ffmpeg()[0] == managed

    monkeypatch.setattr(loc, "_managed_ffmpeg", lambda: (None, None))
    assert loc.find_ffmpeg()[0] == on_path


def test_missing_ffmpeg_downloader_is_survivable(monkeypatch):
    """The frozen bundle does not ship ffmpeg-downloader; importing must not fail."""
    monkeypatch.setattr(loc, "ffdl", None)
    assert loc._managed_ffmpeg() == (None, None)
    with pytest.raises(loc.FFmpegNotAvailable):
        loc.download_ffmpeg()


def test_the_real_locator_still_finds_a_real_ffmpeg():
    """Sanity: on this machine, from source, resolution actually works."""
    ffmpeg, _ = loc.find_ffmpeg()
    assert ffmpeg is not None and ffmpeg.exists()
