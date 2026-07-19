"""Every path that writes an audio file writes a file that decodes.

The P0 this closes: Re-tag was run against a folder of legacy FLACs and produced
files Windows Media Player refused to open (0xC00D36C4), double-stamped
filenames, and a second generation sitting beside the first -- 28 files in a
folder that holds 14.

The suite was fully green throughout, because every writer was verified by
reading its *tags* back. Mutagen reads tags off structurally unusable audio
quite happily; the tag blocks and the audio frames are different parts of the
container. Nothing had ever decoded an output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.converter import retag_flacs
from core.tracks import Tracks, strip_track_prefix, track_filename
from tests.audio_invariants import (
    assert_audio_bit_identical,
    assert_decodes,
    assert_written_audio,
    audio_fingerprint,
)

SR = 44100


@pytest.fixture(scope="module")
def ffmpeg():
    from core.ffmpeg_locator import configure_pydub, find_ffmpeg

    configure_pydub()
    path, _ = find_ffmpeg()
    if path is None:
        pytest.skip("no ffmpeg available")
    return path


def _flac(ffmpeg, path: Path, *, seconds=1.0, subtype="PCM_16", title=None) -> Path:
    """A real, valid, tagged FLAC on disk."""
    wav = path.with_suffix(".seed.wav")
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(int(SR * seconds)) / SR))
    sf.write(str(wav), np.column_stack([tone, tone]).astype(np.float32), SR,
             subtype=subtype)
    args = [str(ffmpeg), "-y", "-i", str(wav)]
    if title:
        args += ["-metadata", f"TITLE={title}"]
    args.append(str(path))
    subprocess.run(args, capture_output=True)
    wav.unlink(missing_ok=True)
    assert path.exists()
    return path


def _track(source: Path, name="Femininomenon", num=1) -> Tracks:
    return Tracks(track_num=num, track_name=name, track_album="Short n' Sweet",
                  track_artist="Chappell Roan", track_wav_loc=source)


# --------------------------------------------------------------------------- #
# The invariant helper itself
# --------------------------------------------------------------------------- #
def test_the_helper_catches_a_file_with_good_tags_and_ruined_audio(ffmpeg, tmp_path):
    """The exact blind spot: mutagen is happy, the audio is not there.

    Truncating a FLAC after its metadata blocks leaves the tags perfectly
    readable. This is what a green suite looked like while shipping corruption.
    """
    from mutagen.flac import FLAC

    good = _flac(ffmpeg, tmp_path / "good.flac", seconds=2.0, title="Song")
    full_frames = len(sf.read(str(good), always_2d=True)[0])

    # Cut the tail only: metadata blocks live at the front of a FLAC, so this
    # leaves the tags perfectly intact and takes away audio -- which is the
    # shape of the blind spot.
    ruined = tmp_path / "ruined.flac"
    ruined.write_bytes(good.read_bytes()[:-4000])

    assert FLAC(str(ruined))["title"] == ["Song"]     # tags read fine...
    with pytest.raises(AssertionError):               # ...the audio is short
        assert_decodes(ruined, min_frames=full_frames)


def test_the_helper_notices_re_encoded_audio(ffmpeg, tmp_path):
    source = _flac(ffmpeg, tmp_path / "a.flac")
    different = tmp_path / "b.flac"
    tone = (0.3 * np.sin(2 * np.pi * 880 * np.arange(SR) / SR))
    sf.write(str(different), np.column_stack([tone, tone]).astype(np.float32), SR)

    with pytest.raises(AssertionError, match="re-encoded, not carried"):
        assert_audio_bit_identical(source, different)


def test_the_fingerprint_ignores_tags_and_compression(ffmpeg, tmp_path):
    """Same audio, different container, same fingerprint -- which is what makes
    it usable as a "was this re-encoded" check."""
    source = _flac(ffmpeg, tmp_path / "a.flac")
    recompressed = tmp_path / "b.flac"
    subprocess.run([str(ffmpeg), "-y", "-i", str(source), "-compression_level", "0",
                    "-metadata", "TITLE=different", str(recompressed)],
                   capture_output=True)

    assert source.read_bytes() != recompressed.read_bytes()
    assert audio_fingerprint(source) == audio_fingerprint(recompressed)


# --------------------------------------------------------------------------- #
# Re-tag: the path that was corrupting files
# --------------------------------------------------------------------------- #
def test_a_retag_writes_a_file_that_decodes(ffmpeg, tmp_path):
    source = _flac(ffmpeg, tmp_path / "[01] -  Femininomenon.flac",
                   title="[01] -  Femininomenon")
    before = audio_fingerprint(source)

    retag_flacs([_track(source)], tmp_path, configure=False)

    written = tmp_path / "[01] - Femininomenon.flac"
    assert_written_audio(written, min_frames=SR // 2)
    assert audio_fingerprint(written) == before


def test_a_retag_never_touches_the_audio(ffmpeg, tmp_path):
    """A re-tag changes metadata. The old path decoded and re-encoded the whole
    file, which is how an audio codec ended up in the path of a metadata edit."""
    source = _flac(ffmpeg, tmp_path / "src.flac", subtype="PCM_24")
    keep = tmp_path / "reference.flac"
    keep.write_bytes(source.read_bytes())

    retag_flacs([_track(source, name="Song")], tmp_path, configure=False)

    written = tmp_path / "[01] - Song.flac"
    assert_audio_bit_identical(keep, written)


def test_a_retag_never_starts_an_audio_decoder(ffmpeg, tmp_path, monkeypatch):
    """The fix, stated directly: re-tagging does not decode.

    Stronger than comparing samples, because it holds even for a codec that
    happens to round-trip losslessly. If anything ever puts pydub back into this
    path, this fails immediately rather than waiting for a format that does not
    survive the trip.
    """
    import pydub

    def explode(*args, **kwargs):
        raise AssertionError("re-tagging decoded the audio; it must only copy it")

    monkeypatch.setattr(pydub.AudioSegment, "from_file", explode)
    monkeypatch.setattr(pydub.AudioSegment, "from_wav", explode)

    source = _flac(ffmpeg, tmp_path / "src.flac", subtype="PCM_24")
    keep = tmp_path / "reference.flac"
    keep.write_bytes(source.read_bytes())

    retag_flacs([_track(source, name="Song")], tmp_path, configure=False)

    assert_audio_bit_identical(keep, tmp_path / "[01] - Song.flac")


# --------------------------------------------------------------------------- #
# Rename sanitation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("given,expected", [
    ("[01] -  Femininomenon", "Femininomenon"),
    ("[01] - Femininomenon", "Femininomenon"),
    ("[A01] - Side Opener", "Side Opener"),
    ("[12] - Track", "Track"),
    ("Femininomenon", "Femininomenon"),
    ("[Live] Song", "[Live] Song"),          # not a track stamp; left alone
    ("", ""),
])
def test_an_existing_stamp_is_stripped_before_restamping(given, expected):
    assert strip_track_prefix(given) == expected


def test_the_stakeholders_exact_filename_case(ffmpeg, tmp_path):
    """A file named and titled "[01] -  Femininomenon" re-tags to one prefix."""
    source = _flac(ffmpeg, tmp_path / "[01] -  Femininomenon.flac",
                   title="[01] -  Femininomenon")

    retag_flacs([_track(source, name="[01] -  Femininomenon")], tmp_path,
                configure=False)

    names = sorted(p.name for p in tmp_path.glob("*.flac"))
    assert names == ["[01] - Femininomenon.flac"], names
    assert "[01] - [01]" not in names[0]


def test_track_filename_strips_before_it_stamps():
    assert track_filename("[01] -  Femininomenon", 1) == "[01] - Femininomenon.flac"
    assert track_filename("Femininomenon", 1) == "[01] - Femininomenon.flac"


# --------------------------------------------------------------------------- #
# In-place semantics
# --------------------------------------------------------------------------- #
def test_retagging_in_place_leaves_one_generation_not_two(ffmpeg, tmp_path):
    """The field report: 28 files in a folder that holds 14."""
    sources = []
    for i, title in enumerate(["Femininomenon", "After Midnight", "Coffee"], 1):
        sources.append(_flac(ffmpeg, tmp_path / f"[{i:02d}] -  {title}.flac",
                             title=f"[{i:02d}] -  {title}"))
    tracks = [_track(p, name=f"[{i:02d}] -  {t}", num=i)
              for i, (t, p) in enumerate(
                  zip(["Femininomenon", "After Midnight", "Coffee"], sources), 1)]

    retag_flacs(tracks, tmp_path, configure=False, max_workers=2)

    remaining = sorted(p.name for p in tmp_path.glob("*.flac"))
    assert len(remaining) == 3, remaining
    for path in tmp_path.glob("*.flac"):
        assert_decodes(path)


def test_retagging_in_place_with_an_unchanged_name_still_works(ffmpeg, tmp_path):
    """Source and destination are literally the same file."""
    source = _flac(ffmpeg, tmp_path / "[01] - Song.flac")
    before = audio_fingerprint(source)

    retag_flacs([_track(source, name="Song")], tmp_path, configure=False)

    assert sorted(p.name for p in tmp_path.glob("*.flac")) == ["[01] - Song.flac"]
    assert_decodes(source)
    assert audio_fingerprint(source) == before


def test_retagging_to_another_folder_leaves_the_source_untouched(ffmpeg, tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    out_dir = tmp_path / "out"
    source = _flac(ffmpeg, source_dir / "[01] -  Song.flac", title="[01] -  Song")
    original = source.read_bytes()

    retag_flacs([_track(source, name="Song")], out_dir, configure=False)

    assert source.exists(), "a cross-folder re-tag consumed the original"
    assert source.read_bytes() == original
    written = out_dir / "[01] - Song.flac"
    assert_written_audio(written, source=source)


def test_source_deletion_stays_opt_in(ffmpeg, tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source = _flac(ffmpeg, source_dir / "[01] -  Song.flac")

    retag_flacs([_track(source, name="Song")], tmp_path / "out", configure=False)
    assert source.exists(), "the source was deleted without being asked"


# --------------------------------------------------------------------------- #
# The other writers inherit the check
# --------------------------------------------------------------------------- #
def test_the_encode_path_writes_decodable_flacs(ffmpeg, tmp_path):
    from core.converter import convert_wavs_to_flacs

    wav = tmp_path / "side.wav"
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR))
    sf.write(str(wav), np.column_stack([tone, tone]).astype(np.float32), SR)

    result = convert_wavs_to_flacs([_track(wav, name="Song")], tmp_path / "out")

    assert result.outcomes
    for outcome in result.outcomes:
        assert_written_audio(outcome.output_path, min_frames=SR // 2)


def test_the_mp3_export_writes_decodable_files(ffmpeg, tmp_path):
    from core.mp3_export import export_mp3

    source = _flac(ffmpeg, tmp_path / "[01] - Song.flac")
    out_dir = tmp_path / "mp3"

    result = export_mp3([source], out_dir)

    assert result.outcomes
    for outcome in result.outcomes:
        if outcome.output_path and Path(outcome.output_path).exists():
            # An encode legitimately produces new audio, so decode only.
            assert_written_audio(outcome.output_path)


# --------------------------------------------------------------------------- #
# Silent subprocesses
# --------------------------------------------------------------------------- #
def test_the_launch_helper_asks_for_no_console():
    """No test can observe the flashing -- a console process spawned from a
    console inherits it and shows nothing, and tests always run from one. So the
    guard is that the flag is applied at the one place that knows about it."""
    import sys

    from core import proc

    if sys.platform != "win32":
        assert proc.no_window_kwargs() == {}
        return
    assert proc.no_window_kwargs()["creationflags"] & proc.CREATE_NO_WINDOW


def test_no_module_spawns_a_subprocess_directly():
    """The fix is a class fix: one helper, not one site at a time."""
    import re

    offenders = []
    for path in Path("core").glob("*.py"):
        if path.name == "proc.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bsubprocess\.(run|Popen|call|check_output)\(", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"subprocess launched outside the helper: {offenders}"


def test_the_helper_never_makes_a_launch_louder():
    """A caller's own creationflags survive, OR-ed rather than replaced."""
    import sys

    from core import proc

    if sys.platform != "win32":
        pytest.skip("creationflags are Windows-only")

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return None

    real = proc.subprocess.run
    proc.subprocess.run = fake_run
    try:
        proc.run(["x"], creationflags=0x00000200)      # CREATE_NEW_PROCESS_GROUP
    finally:
        proc.subprocess.run = real

    assert captured["creationflags"] & proc.CREATE_NO_WINDOW
    assert captured["creationflags"] & 0x00000200
