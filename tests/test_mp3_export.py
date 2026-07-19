"""FLAC->MP3 export: the tag mapping, the art, the quality flag, the error path.

The round-trip tests build a *real* tagged FLAC through the normal converter
path and then export it with the real bundled ffmpeg, exactly as the rest of the
suite does -- the whole point of this module is what survives a genuine encode,
so mocking the encoder would test nothing worth knowing. The two tests that must
not encode (the quality argv and the missing-encoder error) work on the pure
argv builder and a monkeypatched probe instead.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from core import mp3_export
from core.converter import convert_wavs_to_flacs
from core.ffmpeg_locator import configure_pydub
from core.metadata_lookup import CoverArt
from core.tracks import Tracks

SR = 44100
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-cover-bytes" * 4


def _wav(tmp_path, name="a.wav"):
    path = tmp_path / name
    sf.write(str(path), np.zeros(SR // 10, dtype=np.float32), SR, subtype="PCM_16")
    return path


def _full_track(tmp_path) -> Tracks:
    """A track carrying every field the mapping knows how to translate."""
    return Tracks(
        track_num=3,
        track_name="The Third One",
        track_album="Blue Album",
        track_artist="The Artist",
        track_wav_loc=_wav(tmp_path),
        album_artist="Various Artists",
        date="1993",
        track_total=5,
        disc_number=1,
        disc_total=2,
        mb_album_id="album-mbid",
        mb_artist_id="artist-mbid",
        mb_recording_id="recording-mbid",
        mb_track_id="track-mbid",
    )


def _make_flac(tmp_path, track, *, cover=None, restoration_stages=None):
    """Encode ``track`` to a real FLAC and return its path."""
    configure_pydub()
    flac_dir = tmp_path / "flac"
    result = convert_wavs_to_flacs(
        [track], flac_dir, configure=False,
        cover=cover, restoration_stages=restoration_stages,
    )
    assert result.warnings == [], result.warnings
    return result.outcomes[0].output_path


# --- the mapping -------------------------------------------------------------
def test_full_tag_mapping_round_trips(tmp_path):
    """Every mapped Vorbis comment lands in its ID3 frame, with the right value."""
    track = _full_track(tmp_path)
    flac = _make_flac(tmp_path, track, restoration_stages=[])

    out = tmp_path / "mp3"
    result = mp3_export.export_mp3([flac], out, quality=mp3_export.QUALITY_V2)
    assert result.warnings == [], result.warnings
    assert result.total == 1

    mp3 = result.outcomes[0].output_path
    assert mp3.exists() and mp3.suffix == ".mp3"
    tag = ID3(str(mp3))

    assert tag["TIT2"].text == ["The Third One"]
    assert tag["TPE1"].text == ["The Artist"]
    assert tag["TALB"].text == ["Blue Album"]
    assert tag["TPE2"].text == ["Various Artists"]
    assert str(tag["TDRC"].text[0]) == "1993"
    # number and total collapse into one frame
    assert tag["TRCK"].text == ["3/5"]
    assert tag["TPOS"].text == ["1/2"]

    txxx = {frame.desc: frame.text[0] for frame in tag.getall("TXXX")}
    assert txxx["MusicBrainz Album Id"] == "album-mbid"
    assert txxx["MusicBrainz Artist Id"] == "artist-mbid"
    assert txxx["MusicBrainz Recording Id"] == "recording-mbid"
    assert txxx["MusicBrainz Release Track Id"] == "track-mbid"


def test_rrf_provenance_carries_over_as_txxx(tmp_path):
    """The provenance stamp survives the trip to MP3, as user-defined text."""
    from core.version import __version__

    flac = _make_flac(tmp_path, _full_track(tmp_path), restoration_stages=[])
    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    assert result.warnings == [], result.warnings

    tag = ID3(str(result.outcomes[0].output_path))
    txxx = {frame.desc: frame.text[0] for frame in tag.getall("TXXX")}
    assert txxx["RRF_VERSION"] == __version__
    assert txxx["RRF_RESTORATION"] == "none"


def test_written_tag_is_id3v24(tmp_path):
    """The mapping uses TDRC, so the tag it writes must actually be v2.4."""
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    tag = ID3(str(result.outcomes[0].output_path))
    assert tag.version[:2] == (2, 4)


# --- art ---------------------------------------------------------------------
def test_embedded_art_lands_as_apic_front_cover(tmp_path):
    """A FLAC Picture becomes an APIC type-3 frame with the bytes intact."""
    cover = CoverArt(data=_FAKE_PNG, mime="image/png")
    flac = _make_flac(tmp_path, _full_track(tmp_path), cover=cover)

    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    assert result.warnings == [], result.warnings

    tag = ID3(str(result.outcomes[0].output_path))
    apics = tag.getall("APIC")
    assert len(apics) == 1
    assert apics[0].type == 3            # front cover
    assert apics[0].mime == "image/png"
    assert apics[0].data == _FAKE_PNG


def test_no_art_writes_no_apic(tmp_path):
    """A FLAC with no picture produces an MP3 with no APIC frame."""
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    tag = ID3(str(result.outcomes[0].output_path))
    assert tag.getall("APIC") == []


# --- absent writes nothing ---------------------------------------------------
def test_absent_fields_stay_absent(tmp_path):
    """A sparsely-tagged FLAC yields a sparse MP3 -- no empty frames."""
    sparse = Tracks(
        track_num=1,
        track_name="Only Title",
        track_album="Only Album",
        track_artist="Only Artist",
        track_wav_loc=_wav(tmp_path),
    )
    flac = _make_flac(tmp_path, sparse)   # no cover, no restoration stamp

    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    assert result.warnings == [], result.warnings
    tag = ID3(str(result.outcomes[0].output_path))

    present = set(tag.keys())
    # the four we do have
    assert tag["TIT2"].text == ["Only Title"]
    assert tag["TPE1"].text == ["Only Artist"]
    assert tag["TALB"].text == ["Only Album"]
    # a track number with no total is a bare number, not "1/"
    assert tag["TRCK"].text == ["1"]
    # and nothing else: no album artist, date, disc, MBIDs, provenance or art
    assert not any(k.startswith("TPE2") for k in present)
    assert not any(k.startswith("TDRC") for k in present)
    assert not any(k.startswith("TPOS") for k in present)
    assert tag.getall("TXXX") == []
    assert tag.getall("APIC") == []


def test_track_total_without_number_is_dropped():
    """A total with no number is meaningless -- it writes nothing, not '/5'."""
    frames = list(mp3_export._id3_frames({"tracktotal": ["5"]}))
    assert frames == []


def test_alternate_total_spellings_are_read(tmp_path):
    """Files from other taggers spell totals TOTALTRACKS/TOTALDISCS."""
    frames = {
        f.FrameID: f.text[0] for f in mp3_export._id3_frames({
            "tracknumber": ["2"], "totaltracks": ["9"],
            "discnumber": ["1"], "totaldiscs": ["3"],
        })
    }
    assert frames["TRCK"] == "2/9"
    assert frames["TPOS"] == "1/3"


# --- the quality flag --------------------------------------------------------
@pytest.mark.parametrize("quality,expected", [
    (mp3_export.QUALITY_V0, ["-q:a", "0"]),
    (mp3_export.QUALITY_320, ["-b:a", "320k"]),
    (mp3_export.QUALITY_V2, ["-q:a", "2"]),
])
def test_quality_flag_reaches_ffmpeg(quality, expected, tmp_path):
    """The chosen quality becomes the expected ffmpeg arguments, in order."""
    args = mp3_export.encode_args("ffmpeg", tmp_path / "in.flac",
                                  tmp_path / "out.mp3", quality)
    assert "libmp3lame" in args
    assert args[args.index("-codec:a") + 1] == "libmp3lame"
    # the quality flag/value pair appears contiguously
    joined = " ".join(args)
    assert " ".join(expected) in joined


def test_default_quality_is_v0():
    assert mp3_export.DEFAULT_QUALITY == mp3_export.QUALITY_V0


def test_unknown_quality_is_refused(tmp_path):
    """A typo'd quality raises rather than quietly encoding at some default."""
    with pytest.raises(ValueError, match="Unknown MP3 quality"):
        mp3_export.encode_args("ffmpeg", tmp_path / "a.flac",
                               tmp_path / "a.mp3", "V9")


