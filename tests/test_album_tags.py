"""Seam test: the album path must tag and embed cover art exactly like the
single-side path.

This is the regression the stakeholder hit -- album-mode FLACs came out with
default/missing tags and no picture. It drives the *real* wiring end to end:
a mocked release -> two synthetic side WAVs -> FullRipTab -> AlbumController ->
_album_analyze -> _album_encode -> converter, then reads the written FLACs back
with mutagen and asserts the complete Vorbis field set plus the embedded front
cover, per track.

Restoration stages are switched off in config -- this test is about metadata
threading, not DSP, and the stages cost seconds per side.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from core.album import SideState
from core.metadata_lookup import CoverArt, MediumInfo, ReleaseDetail, TrackInfo

SR = 44100

# A minimal but real JPEG header so mutagen stores a sane picture.
COVER_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 200
COVER = CoverArt(data=COVER_BYTES, mime="image/jpeg")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _side_wav(path, n_tracks=2, tone_s=3.0, gap_s=1.5):
    """A side: `n_tracks` tones separated by near-silence on a noise floor."""
    rng = np.random.RandomState(7)
    pieces = []
    for i in range(n_tracks):
        t = np.arange(int(tone_s * SR)) / SR
        pieces.append(0.35 * np.sin(2 * np.pi * (220 + 110 * i) * t))
        if i < n_tracks - 1:
            pieces.append(np.zeros(int(gap_s * SR)))
    sig = np.concatenate(pieces)
    sig = sig + rng.normal(0.0, 10 ** (-55 / 20), sig.size)   # vinyl floor
    sf.write(str(path), sig.astype(np.float32), SR, subtype="PCM_16")
    return path


def _release():
    """Two sides, two tracks each, every MB id populated -- the full 5.99 set."""
    def track(pos, title, rec, trk):
        return TrackInfo(position=pos, number=str(pos), title=title,
                         length_ms=3000, artist="", artist_id="",
                         recording_id=rec, track_mbid=trk)

    return ReleaseDetail(
        release_id="rel-mbid", title="Kind of Blue", artist="Miles Davis",
        year="1959", country="US", artist_id="art-mbid", cover=COVER,
        media=(
            MediumInfo(1, "Vinyl", tracks=(
                track(1, "So What", "rec-a1", "trk-a1"),
                track(2, "Freddie Freeloader", "rec-a2", "trk-a2"))),
            MediumInfo(2, "Vinyl", tracks=(
                track(1, "Blue in Green", "rec-b1", "trk-b1"),
                track(2, "Flamenco Sketches", "rec-b2", "trk-b2"))),
        ),
    )


def _wait(predicate, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _run_album(qapp, tmp_path, edit=None):
    """Drive a full two-side album job and return the output dir."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip

    cfg = fr.settings.config
    cfg.rumble_enabled = cfg.hum_enabled = False
    cfg.noise_enabled = cfg.declick_enabled = False    # metadata test, not DSP
    cfg.min_silence = 0.5
    cfg.min_track_length = 1.0
    # Settings load from the real user config file, so pin anything this test
    # asserts on rather than inheriting whatever the developer has set.
    cfg.filename_side_letters = False
    cfg.encode_workers = 2

    fr._apply_release(_release())

    src = tmp_path / "src"
    src.mkdir()
    a = _side_wav(src / "SideA.wav")
    b = _side_wav(src / "SideB.wav")
    out = tmp_path / "out"

    fr.output_edit.setText(str(out))
    fr._album_wavs = [a, b]
    fr._rebuild_mapping_table()
    assert fr._album_mapping == [0, 1], fr._album_mapping

    fr._start_album()
    assert fr._album is not None

    # Review + accept every side as it becomes ready, through the real GUI path:
    # load it into the review area, then Accept -- which snapshots the table and
    # enqueues the encode itself. `edit` may rewrite the table first.
    accepted = set()

    def pump():
        for side in fr._album.sides:
            if side.state == SideState.READY and side.index not in accepted:
                fr._load_side_for_review(side)
                if edit is not None:
                    edit(fr, side)
                fr._accept_album_side()
                accepted.add(side.index)
        return all(s.state in (SideState.DONE, SideState.ERROR, SideState.CANCELLED)
                   for s in fr._album.sides)

    assert _wait(pump), [(s.label, s.state, s.error) for s in fr._album.sides]
    errors = [(s.label, s.error) for s in fr._album.sides if s.state == SideState.ERROR]
    assert not errors, errors
    return out


