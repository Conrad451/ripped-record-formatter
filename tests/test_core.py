"""Smoke tests for the core package.

Covers the four behaviours called out for this phase: filename zero-padding,
config round-trip, the source==destination deletion guard, and a real
end-to-end WAV->FLAC conversion (which doubles as proof that ffmpeg resolution
via core.ffmpeg_locator actually works).
"""

from mutagen.flac import FLAC
from pydub import AudioSegment

from core import config, converter
from core.ffmpeg_locator import configure_pydub
from core.tracks import Tracks


def test_filename_zero_padding():
    assert Tracks(1, "Intro", "Al", "Ar", "x.wav").filename() == "[01] - Intro.flac"
    assert Tracks(9, "Nine", "Al", "Ar", "x.wav").filename() == "[09] - Nine.flac"
    assert Tracks(10, "Ten", "Al", "Ar", "x.wav").filename() == "[10] - Ten.flac"
    assert Tracks(12, "Outro", "Al", "Ar", "x.wav").filename() == "[12] - Outro.flac"
    # __str__ mirrors filename() for CLI back-compat.
    assert str(Tracks(3, "Three", "Al", "Ar", "x.wav")) == "[03] - Three.flac"


def test_config_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    # Missing file -> defaults, no crash.
    assert config.load(path) == config.Config()
    cfg = config.Config(
        source_dir="S", output_dir="O", last_artist="Miles", last_album="KoB"
    )
    config.save(cfg, path)
    assert config.load(path) == cfg


def test_retag_same_path_guard(tmp_path):
    """Re-tagging must never delete a source that resolves to the destination."""
    configure_pydub()
    flac = tmp_path / "[01] - Keep.flac"
    AudioSegment.silent(duration=100).export(str(flac), format="flac")

    # filename() resolves to the same name in the same dir -> source == dest.
    track = Tracks(1, "Keep", "Al", "Ar", flac)
    result = converter.retag_flacs([track], tmp_path, configure=False)

    assert flac.exists(), "guard should have kept the file"
    assert result.outcomes[0].source_deleted is False
    assert result.warnings, "a warning should be recorded when deletion is skipped"


def test_end_to_end_conversion(tmp_path):
    """Generate a tiny WAV, convert it, and verify the tagged FLAC lands.

    Also asserts progress fires once per track (after completion).
    """
    configure_pydub()  # proves ffmpeg resolution works
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    wav = src / "tone.wav"
    AudioSegment.silent(duration=150).export(str(wav), format="wav")

    track = Tracks(1, "Tone", "Album", "Artist", wav)
    calls: list[tuple[int, int, str]] = []
    converter.convert_wavs_to_flacs(
        [track],
        out,
        on_progress=lambda c, t, n: calls.append((c, t, n)),
        configure=False,
    )

    produced = out / "[01] - Tone.flac"
    assert produced.exists() and produced.stat().st_size > 0
    assert calls == [(1, 1, "Tone")]  # progress reported after the track finished
    assert wav.exists()  # conversion leaves the source WAV in place

    tags = FLAC(str(produced))
    assert tags["artist"] == ["Artist"]
    assert tags["album"] == ["Album"]
    assert tags["title"] == ["Tone"]
    assert tags["tracknumber"] == ["1"]
