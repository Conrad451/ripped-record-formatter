"""The export family: one path, several destinations, all of them verified.

FLAC is the library; an export is a copy for somewhere the library cannot go.
Each profile is checked the way a third party would check it -- tags read back
with mutagen, the container inspected with ffprobe, and the audio actually
decoded -- because the corruption incident that produced the decode invariant
happened when a writer was verified by reading its tags back and nothing else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core import export_profiles
from core.audio_export import (
    EncoderUnavailable,
    assert_encoder,
    available_encoders,
    build_args,
    export_audio,
)
from tests.audio_invariants import assert_written_audio

SR = 44100


@pytest.fixture(scope="module")
def ffmpeg():
    from core.ffmpeg_locator import configure_pydub, find_ffmpeg

    configure_pydub()
    path, _ = find_ffmpeg()
    if path is None:
        pytest.skip("no ffmpeg available")
    return path


@pytest.fixture(scope="module")
def tagged_flac(ffmpeg, tmp_path_factory):
    """A real FLAC with the full tag set and embedded art."""
    tmp = tmp_path_factory.mktemp("source")
    wav = tmp / "seed.wav"
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR)
    sf.write(str(wav), np.column_stack([tone, tone]).astype(np.float32), SR,
             subtype="PCM_16")
    png = tmp / "cover.png"
    subprocess.run([str(ffmpeg), "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=64x64", "-frames:v", "1", str(png)],
                   capture_output=True)
    flac = tmp / "[01] - So What.flac"
    subprocess.run([
        str(ffmpeg), "-y", "-i", str(wav), "-i", str(png),
        "-map", "0:a", "-map", "1:v", "-disposition:v", "attached_pic",
        "-metadata", "TITLE=So What",
        "-metadata", "ARTIST=Miles Davis",
        "-metadata", "ALBUM=Kind of Blue",
        "-metadata", "ALBUMARTIST=Miles Davis",
        "-metadata", "DATE=1959",
        "-metadata", "TRACKNUMBER=1", "-metadata", "TRACKTOTAL=5",
        "-metadata", "DISCNUMBER=1", "-metadata", "DISCTOTAL=2",
        "-metadata", "MUSICBRAINZ_ALBUMID=rel-1",
        "-metadata", "RRF_VERSION=3.2.0",
        str(flac)], capture_output=True)
    assert flac.exists()
    return flac


def _probe(ffmpeg, path):
    """Container facts, as a third-party reader sees them."""
    ffprobe = Path(ffmpeg).with_name("ffprobe.exe")
    if not ffprobe.exists():
        ffprobe = Path(ffmpeg).with_name("ffprobe")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_format", "-show_streams",
         "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(result.stdout or "{}")


# --------------------------------------------------------------------------- #
# The profile table
# --------------------------------------------------------------------------- #
def test_the_shipped_menu_is_the_trimmed_one():
    """Two formats plus the MP3 family. Navidrome streams FLAC everywhere the
    stakeholder listens, so the lossy menu answered a question nobody asks."""
    assert [p.key for p in export_profiles.PROFILES] == ["alac", "wav", "mp3"]
    assert export_profiles.DEFAULT_PROFILE == "alac"


def test_an_unknown_format_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="Unknown export format"):
        export_profiles.get("flac2000")


def test_an_unknown_quality_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="Unknown MP3 quality"):
        export_profiles.get("mp3").encode_args("V9")


def test_formats_without_variants_need_none():
    assert export_profiles.get("alac").encode_args() == ("-codec:a", "alac", "-vn")
    assert export_profiles.get("wav").default_variant == ""


def test_each_profile_names_its_own_output(tmp_path):
    source = tmp_path / "[01] - Song.flac"
    assert export_profiles.get("alac").output_name(source) == "[01] - Song.m4a"
    assert export_profiles.get("wav").output_name(source) == "[01] - Song.wav"
    assert export_profiles.get("mp3").output_name(source) == "[01] - Song.mp3"


def test_the_argv_is_pure_and_inspectable(tmp_path):
    """A quality setting is assertable without running an encoder."""
    args = build_args("ffmpeg", tmp_path / "a.flac", tmp_path / "a.mp3",
                      export_profiles.get("mp3"), "320")
    assert "-b:a" in args and "320k" in args
    assert args[-1].endswith("a.mp3")


# --------------------------------------------------------------------------- #
# Encoder verification
# --------------------------------------------------------------------------- #
def test_the_bundled_build_has_every_encoder_we_ship(ffmpeg):
    encoders = available_encoders(ffmpeg)
    for profile in export_profiles.PROFILES:
        if profile.encoder:
            assert profile.encoder in encoders, (
                f"{profile.label} needs {profile.encoder}, absent from {ffmpeg}")


def test_the_designed_but_unbuilt_formats_would_also_work(ffmpeg):
    """AAC and Opus are data entry, not a build change -- proven, not assumed."""
    encoders = available_encoders(ffmpeg)
    assert "aac" in encoders
    assert "libopus" in encoders


def test_a_missing_encoder_is_named_not_substituted(ffmpeg):
    """An export that silently changes format is worse than one that refuses."""
    from dataclasses import replace

    impossible = replace(export_profiles.get("alac"), encoder="libnonexistent")
    with pytest.raises(EncoderUnavailable) as raised:
        assert_encoder(ffmpeg, impossible)
    assert "libnonexistent" in str(raised.value)
    assert "ALAC" in str(raised.value)


def test_a_format_needing_no_encoder_is_never_refused(ffmpeg):
    assert_encoder(ffmpeg, export_profiles.get("wav"))      # must not raise


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #
def test_alac_round_trip(ffmpeg, tagged_flac, tmp_path):
    from mutagen.mp4 import MP4

    result = export_audio([tagged_flac], tmp_path, profile="alac", configure=False)
    outcome = result.outcomes[0]
    assert outcome.warnings == []

    written = outcome.output_path
    assert_written_audio(written)

    # Third-party reader: the container really is ALAC in MP4.
    probed = _probe(ffmpeg, written)
    assert any(s.get("codec_name") == "alac" for s in probed.get("streams", []))

    tags = MP4(str(written))
    assert tags["\xa9nam"] == ["So What"]
    assert tags["\xa9ART"] == ["Miles Davis"]
    assert tags["aART"] == ["Miles Davis"]
    assert tags["\xa9alb"] == ["Kind of Blue"]
    assert tags["\xa9day"] == ["1959"]
    assert tags["trkn"] == [(1, 5)]
    assert tags["disk"] == [(1, 2)]
    assert tags["covr"], "cover atom missing"
    assert tags["----:com.apple.iTunes:MusicBrainz Album Id"]


def test_alac_is_actually_lossless(ffmpeg, tagged_flac, tmp_path):
    """It is a copy of the library, not a degradation of it."""
    from tests.audio_invariants import audio_fingerprint

    export_audio([tagged_flac], tmp_path, profile="alac", configure=False)
    written = tmp_path / "[01] - So What.m4a"

    decoded = tmp_path / "check.wav"
    subprocess.run([str(ffmpeg), "-y", "-i", str(written), str(decoded)],
                   capture_output=True)

    assert audio_fingerprint(decoded) == audio_fingerprint(tagged_flac)


def test_wav_round_trip_is_16_44_and_honestly_untagged(ffmpeg, tagged_flac, tmp_path):
    result = export_audio([tagged_flac], tmp_path, profile="wav", configure=False)
    assert result.outcomes[0].warnings == []

    written = result.outcomes[0].output_path
    assert_written_audio(written)

    info = sf.info(str(written))
    assert info.samplerate == 44100
    assert "16" in info.subtype
    # No ceremonial INFO chunks: the profile says so and the caveat says so.
    assert export_profiles.get("wav").tag_strategy == export_profiles.TAG_NONE
    assert "No tags" in export_profiles.get("wav").caveat


def test_mp3_still_works_through_the_refactor(ffmpeg, tagged_flac, tmp_path):
    """The regression that matters: the shipped family keeps shipping."""
    from mutagen.id3 import ID3

    for variant in ("V0", "320", "V2"):
        out = tmp_path / variant
        result = export_audio([tagged_flac], out, profile="mp3",
                              variant=variant, configure=False)
        assert result.outcomes[0].warnings == []
        written = result.outcomes[0].output_path
        assert_written_audio(written)

        tags = ID3(str(written))
        assert tags["TIT2"].text == ["So What"]
        assert tags["TALB"].text == ["Kind of Blue"]
        assert tags.getall("APIC"), "cover art missing"


def test_the_source_is_never_touched(ffmpeg, tagged_flac, tmp_path):
    before = tagged_flac.read_bytes()

    for key in ("alac", "wav", "mp3"):
        export_audio([tagged_flac], tmp_path / key, profile=key, configure=False)

    assert tagged_flac.exists()
    assert tagged_flac.read_bytes() == before


def test_every_shipped_profile_decodes(ffmpeg, tagged_flac, tmp_path):
    """The invariant, applied across the whole menu at once."""
    for profile in export_profiles.PROFILES:
        out = tmp_path / profile.key
        result = export_audio([tagged_flac], out, profile=profile.key,
                              configure=False)
        written = result.outcomes[0].output_path
        assert written.exists(), f"{profile.label} produced nothing"
        assert_written_audio(written)


def test_a_file_that_does_not_decode_is_reported_as_a_failure(ffmpeg, tmp_path):
    """ffmpeg exiting 0 is not the same as having written usable audio."""
    from core.audio_export import verify_output

    fake = tmp_path / "broken.wav"
    fake.write_bytes(b"RIFF____WAVEfmt ")

    assert "does not decode" in verify_output(fake)
