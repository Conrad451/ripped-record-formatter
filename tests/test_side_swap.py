"""Replacing one side of an album that already exists on disk.

The story: an 18-track, 8-side 45rpm deluxe pressing of Bewitched, ripped by
pre-app scripts in 2024. Tracks 13 and 14 came out garbled and share a side. The
alternative to this feature is re-recording all eight sides.

The design commitment under test is the ordering: encode, verify the new files
decode, and only then touch the old ones. No failure may leave the album worse
than it started.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core import album_folder, side_swap

SR = 44100

#: The real pressing, as it sits on disk.
BEWITCHED = [
    "Dreamer", "Second Best", "Haunted", "Must Be Love",
    "While You Were Sleeping", "Lovesick", "California and Me", "Nocturne",
    "Promise", "From the Start", "Misty", "Serendipity",
    "Letter to My 13 Year Old Self ERROR", "Bewitched", "Bored", "Trouble",
    "It Could Happen to You", "Goddess",
]


@pytest.fixture(scope="module")
def ffmpeg():
    from core.ffmpeg_locator import configure_pydub, find_ffmpeg

    configure_pydub()
    path, _ = find_ffmpeg()
    if path is None:
        pytest.skip("no ffmpeg available")
    return path


def _flac(ffmpeg, path: Path, seconds=0.25) -> Path:
    wav = path.with_suffix(".seed.wav")
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(int(SR * seconds)) / SR)
    sf.write(str(wav), np.column_stack([tone, tone]).astype(np.float32), SR,
             subtype="PCM_16")
    subprocess.run([str(ffmpeg), "-y", "-i", str(wav), str(path)],
                   capture_output=True)
    wav.unlink(missing_ok=True)
    return path


@pytest.fixture
def album(ffmpeg, tmp_path):
    """A synthetic Bewitched: conforming folder, 18 numbered tracks."""
    folder = tmp_path / "Laufey" / "Bewitched- The Goddess Edition"
    folder.mkdir(parents=True)
    for position, title in enumerate(BEWITCHED, start=1):
        _flac(ffmpeg, folder / f"[{position:02d}] - {title}.flac")
    return folder


# --------------------------------------------------------------------------- #
# Reading a folder the app could have written
# --------------------------------------------------------------------------- #
def test_a_conforming_folder_reads_as_an_album(album):
    read = album_folder.read(album)

    assert read.conforms
    assert read.count == 18
    assert read.at(13).title == "Letter to My 13 Year Old Self ERROR"
    assert read.at(14).title == "Bewitched"


def test_provenance_is_irrelevant(album):
    """The output format is the contract -- no journal, no db, no native flag."""
    read = album_folder.read(album)

    assert read.conforms
    assert [t.position for t in read.tracks] == list(range(1, 19))


def test_the_side_being_replaced_is_found_by_position(album):
    read = album_folder.read(album)

    side = read.at_positions([13, 14])

    assert [t.name for t in side] == [
        "[13] - Letter to My 13 Year Old Self ERROR.flac",
        "[14] - Bewitched.flac"]


@pytest.mark.parametrize("filename", [
    "Letter to My 13 Year Old Self.flac",      # no number at all
    "13 - Letter.flac",                        # number, but not the app's form
    "[A01] - Letter.flac",                     # per-side, not album position
])
def test_a_non_conforming_folder_is_redirected_not_failed(ffmpeg, tmp_path, filename):
    folder = tmp_path / "mixed"
    folder.mkdir()
    _flac(ffmpeg, folder / "[01] - Fine.flac")
    _flac(ffmpeg, folder / filename)

    read = album_folder.read(folder)

    assert not read.conforms
    assert "Re-tag it first" in read.problem


def test_duplicate_numbers_are_refused(ffmpeg, tmp_path):
    """A number that does not identify one file cannot drive a replacement."""
    folder = tmp_path / "dupes"
    folder.mkdir()
    _flac(ffmpeg, folder / "[01] - One.flac")
    _flac(ffmpeg, folder / "[01] - One Again.flac")

    read = album_folder.read(folder)

    assert not read.conforms
    assert "share track number" in read.problem


def test_a_gap_in_the_numbering_is_refused(ffmpeg, tmp_path):
    folder = tmp_path / "gappy"
    folder.mkdir()
    for position in (1, 2, 4):
        _flac(ffmpeg, folder / f"[{position:02d}] - Track.flac")

    read = album_folder.read(folder)

    assert not read.conforms
    assert "not continuous" in read.problem
    assert "missing 3" in read.problem


def test_an_empty_or_missing_folder_says_so(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no FLACs" in album_folder.read(empty).problem
    assert "not a folder" in album_folder.read(tmp_path / "absent").problem


# --------------------------------------------------------------------------- #
# The plan, and the consent it asks for
# --------------------------------------------------------------------------- #
def test_the_plan_names_the_exact_files(album):
    planned = side_swap.plan(album, [13, 14])

    assert [p.name for p in planned.condemned] == [
        "[13] - Letter to My 13 Year Old Self ERROR.flac",
        "[14] - Bewitched.flac"]
    summary = planned.summary()
    assert "Letter to My 13 Year Old Self ERROR" in summary
    assert "Bewitched" in summary
    # Never a bare count: the risk of this operation is replacing the wrong side.
    assert "2 files" not in summary


def test_the_plan_identifies_by_position_not_by_name(album, ffmpeg):
    """The new side may be named differently -- a corrected title, a dropped
    ERROR suffix -- so matching on name would miss or mis-hit."""
    planned = side_swap.plan(album, [13])

    assert [p.name for p in planned.condemned] == [
        "[13] - Letter to My 13 Year Old Self ERROR.flac"]


def test_a_plan_for_positions_that_are_not_there_condemns_nothing(album):
    planned = side_swap.plan(album, [97, 98])

    assert planned.describes_nothing
    assert "Nothing would be replaced" in planned.summary()


# --------------------------------------------------------------------------- #
# The swap
# --------------------------------------------------------------------------- #
def _replace_side(ffmpeg, album, positions, titles, *, archive=False):
    """The real ordering: plan, encode, verify, retire.

    The plan is made *before* anything is written, which is not an incidental
    detail -- once the new files are on disk the folder momentarily holds two
    tracks at the same position, and a plan computed then would read the folder
    as non-conforming and condemn nothing. It is also the order the user
    experiences: the confirmation names the files before the work starts.
    """
    planned = side_swap.plan(album, positions, archive=archive)
    written = []
    for position, title in zip(positions, titles):
        written.append(_flac(ffmpeg, album / f"[{position:02d}] - {title}.flac"))
    problems = side_swap.verify_encoded(written)
    assert problems == [], problems
    return written, side_swap.retire(planned, keep=set(written))


def test_replacing_a_side_leaves_every_other_file_byte_untouched(ffmpeg, album):
    before = {p.name: p.read_bytes() for p in album.glob("*.flac")}

    _replace_side(ffmpeg, album, [13, 14],
                  ["Letter to My 13 Year Old Self", "Bewitched"])

    after = {p.name: p.read_bytes() for p in album.glob("*.flac")}
    for position in list(range(1, 13)) + list(range(15, 19)):
        name = f"[{position:02d}] - {BEWITCHED[position - 1]}.flac"
        assert name in after, f"{name} disappeared"
        assert after[name] == before[name], f"{name} was modified"


def test_the_replaced_files_are_gone_and_the_new_ones_are_there(ffmpeg, album):
    _replace_side(ffmpeg, album, [13, 14],
                  ["Letter to My 13 Year Old Self", "Bewitched"])

    names = sorted(p.name for p in album.glob("*.flac"))
    assert "[13] - Letter to My 13 Year Old Self ERROR.flac" not in names
    assert "[13] - Letter to My 13 Year Old Self.flac" in names
    assert "[14] - Bewitched.flac" in names
    assert len(names) == 18, "the album gained or lost a track"


def test_a_replacement_landing_on_the_same_name_is_not_deleted(ffmpeg, album):
    """Track 14's title is unchanged, so the new file overwrites the old path.
    That file is now the *new* one and must survive the retirement."""
    _written, result = _replace_side(ffmpeg, album, [14], ["Bewitched"])

    assert (album / "[14] - Bewitched.flac").exists()
    assert result.removed == []          # nothing to retire; it was overwritten
    assert result.ok


def test_archiving_keeps_the_originals_out_of_the_way(ffmpeg, album):
    _replace_side(ffmpeg, album, [13], ["Letter to My 13 Year Old Self"],
                  archive=True)

    archive = album / side_swap.ARCHIVE_DIRNAME
    assert archive.is_dir()
    assert (archive / "[13] - Letter to My 13 Year Old Self ERROR.flac").exists()
    # ...and out of the album proper, so the folder still holds exactly 18.
    assert len(list(album.glob("*.flac"))) == 18


def test_deleting_is_the_default(ffmpeg, album):
    _written, result = _replace_side(
        ffmpeg, album, [13], ["Letter to My 13 Year Old Self"])

    assert result.removed, "nothing was removed"
    assert not (album / side_swap.ARCHIVE_DIRNAME).exists()


# --------------------------------------------------------------------------- #
# Failure leaves the album exactly as it was
# --------------------------------------------------------------------------- #
def test_a_new_file_that_does_not_decode_stops_the_swap(ffmpeg, album):
    """The invariant at the one moment it decides whether anything is deleted."""
    before = sorted(p.name for p in album.glob("*.flac"))

    broken = album / "[13] - Letter to My 13 Year Old Self.flac"
    broken.write_bytes(b"fLaC not really")

    problems = side_swap.verify_encoded([broken])

    assert problems, "a corrupt replacement was accepted"
    # The caller stops here -- and the original is still present.
    assert (album / "[13] - Letter to My 13 Year Old Self ERROR.flac").exists()
    assert sorted(p.name for p in album.glob("*.flac")) == sorted(
        before + ["[13] - Letter to My 13 Year Old Self.flac"])


def test_a_missing_encode_is_reported_rather_than_assumed(album):
    problems = side_swap.verify_encoded([album / "never-written.flac"])

    assert problems and "was not written" in problems[0]


def test_a_file_that_cannot_be_removed_is_a_warning_not_a_disaster(ffmpeg, album,
                                                                   monkeypatch):
    """The new side is already written and correct; a stuck old file is untidy,
    not lost work."""
    planned = side_swap.plan(album, [13])

    def refuse(self):
        raise OSError("file in use")

    monkeypatch.setattr(Path, "unlink", refuse)
    result = side_swap.retire(planned)

    assert not result.ok
    assert result.warnings and "Could not remove" in result.warnings[0]


# --------------------------------------------------------------------------- #
# The entry point, in the tab
# --------------------------------------------------------------------------- #
def test_the_entry_point_sits_with_the_other_sources(qapp_gui):
    """It answers "what am I working on", like the other two -- not a mode."""
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.full_rip.replace_side_btn is not None
        assert "Replace a side" in window.full_rip.replace_side_btn.text()
    finally:
        window.close()


def test_choosing_a_conforming_album_arms_the_replacement(qapp_gui, album,
                                                          monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        full_rip = window.full_rip
        logged: list[str] = []
        full_rip.logMessage.connect(logged.append)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(album)))

        full_rip._begin_replace_side()

        assert full_rip._replace_album is not None
        assert full_rip._replace_album.count == 18
        assert any("18 tracks" in m for m in logged), logged
    finally:
        window.close()


def test_a_non_conforming_album_gets_the_redirect(qapp_gui, ffmpeg, tmp_path,
                                                  monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from gui.main_window import MainWindow

    folder = tmp_path / "legacy"
    folder.mkdir()
    _flac(ffmpeg, folder / "Some Song.flac")

    window = MainWindow()
    try:
        full_rip = window.full_rip
        logged: list[str] = []
        full_rip.logMessage.connect(logged.append)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(folder)))

        full_rip._begin_replace_side()

        assert full_rip._replace_album is None
        assert any("Re-tag it first" in m for m in logged), logged
    finally:
        window.close()


def test_side_positions_come_from_the_defined_sides_not_the_release(qapp_gui):
    """MusicBrainz shape is advisory: an 8-side 45rpm deluxe pressing is very
    often catalogued as a 2-disc CD, and the object on the turntable wins."""
    from core.side_partition import Side
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        full_rip = window.full_rip
        # 18 tracks cut as an 8-side 45, which no CD-shaped release describes.
        sides = [Side(index=0, track_indices=(0, 1), total_ms=0),
                 Side(index=1, track_indices=(2, 3), total_ms=0),
                 Side(index=6, track_indices=(12, 13), total_ms=0)]
        full_rip._sides = sides

        assert full_rip.replace_side_positions(0) == [1, 2]
        assert full_rip.replace_side_positions(2) == [13, 14]   # the garbled side
        assert full_rip.replace_side_positions(99) == []
    finally:
        window.close()
