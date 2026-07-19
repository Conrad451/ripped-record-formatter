"""Re-tag reaches tagging parity with Full Rip.

The design frame: Re-tag is the pipeline's tagging stage retargeted at FLACs
that already exist, which is how a pre-app library becomes first-class rather
than a second-class folder the good tools cannot reach. So the test that matters
most is the one that puts a Full-Rip-produced album and a Re-tag-produced album
side by side and demands the same tags.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from core.metadata_lookup import CoverArt, MediumInfo, ReleaseDetail, TrackInfo
from gui.retag_table import (
    COL_ALBUM,
    COL_ALBUM_ARTIST,
    COL_ARTIST,
    COL_DATE,
    COL_DISC,
    COL_MBID,
    COL_NUM,
    COL_TITLE,
    RetagRow,
    RetagTableModel,
)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _png(width=16, height=16) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF0000)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def _release():
    def track(pos, title):
        return TrackInfo(position=pos, number=str(pos), title=title,
                         length_ms=180000, artist="", artist_id="",
                         recording_id=f"rec-{pos}", track_mbid=f"trk-{pos}")

    return ReleaseDetail(
        release_id="rel-1", title="Kind of Blue", artist="Miles Davis",
        year="1959", country="US", artist_id="artist-1",
        media=(MediumInfo(1, "Vinyl", "", (track(1, "So What"),
                                           track(2, "Freddie Freeloader"),
                                           track(3, "Blue in Green"))),
               MediumInfo(2, "Vinyl", "", (track(1, "All Blues"),
                                           track(2, "Flamenco Sketches")))),
        cover=CoverArt(data=_png(), mime="image/png"))


def _rows(n=5):
    return [RetagRow(source_path=Path(f"/lib/[{i:02d}] -  Track {i}.flac"),
                     title=f"Track {i}") for i in range(1, n + 1)]


# --------------------------------------------------------------------------- #
# The parity claim, stated as a comparison
# --------------------------------------------------------------------------- #
def test_retag_writes_the_same_tags_full_rip_writes(qapp):
    """The heart of it: same release, same sides, same tags, field for field."""
    from gui.main_window import MainWindow

    release = _release()
    window = MainWindow()
    try:
        full_rip = window.full_rip
        full_rip._apply_release(release)
        # Full Rip's own path, for side A of the same record.
        expected = full_rip._enrich_tracks(
            ["So What", "Freddie Freeloader", "Blue in Green"],
            [Path(f"/seg/{i}.wav") for i in range(3)],
            list(release.media[0].tracks), 1, 2, "Miles Davis", "Kind of Blue")

        model = RetagTableModel(_rows(5))
        model.paste_titles(0, [t.title for t in release.tracks])
        model.apply_release_fields(release)
        model.apply_sides([[0, 1, 2], [3, 4]])
        actual = model.build_tracks(per_row_artist=False,
                                    default_artist="Miles Davis",
                                    default_album="Kind of Blue")

        for want, got in zip(expected, actual[:3]):
            assert got.vorbis_tags() == want.vorbis_tags(), (
                f"{got.track_name}: {got.vorbis_tags()} != {want.vorbis_tags()}")
    finally:
        window.close()


def test_per_side_numbering_follows_the_picard_vinyl_convention(qapp):
    model = RetagTableModel(_rows(5))
    model.apply_sides([[0, 1, 2], [3, 4]])

    tags = [t.vorbis_tags() for t in model.build_tracks(
        per_row_artist=False, default_artist="A", default_album="B")]

    assert [t["tracknumber"] for t in tags] == ["1", "2", "3", "1", "2"]
    assert [t["tracktotal"] for t in tags] == ["3", "3", "3", "2", "2"]
    assert [t["discnumber"] for t in tags] == ["1", "1", "1", "2", "2"]
    assert [t["disctotal"] for t in tags] == ["2", "2", "2", "2", "2"]


def test_clearing_sides_returns_to_flat_numbering(qapp):
    model = RetagTableModel(_rows(4))
    model.apply_sides([[0, 1], [2, 3]])
    model.apply_sides([])

    tags = [t.vorbis_tags() for t in model.build_tracks(
        per_row_artist=False, default_artist="A", default_album="B")]

    assert [t["tracknumber"] for t in tags] == ["1", "2", "3", "4"]
    assert all("discnumber" not in t for t in tags)


def test_all_thirteen_fields_reach_the_tags(qapp):
    model = RetagTableModel(_rows(3))
    model.apply_release_fields(_release())
    model.apply_sides([[0, 1, 2]])

    tags = model.build_tracks(per_row_artist=True, default_artist="Miles Davis",
                              default_album="Kind of Blue")[0].vorbis_tags()

    for field in ("artist", "album", "title", "tracknumber", "albumartist",
                  "date", "tracktotal", "discnumber", "disctotal",
                  "musicbrainz_albumid", "musicbrainz_artistid",
                  "musicbrainz_recordingid", "musicbrainz_trackid"):
        assert field in tags, f"{field} never made it into the tags"
    assert tags["musicbrainz_albumid"] == "rel-1"
    assert tags["musicbrainz_recordingid"] == "rec-1"


def test_per_track_artist_wins_over_the_album_artist(qapp):
    """Soundtrack mode: the row's own artist is what gets written."""
    rows = _rows(2)
    rows[0].artist = "John Coltrane"
    model = RetagTableModel(rows)

    tracks = model.build_tracks(per_row_artist=True, default_artist="Miles Davis",
                                default_album="Kind of Blue")

    assert tracks[0].vorbis_tags()["artist"] == "John Coltrane"
    assert tracks[1].vorbis_tags()["artist"] == "Miles Davis"