def test_selected_quality_is_actually_used(tmp_path, monkeypatch):
    """The quality passed to export_mp3 is the one handed to the subprocess."""
    seen = []
    real = mp3_export.encode_args

    def spy(ffmpeg, source, dest, quality):
        seen.append(quality)
        return real(ffmpeg, source, dest, quality)

    monkeypatch.setattr(mp3_export, "encode_args", spy)
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    mp3_export.export_mp3([flac], tmp_path / "mp3", quality=mp3_export.QUALITY_320)
    assert seen == [mp3_export.QUALITY_320]


# --- the missing-encoder error ----------------------------------------------
def test_missing_libmp3lame_errors_plainly(tmp_path, monkeypatch):
    """A future bundle without lame fails with a message that says what to do."""
    monkeypatch.setattr(mp3_export, "has_libmp3lame", lambda _ffmpeg: False)

    with pytest.raises(mp3_export.Mp3EncoderUnavailable) as excinfo:
        mp3_export.export_mp3([tmp_path / "nope.flac"], tmp_path / "mp3")

    message = str(excinfo.value)
    assert "libmp3lame" in message
    assert "--enable-libmp3lame" in message
    assert "fetch_ffmpeg" in message


def test_missing_libmp3lame_raises_before_encoding_anything(tmp_path, monkeypatch):
    """The check is up front -- no half-exported folder left behind."""
    monkeypatch.setattr(mp3_export, "has_libmp3lame", lambda _ffmpeg: False)
    out = tmp_path / "mp3"

    with pytest.raises(mp3_export.Mp3EncoderUnavailable):
        mp3_export.export_mp3([tmp_path / "nope.flac"], out)

    assert not out.exists()