def test_album_path_writes_full_tags_and_cover(qapp, tmp_path):
    out = _run_album(qapp, tmp_path)

    # Flat output, continuous filenames, all four tracks present.
    produced = sorted(p.name for p in out.glob("*.flac"))
    assert produced == [
        "[01] - So What.flac",
        "[02] - Freddie Freeloader.flac",
        "[03] - Blue in Green.flac",
        "[04] - Flamenco Sketches.flac",
    ], produced

    expected = {
        "[01] - So What.flac": dict(
            title="So What", tracknumber="1", tracktotal="2", discnumber="1",
            recording="rec-a1", track="trk-a1"),
        "[02] - Freddie Freeloader.flac": dict(
            title="Freddie Freeloader", tracknumber="2", tracktotal="2", discnumber="1",
            recording="rec-a2", track="trk-a2"),
        "[03] - Blue in Green.flac": dict(
            title="Blue in Green", tracknumber="1", tracktotal="2", discnumber="2",
            recording="rec-b1", track="trk-b1"),
        "[04] - Flamenco Sketches.flac": dict(
            title="Flamenco Sketches", tracknumber="2", tracktotal="2", discnumber="2",
            recording="rec-b2", track="trk-b2"),
    }

    for name, want in expected.items():
        f = FLAC(str(out / name))

        # --- the complete 5.97/5.99 Vorbis field set ---
        assert f["title"] == [want["title"]], name
        assert f["artist"] == ["Miles Davis"], name
        assert f["album"] == ["Kind of Blue"], name
        assert f["albumartist"] == ["Miles Davis"], name
        assert f["date"] == ["1959"], name
        assert f["tracknumber"] == [want["tracknumber"]], name
        assert f["tracktotal"] == [want["tracktotal"]], name
        assert f["discnumber"] == [want["discnumber"]], name
        assert f["disctotal"] == ["2"], name
        assert f["musicbrainz_albumid"] == ["rel-mbid"], name
        assert f["musicbrainz_artistid"] == ["art-mbid"], name
        assert f["musicbrainz_recordingid"] == [want["recording"]], name
        assert f["musicbrainz_trackid"] == [want["track"]], name

        # --- embedded front cover ---
        assert f.pictures, f"{name}: no embedded picture"
        pic = f.pictures[0]
        assert pic.type == 3, name          # front cover
        assert pic.mime == "image/jpeg", name
        assert pic.data == COVER_BYTES, name


def test_album_encode_warnings_are_surfaced(qapp, tmp_path, monkeypatch):
    """A failed cover embed / tag write must be reported, not swallowed.

    The album path used to discard the BatchResult that convert_wavs_to_flacs
    returns. Those warnings are per-track and never fail the batch, so throwing
    the result away meant a tag or cover failure disappeared without a trace --
    which is precisely how a tagging problem presents as "album mode just does
    not tag". The single-side path has always logged them via _on_encode_done.
    """
    import core.converter as C

    def boom(flac_path, cover):
        raise OSError("simulated cover failure")

    monkeypatch.setattr(C, "_embed_cover", boom)

    from gui.main_window import MainWindow
    w = MainWindow()
    fr = w.full_rip
    cfg = fr.settings.config
    cfg.rumble_enabled = cfg.hum_enabled = False
    cfg.noise_enabled = cfg.declick_enabled = False
    cfg.min_silence = 0.5
    cfg.min_track_length = 1.0
    cfg.filename_side_letters = False

    fr._apply_release(_release())
    src = tmp_path / "src"; src.mkdir()
    a = _side_wav(src / "SideA.wav")
    fr.output_edit.setText(str(tmp_path / "out"))
    fr._album_wavs = [a]
    fr._rebuild_mapping_table()
    fr._start_album()

    def pump():
        for side in fr._album.sides:
            if side.state == SideState.READY:
                cuts = [p.timestamp for p in side.analysis.proposal.split_points]
                fr._album.accept_side(side.index, cuts, list(side.titles))
        return all(s.state in (SideState.DONE, SideState.ERROR, SideState.CANCELLED)
                   for s in fr._album.sides)

    assert _wait(pump), [(s.label, s.state, s.error) for s in fr._album.sides]
    qapp.processEvents()

    log = w.log.toPlainText()
    assert "Could not embed cover art" in log, log[-800:]
    assert "simulated cover failure" in log


def test_reviewer_edits_are_snapshotted_into_the_written_tags(qapp, tmp_path):
    """Titles/artists as the reviewer left them at Accept are what get written.

    Accept snapshots the table onto the SideJob and enqueues the encode in one
    step, so the edits must survive the hand-off even though the review area is
    released immediately afterwards.
    """
    def edit(fr, side):
        rows = fr.model.rows()
        if side.index == 0:                       # rewrite side A only
            rows[0].title = "So What (alt take)"
            rows[1].artist = "Miles Davis Sextet"
            fr.model.set_rows(rows)

    out = _run_album(qapp, tmp_path, edit=edit)

    produced = sorted(p.name for p in out.glob("*.flac"))
    assert "[01] - So What (alt take).flac" in produced, produced

    edited = FLAC(str(out / "[01] - So What (alt take).flac"))
    assert edited["title"] == ["So What (alt take)"]
    assert edited["tracknumber"] == ["1"]         # per-side numbering untouched
    assert edited.pictures                        # cover still embedded

    guest = FLAC(str(out / "[02] - Freddie Freeloader.flac"))
    assert guest["artist"] == ["Miles Davis Sextet"]   # per-track artist edit landed
    assert guest["albumartist"] == ["Miles Davis"]     # ...without touching ALBUMARTIST

    # Side B, untouched, still carries the release's own values.
    b = FLAC(str(out / "[03] - Blue in Green.flac"))
    assert b["title"] == ["Blue in Green"]
    assert b["artist"] == ["Miles Davis"]
    assert b["discnumber"] == ["2"]