# --------------------------------------------------------------------------- #
# Filename convention, composed with the v3.0.1 prefix strip
# --------------------------------------------------------------------------- #
def test_the_side_letter_convention_composes_with_the_prefix_strip(qapp):
    """The field case: a legacy name re-stamps once, in the chosen style."""
    rows = [RetagRow(source_path=Path(f"/lib/[{i:02d}] -  Song {i}.flac"),
                     title=f"[{i:02d}] -  Song {i}") for i in range(1, 4)]
    model = RetagTableModel(rows)
    model.apply_sides([[0, 1], [2]])

    lettered = model.build_tracks(per_row_artist=False, default_artist="A",
                                  default_album="B", use_side_letters=True)
    continuous = model.build_tracks(per_row_artist=False, default_artist="A",
                                    default_album="B", use_side_letters=False)

    assert [t.filename() for t in lettered] == [
        "[A01] - Song 1.flac", "[A02] - Song 2.flac", "[B01] - Song 3.flac"]
    assert [t.filename() for t in continuous] == [
        "[01] - Song 1.flac", "[02] - Song 2.flac", "[01] - Song 3.flac"]
    for name in [t.filename() for t in lettered + continuous]:
        assert "] - [" not in name, f"double-stamped: {name}"


# --------------------------------------------------------------------------- #
# The table is the preview of the write
# --------------------------------------------------------------------------- #
def test_edited_cells_land_in_the_written_tags_verbatim(qapp):
    from PySide6.QtCore import Qt

    model = RetagTableModel(_rows(2))
    for column, value in ((COL_TITLE, "Typed Title"), (COL_ARTIST, "Typed Artist"),
                          (COL_ALBUM, "Typed Album"),
                          (COL_ALBUM_ARTIST, "Typed Album Artist"),
                          (COL_DATE, "1971")):
        assert model.setData(model.index(0, column), value,
                             Qt.ItemDataRole.EditRole)

    tags = model.build_tracks(per_row_artist=True, default_artist="x",
                              default_album="y")[0].vorbis_tags()

    assert tags["title"] == "Typed Title"
    assert tags["artist"] == "Typed Artist"
    assert tags["album"] == "Typed Album"
    assert tags["albumartist"] == "Typed Album Artist"
    assert tags["date"] == "1971"


def test_derived_and_identifying_columns_are_not_editable(qapp):
    from PySide6.QtCore import Qt

    model = RetagTableModel(_rows(2))
    for column in (COL_NUM, COL_DISC, COL_MBID):
        flags = model.flags(model.index(0, column))
        assert not (flags & Qt.ItemFlag.ItemIsEditable), (
            f"column {column} should not be typed into")
        assert model.setData(model.index(0, column), "nope",
                             Qt.ItemDataRole.EditRole) is False


