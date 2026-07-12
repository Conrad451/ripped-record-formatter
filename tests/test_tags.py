"""Rich Vorbis-comment propagation through convert -> written FLAC."""

from __future__ import annotations

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from core.converter import convert_wavs_to_flacs
from core.ffmpeg_locator import configure_pydub
from core.tracks import Tracks


def _wav(tmp_path):
    p = tmp_path / "a.wav"
    sf.write(str(p), np.zeros(4410, dtype=np.float32), 44100, subtype="PCM_16")
    return p


def test_vorbis_tags_full_set():
    t = Tracks(3, "Song", "Album", "Artist", "x.wav",
               album_artist="AlbArt", date="1993", track_total=6,
               disc_number=1, disc_total=2, mb_album_id="alb", mb_artist_id="art",
               mb_recording_id="rec", mb_track_id="trk")
    tags = t.vorbis_tags()
    assert tags == {
        "artist": "Artist", "album": "Album", "title": "Song", "tracknumber": "3",
        "albumartist": "AlbArt", "date": "1993", "tracktotal": "6",
        "discnumber": "1", "disctotal": "2",
        "musicbrainz_albumid": "alb", "musicbrainz_artistid": "art",
        "musicbrainz_recordingid": "rec", "musicbrainz_trackid": "trk",
    }


def test_vorbis_tags_minimal_omits_absent():
    t = Tracks(1, "Song", "Album", "Artist", "x.wav")
    assert set(t.vorbis_tags()) == {"artist", "album", "title", "tracknumber"}


def test_vorbis_tags_drops_empty_strings():
    t = Tracks(1, "Song", "", "", "x.wav")     # no album, no artist
    assert set(t.vorbis_tags()) == {"title", "tracknumber"}


def test_full_release_tags_written_verbatim(tmp_path):
    configure_pydub()
    t = Tracks(2, "So What", "Kind of Blue", "Miles Davis", _wav(tmp_path),
               album_artist="Miles Davis", date="1959", track_total=5,
               disc_number=1, disc_total=2, mb_album_id="rel-mbid",
               mb_artist_id="art-mbid", mb_recording_id="rec-mbid", mb_track_id="trk-mbid")
    res = convert_wavs_to_flacs([t], tmp_path / "out", configure=False)
    assert not res.warnings, res.warnings

    f = FLAC(str(res.outcomes[0].output_path))
    assert f["artist"] == ["Miles Davis"]
    assert f["album"] == ["Kind of Blue"]
    assert f["title"] == ["So What"]
    assert f["tracknumber"] == ["2"]
    assert f["albumartist"] == ["Miles Davis"]
    assert f["date"] == ["1959"]
    assert f["tracktotal"] == ["5"]
    assert f["discnumber"] == ["1"]
    assert f["disctotal"] == ["2"]
    assert f["musicbrainz_albumid"] == ["rel-mbid"]
    assert f["musicbrainz_artistid"] == ["art-mbid"]
    assert f["musicbrainz_recordingid"] == ["rec-mbid"]
    assert f["musicbrainz_trackid"] == ["trk-mbid"]


def _two_side_album(tmp_path, *, side_letters=False):
    """A 2-side album: side A has 3 tracks, side B has 2 -- 5 in flat album order.

    TRACKNUMBER is per-side (1,2,3 / 1,2); the *filename* number is album-wide.
    """
    names = ["One", "Two", "Three", "Four", "Five"]
    tracks = []
    flat = 0
    for side_idx, (letter, count) in enumerate([("A", 3), ("B", 2)]):
        for n in range(1, count + 1):
            flat += 1
            tracks.append(Tracks(
                n, names[flat - 1], "Album", "Artist", _wav(tmp_path),
                album_artist="Artist", date="1970",
                track_total=count, disc_number=side_idx + 1, disc_total=2,
                file_index=flat, side_letter=letter, use_side_letters=side_letters,
            ))
    return tracks


def test_album_filenames_are_continuous_across_sides(tmp_path):
    """Flat output: side B carries on numbering where side A stopped, no collisions."""
    tracks = _two_side_album(tmp_path)
    names = [t.filename() for t in tracks]

    assert names == [
        "[01] - One.flac",
        "[02] - Two.flac",
        "[03] - Three.flac",
        "[04] - Four.flac",
        "[05] - Five.flac",
    ]
    assert len(set(names)) == len(names)      # no collisions in a single flat dir


def test_album_filenames_side_letter_variant(tmp_path):
    """filename_side_letters=on: per-side number, disambiguated by the side letter."""
    tracks = _two_side_album(tmp_path, side_letters=True)
    names = [t.filename() for t in tracks]

    assert names == [
        "[A01] - One.flac",
        "[A02] - Two.flac",
        "[A03] - Three.flac",
        "[B01] - Four.flac",
        "[B02] - Five.flac",
    ]
    assert len(set(names)) == len(names)


def test_per_side_tracknumber_survives_flat_filename_change(tmp_path):
    """The filename change is filename-only: tags stay exactly per-side."""
    configure_pydub()
    tracks = _two_side_album(tmp_path)
    out = tmp_path / "out"
    res = convert_wavs_to_flacs(tracks, out, configure=False)
    assert not res.warnings, res.warnings

    # All five landed in ONE flat directory, none overwritten.
    produced = sorted(p.name for p in out.glob("*.flac"))
    assert produced == [
        "[01] - One.flac", "[02] - Two.flac", "[03] - Three.flac",
        "[04] - Four.flac", "[05] - Five.flac",
    ]

    # ...while TRACKNUMBER/TRACKTOTAL remain per-side and DISCNUMBER identifies it.
    got = []
    for outcome in res.outcomes:
        f = FLAC(str(outcome.output_path))
        got.append((f["tracknumber"][0], f["tracktotal"][0], f["discnumber"][0]))
    assert got == [
        ("1", "3", "1"), ("2", "3", "1"), ("3", "3", "1"),   # side A
        ("1", "2", "2"), ("2", "2", "2"),                    # side B
    ]


def test_single_side_filenames_unchanged(tmp_path):
    """Non-album jobs set no file_index and keep the original [NN] behaviour."""
    t = Tracks(2, "So What", "Kind of Blue", "Miles Davis", _wav(tmp_path))
    assert t.filename() == "[02] - So What.flac"


def test_minimal_flow_has_no_empty_fields(tmp_path):
    configure_pydub()
    t = Tracks(1, "Song", "Album", "Artist", _wav(tmp_path))
    res = convert_wavs_to_flacs([t], tmp_path / "out", configure=False)
    f = FLAC(str(res.outcomes[0].output_path))
    keys = {k.lower() for k in f.keys()}
    assert keys == {"artist", "album", "title", "tracknumber"}
    for absent in ("albumartist", "date", "tracktotal", "discnumber",
                   "musicbrainz_albumid", "musicbrainz_artistid",
                   "musicbrainz_recordingid", "musicbrainz_trackid"):
        assert absent not in keys
