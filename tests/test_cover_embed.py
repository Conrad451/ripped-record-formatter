"""Cover-art embedding in the converter."""

from __future__ import annotations

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from core.converter import convert_wavs_to_flacs
from core.ffmpeg_locator import configure_pydub
from core.metadata_lookup import CoverArt
from core.tracks import Tracks

# Minimal "PNG" -- valid signature, arbitrary payload; mutagen stores bytes as-is.
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-cover-bytes" * 4


def test_convert_embeds_front_cover(tmp_path):
    configure_pydub()
    src = tmp_path / "a.wav"
    sf.write(str(src), np.zeros(4410, dtype=np.float32), 44100, subtype="PCM_16")

    cover = CoverArt(data=_FAKE_PNG, mime="image/png")
    track = Tracks(1, "Song", "Album", "Artist", src)
    result = convert_wavs_to_flacs([track], tmp_path / "out", configure=False, cover=cover)

    assert not result.warnings, result.warnings
    flac = FLAC(str(result.outcomes[0].output_path))
    assert len(flac.pictures) == 1
    pic = flac.pictures[0]
    assert pic.type == 3            # front cover
    assert pic.desc == "front cover"
    assert pic.mime == "image/png"
    assert pic.data == _FAKE_PNG


def test_convert_without_cover_has_no_picture(tmp_path):
    configure_pydub()
    src = tmp_path / "a.wav"
    sf.write(str(src), np.zeros(4410, dtype=np.float32), 44100, subtype="PCM_16")
    track = Tracks(1, "Song", "Album", "Artist", src)
    result = convert_wavs_to_flacs([track], tmp_path / "out", configure=False)
    flac = FLAC(str(result.outcomes[0].output_path))
    assert len(flac.pictures) == 0


def test_cover_embed_failure_becomes_warning_not_crash(tmp_path):
    configure_pydub()
    src = tmp_path / "a.wav"
    sf.write(str(src), np.zeros(4410, dtype=np.float32), 44100, subtype="PCM_16")
    # A CoverArt whose .data isn't bytes -> mutagen fails -> warning, no raise.
    bad = CoverArt(data="not-bytes", mime="image/png")  # type: ignore[arg-type]
    track = Tracks(1, "Song", "Album", "Artist", src)
    result = convert_wavs_to_flacs([track], tmp_path / "out", configure=False, cover=bad)
    assert result.total == 1
    assert any("cover" in w.lower() for w in result.warnings), result.warnings