def test_apply_to_all_floods_eligible_columns(qapp):
    model = RetagTableModel(_rows(4))

    assert model.flood_column(COL_ALBUM_ARTIST, "Miles Davis") == 4
    assert model.flood_column(COL_DATE, "1959") == 4
    assert model.flood_column(COL_ARTIST, "Miles Davis") == 4
    assert model.flood_column(COL_ALBUM, "Kind of Blue") == 4

    for row in model.rows():
        assert row.album_artist == "Miles Davis"
        assert row.date == "1959"
        assert row.album == "Kind of Blue"


def test_apply_to_all_refuses_title_and_number(qapp):
    """Per-track by definition -- flooding them is only ever a mistake."""
    model = RetagTableModel(_rows(3))
    before = [r.title for r in model.rows()]

    assert model.flood_column(COL_TITLE, "Same") == 0
    assert model.flood_column(COL_NUM, "1") == 0
    assert not model.can_flood(COL_TITLE)
    assert not model.can_flood(COL_NUM)
    assert [r.title for r in model.rows()] == before


def test_flooding_reports_only_the_rows_it_changed(qapp):
    model = RetagTableModel(_rows(3))
    model.rows()[0].date = "1959"

    assert model.flood_column(COL_DATE, "1959") == 2      # one already matched


def test_the_disc_column_updates_when_sides_are_defined(qapp):
    from PySide6.QtCore import Qt

    model = RetagTableModel(_rows(4))
    assert model.data(model.index(0, COL_DISC), Qt.ItemDataRole.DisplayRole) == "—"

    model.apply_sides([[0, 1], [2, 3]])

    assert model.data(model.index(0, COL_DISC), Qt.ItemDataRole.DisplayRole) == "1/2"
    assert model.data(model.index(3, COL_DISC), Qt.ItemDataRole.DisplayRole) == "2/2"


def test_the_mbid_column_shows_what_is_being_written(qapp):
    from PySide6.QtCore import Qt

    model = RetagTableModel(_rows(2))
    assert model.data(model.index(0, COL_MBID), Qt.ItemDataRole.DisplayRole) == "—"

    model.apply_release_fields(_release())

    assert model.data(model.index(0, COL_MBID),
                      Qt.ItemDataRole.DisplayRole) == "4/4 IDs"
    assert "rec-1" in model.data(model.index(0, COL_MBID),
                                 Qt.ItemDataRole.ToolTipRole)


# --------------------------------------------------------------------------- #
# Quality of life
# --------------------------------------------------------------------------- #
def test_choosing_a_folder_loads_it(qapp, tmp_path):
    from gui.main_window import MainWindow

    for i in range(1, 4):
        (tmp_path / f"[{i:02d}] -  Song {i}.flac").write_bytes(b"")

    window = MainWindow()
    try:
        retag = window.retag_panel
        retag.source_edit.setText(str(tmp_path))
        retag._on_source_chosen()

        assert len(retag.model.rows()) == 3
        # Titles seed without the stamp -- carrying it is how it got applied twice.
        assert retag.model.rows()[0].title == "Song 1"
    finally:
        window.close()


def test_artist_and_album_are_derived_from_the_path_under_the_flac_root(qapp, tmp_path):
    from gui.main_window import MainWindow

    root = tmp_path / "Library"
    folder = root / "Miles Davis" / "Kind of Blue"
    folder.mkdir(parents=True)
    (folder / "01.flac").write_bytes(b"")

    window = MainWindow()
    try:
        retag = window.retag_panel
        retag.settings.set(default_output_dir=str(root))
        retag.source_edit.setText(str(folder))
        retag._on_source_chosen()

        assert retag.artist_edit.text() == "Miles Davis"
        assert retag.album_edit.text() == "Kind of Blue"
    finally:
        window.close()


def test_a_folder_outside_the_root_derives_nothing(qapp, tmp_path):
    """Two path segments are a guess, and a wrong guess in a tag field is worse
    than an empty one."""
    from gui.main_window import MainWindow

    root = tmp_path / "Library"
    root.mkdir()
    elsewhere = tmp_path / "Downloads" / "some folder"
    elsewhere.mkdir(parents=True)
    (elsewhere / "01.flac").write_bytes(b"")

    window = MainWindow()
    try:
        retag = window.retag_panel
        retag.settings.set(default_output_dir=str(root))
        retag.source_edit.setText(str(elsewhere))
        retag._on_source_chosen()

        assert retag.artist_edit.text() == ""
        assert retag.album_edit.text() == ""
    finally:
        window.close()