def test_bundled_ffmpeg_has_libmp3lame():
    """The ffmpeg this app actually resolves can encode MP3.

    Guards the premise the whole feature rests on: if a future pinned bundle
    drops lame, this fails here rather than in front of a user.
    """
    from core.ffmpeg_locator import ensure_ffmpeg

    ffmpeg, _ = ensure_ffmpeg()
    assert mp3_export.has_libmp3lame(ffmpeg)


# --- batch behaviour ---------------------------------------------------------
def test_sources_are_never_touched(tmp_path):
    """Export is one-directional: the FLAC library is left exactly as it was."""
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    before = flac.read_bytes()

    mp3_export.export_mp3([flac], tmp_path / "mp3")

    assert flac.exists()
    assert flac.read_bytes() == before


def test_progress_and_order_over_a_parallel_batch(tmp_path):
    """Progress counts completions 1..N of N; outcomes keep input order."""
    tracks = [
        Tracks(track_num=i + 1, track_name=f"T{i + 1}", track_album="Alb",
               track_artist="Art", track_wav_loc=_wav(tmp_path, f"s{i}.wav"))
        for i in range(4)
    ]
    flacs = [_make_flac(tmp_path, t) for t in tracks]

    seen = []
    result = mp3_export.export_mp3(
        flacs, tmp_path / "mp3", max_workers=3,
        on_progress=lambda current, total, name: seen.append((current, total, name)),
    )

    assert result.total == 4
    assert [t for _c, t, _n in seen] == [4] * 4
    assert sorted(c for c, _t, _n in seen) == [1, 2, 3, 4]
    assert [o.source.name for o in result.outcomes] == [f.name for f in flacs]


def test_cancel_before_submission_exports_nothing(tmp_path):
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    result = mp3_export.export_mp3(
        [flac], tmp_path / "mp3", should_cancel=lambda: True)
    assert result.total == 0


def test_encode_failure_is_a_warning_not_a_crash(tmp_path):
    """One bad source warns and the batch carries on."""
    good = _make_flac(tmp_path, _full_track(tmp_path))
    bad = tmp_path / "not-audio.flac"
    bad.write_bytes(b"this is not a FLAC")

    result = mp3_export.export_mp3([bad, good], tmp_path / "mp3")

    assert result.total == 2
    assert any("not-audio.flac" in w for w in result.warnings)
    # the good one still made it, fully tagged
    good_out = result.outcomes[1].output_path
    assert good_out.exists()
    assert ID3(str(good_out))["TIT2"].text == ["The Third One"]


def test_output_name_mirrors_the_source(tmp_path):
    assert mp3_export.mp3_name(tmp_path / "[03] - The Third One.flac") == \
        "[03] - The Third One.mp3"


def test_exported_mp3_is_playable_audio(tmp_path):
    """Sanity: the output is a real MP3 with duration, not just a tagged stub."""
    flac = _make_flac(tmp_path, _full_track(tmp_path))
    result = mp3_export.export_mp3([flac], tmp_path / "mp3")
    audio = MP3(str(result.outcomes[0].output_path))
    assert audio.info.length > 0
    assert audio.info.sample_rate == SR
