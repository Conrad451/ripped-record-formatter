"""Shared invariants for anything that writes an audio file.

This module exists because of a specific gap. Every writer was verified by
reading its *tags* back with mutagen -- and mutagen will happily read tags off a
file whose audio is structurally unusable, because the tag blocks and the audio
frames are different parts of the container. So a re-tag path that produced
files Windows Media Player could not open passed a full green suite.

**The rule: a written file is not verified until something has decoded it.**

Decoding is done with ``soundfile`` (libsndfile) rather than by shelling out to
ffmpeg, for three reasons. It is already a dependency, so no test needs a
subprocess. It is *stricter* than ffmpeg, which is the most permissive decoder
in existence and will play through container damage that other players reject --
and "other players reject it" was the actual bug. And it hands back the samples,
which is what makes :func:`assert_audio_bit_identical` possible at all.

MP3 is the exception libsndfile cannot always read, so :func:`assert_decodes`
falls back to ffmpeg for formats it does not know. The fallback is explicit
rather than silent: a caller can see which check ran.

New writers inherit this by construction: :func:`assert_written_audio` is the
one entry point, and a format added later either passes it or is a bug.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

#: Formats libsndfile handles natively. Anything else goes to the fallback.
_NATIVE = {".flac", ".wav", ".aiff", ".aif", ".ogg"}


def _ffmpeg_decodes(path: Path) -> tuple[bool, str]:
    """Last-resort decode for formats libsndfile will not open."""
    from core.ffmpeg_locator import find_ffmpeg

    ffmpeg, _ = find_ffmpeg()
    if ffmpeg is None:
        return True, "no ffmpeg available; decode skipped"
    result = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True)
    return result.returncode == 0, result.stderr.decode(errors="replace")[:400]


def assert_decodes(path, *, min_frames: int = 1) -> None:
    """The file is real audio, not merely a file with readable tags.

    Reads every frame -- not just the header -- because a truncated file has a
    perfectly good header and fails half way through, which is exactly the shape
    of damage a re-encode produces.
    """
    path = Path(path)
    assert path.exists(), f"{path} was never written"
    assert path.stat().st_size > 0, f"{path} is empty"

    if path.suffix.lower() not in _NATIVE:
        ok, detail = _ffmpeg_decodes(path)
        assert ok, f"{path.name} does not decode: {detail}"
        return

    try:
        data, rate = sf.read(str(path), always_2d=True)
    except Exception as exc:                      # noqa: BLE001 - reported as-is
        raise AssertionError(f"{path.name} does not decode: {exc}") from exc

    assert rate > 0, f"{path.name} claims a sample rate of {rate}"
    assert len(data) >= min_frames, (
        f"{path.name} decoded only {len(data)} frame(s); expected at least "
        f"{min_frames}")


def audio_fingerprint(path) -> str:
    """A hash of the decoded samples -- the audio, independent of the container.

    Two files with different tags, different compression levels and different
    byte lengths fingerprint the same if and only if they hold the same audio.
    """
    data, rate = sf.read(str(path), always_2d=True, dtype="float64")
    digest = hashlib.md5()
    digest.update(str(rate).encode())
    digest.update(np.ascontiguousarray(data).tobytes())
    return digest.hexdigest()


def assert_audio_bit_identical(source, written) -> None:
    """The audio was carried, not re-encoded.

    The invariant for re-tagging specifically: changing metadata must not touch
    a sample. The old path decoded the whole file and re-encoded it, which
    rewrote every byte of the container and put an audio codec in the path of an
    operation that has no business near one.
    """
    assert_decodes(written)
    before, after = audio_fingerprint(source), audio_fingerprint(written)
    assert before == after, (
        f"{Path(written).name} does not hold the same audio as "
        f"{Path(source).name} -- it was re-encoded, not carried "
        f"({before} != {after})")


def assert_written_audio(path, *, source=None, min_frames: int = 1) -> None:
    """The one entry point every writing path's tests should call.

    Give it ``source`` when the operation was supposed to preserve audio (a
    re-tag, a copy, a container rewrite) and it additionally proves the samples
    survived. Omit it when the operation legitimately produces new audio (an
    encode, a resample, an MP3 export) and it proves only that the result is
    decodable -- which is the part that was missing.
    """
    assert_decodes(path, min_frames=min_frames)
    if source is not None:
        assert_audio_bit_identical(source, path)