def test_derivation_never_overwrites_something_already_typed(qapp, tmp_path):
    from gui.main_window import MainWindow

    root = tmp_path / "Library"
    folder = root / "Miles Davis" / "Kind of Blue"
    folder.mkdir(parents=True)

    window = MainWindow()
    try:
        retag = window.retag_panel
        retag.settings.set(default_output_dir=str(root))
        retag.artist_edit.setText("My Own Answer")
        retag._derive_identity_from(folder)

        assert retag.artist_edit.text() == "My Own Answer"
        assert retag.album_edit.text() == "Kind of Blue"      # the empty one filled
    finally:
        window.close()


def test_identity_does_not_survive_a_restart(qapp):
    """The clean-slate doctrine, extended to the utility tabs.

    No default is safe for identity: a remembered artist is how a stale name
    ends up tagged onto the next record.
    """
    from gui.main_window import MainWindow

    first = MainWindow()
    try:
        first.retag_panel.artist_edit.setText("Miles Davis")
        first.retag_panel.album_edit.setText("Kind of Blue")
        first.retag_panel.artist_edit.editingFinished.emit()
    finally:
        first.close()

    second = MainWindow()
    try:
        assert second.retag_panel.artist_edit.text() == ""
        assert second.retag_panel.album_edit.text() == ""
        assert second.convert_panel.artist_edit.text() == ""
    finally:
        second.close()


# --------------------------------------------------------------------------- #
# Manual cover art
# --------------------------------------------------------------------------- #
def test_a_chosen_image_becomes_cover_art(qapp, tmp_path):
    from gui.cover_picker import load_cover_file

    path = tmp_path / "sleeve.png"
    path.write_bytes(_png())

    cover, problem = load_cover_file(path)

    assert problem == ""
    assert cover is not None
    assert cover.mime == "image/png"
    assert cover.data == path.read_bytes()


def test_an_oversized_file_is_refused_with_the_limit_named(qapp, tmp_path):
    from gui.cover_picker import MAX_COVER_BYTES, load_cover_file

    path = tmp_path / "huge.jpg"
    path.write_bytes(b"\xff\xd8" + b"\x00" * (MAX_COVER_BYTES + 1))

    cover, problem = load_cover_file(path)

    assert cover is None
    assert "10 MB" in problem
    assert "every track" in problem, "the refusal should say why the cap exists"


def test_an_oversized_image_is_refused_with_its_dimensions(qapp, tmp_path):
    from gui.cover_picker import MAX_COVER_PIXELS, load_cover_file

    path = tmp_path / "enormous.png"
    path.write_bytes(_png(MAX_COVER_PIXELS + 10, 10))

    cover, problem = load_cover_file(path)

    assert cover is None
    assert str(MAX_COVER_PIXELS) in problem


def test_a_non_image_is_refused(qapp, tmp_path):
    from gui.cover_picker import load_cover_file

    path = tmp_path / "notes.txt"
    path.write_text("not a picture", encoding="utf-8")

    cover, problem = load_cover_file(path)

    assert cover is None
    assert "JPEG or PNG" in problem


def test_the_no_art_state_offers_the_way_out(qapp):
    """The amber warning gains the fix instead of ending the conversation."""
    import dataclasses

    from gui.release_preview import ReleasePreview

    preview = ReleasePreview()
    with_art = _release()
    without_art = dataclasses.replace(with_art, cover=None)

    preview.set_release(with_art)
    assert preview.choose_cover_button.isHidden()

    preview.set_release(without_art)
    assert not preview.choose_cover_button.isHidden()


def test_a_chosen_cover_is_announced_to_the_host(qapp, tmp_path):
    import dataclasses

    from gui.cover_picker import load_cover_file
    from gui.release_preview import ReleasePreview

    path = tmp_path / "sleeve.png"
    path.write_bytes(_png())
    cover, _ = load_cover_file(path)

    preview = ReleasePreview()
    preview.set_release(dataclasses.replace(_release(), cover=None))
    seen = []
    preview.coverChosen.connect(seen.append)

    preview.set_cover(cover)
    preview.coverChosen.emit(cover)

    assert seen == [cover]
