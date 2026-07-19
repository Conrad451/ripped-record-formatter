"""The Convert tab's MP3 export section: derivation, job assembly, and wiring."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from core import audio_export, mp3_export
from gui.mp3_export import Mp3ExportSection, derived_output_dir


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class _Settings:
    """Minimal stand-in for gui.main_window.Settings."""

    def __init__(self, **fields):
        from core.config import Config

        self.config = Config(**fields)

    def set(self, **fields):
        for key, value in fields.items():
            setattr(self.config, key, value)


def _section(qapp, tmp_path, *, root="", artist="", album="", recent=""):
    return Mp3ExportSection(
        _Settings(encode_workers=2),
        output_root=lambda: root,
        metadata=lambda: (artist, album),
        recent_album_dir=lambda: recent,
    )


def _flac_folder(tmp_path, name="flacs", count=2):
    folder = tmp_path / name
    folder.mkdir()
    for i in range(count):
        (folder / f"[0{i + 1}] - Track {i + 1}.flac").write_bytes(b"fLaC-stub")
    return folder


# --- the derived destination -------------------------------------------------
def test_derived_output_dir_shape():
    derived = derived_output_dir(r"D:\Music", "Miles Davis", "Kind of Blue")
    assert Path(derived) == Path(r"D:\Music") / "MP3" / "Miles Davis" / "Kind of Blue"


def test_derived_output_dir_sanitizes_components():
    """A title with path characters must not escape into the folder structure."""
    derived = derived_output_dir(r"D:\Music", "AC/DC", "Back: In Black?")
    assert derived is not None
    parts = Path(derived).parts
    assert "AC DC" in parts
    assert "Back In Black" in parts


@pytest.mark.parametrize("root,artist,album", [
    ("", "Artist", "Album"),          # no root
    (r"D:\Music", "", "Album"),       # no artist
    (r"D:\Music", "Artist", ""),      # no album
    (r"D:\Music", "  ", "Album"),     # whitespace-only artist
    (r"D:\Music", "Artist", "///"),   # sanitizes away to nothing
])
def test_derivation_declines_rather_than_guessing(root, artist, album):
    assert derived_output_dir(root, artist, album) is None


def test_suggest_fills_the_output_field(qapp, tmp_path):
    section = _section(qapp, tmp_path, root=r"D:\Music",
                       artist="Miles Davis", album="Kind of Blue")
    section.suggest_output()
    assert Path(section.output_edit.text()) == \
        Path(r"D:\Music") / "ALAC" / "Miles Davis" / "Kind of Blue"


def test_suggest_says_so_when_not_derivable(qapp, tmp_path):
    section = _section(qapp, tmp_path, root=r"D:\Music", artist="Miles", album="")
    messages = []
    section.logMessage.connect(messages.append)
    section.suggest_output()
    assert section.output_edit.text() == ""
    assert any("Cannot suggest" in m for m in messages)


def test_offered_output_never_overwrites_a_typed_path(qapp, tmp_path):
    section = _section(qapp, tmp_path, root=r"D:\Music",
                       artist="Miles Davis", album="Kind of Blue")
    section.output_edit.setText(r"E:\somewhere\i\chose")
    section._offer_output()
    assert section.output_edit.text() == r"E:\somewhere\i\chose"


# --- the just-finished-album shortcut ----------------------------------------
def test_use_recent_album_fills_the_source(qapp, tmp_path):
    album = _flac_folder(tmp_path, "finished-album")
    section = _section(qapp, tmp_path, recent=str(album))
    section.use_recent_album()
    assert Path(section.source_edit.text()) == album


def test_use_recent_album_says_so_when_there_is_none(qapp, tmp_path):
    section = _section(qapp, tmp_path, recent="")
    messages = []
    section.logMessage.connect(messages.append)
    section.use_recent_album()
    assert section.source_edit.text() == ""
    assert any("No finished album" in m for m in messages)


# --- job assembly ------------------------------------------------------------
def test_collect_job_is_the_standard_four_tuple(qapp, tmp_path):
    folder = _flac_folder(tmp_path)
    section = _section(qapp, tmp_path)
    section.source_edit.setText(str(folder))
    section.output_edit.setText(str(tmp_path / "out"))

    operation, flacs, output_dir, kwargs = section.collect_job()

    assert operation is audio_export.export_audio
    assert [f.name for f in flacs] == [
        "[01] - Track 1.flac", "[02] - Track 2.flac"]
    assert output_dir == tmp_path / "out"
    # The job now names a profile; ALAC is the default and has no variant.
    assert kwargs == {"profile": "alac", "variant": "", "max_workers": 2}


def test_default_quality_is_v0_and_the_combo_carries_the_constant(qapp, tmp_path):
    section = _section(qapp, tmp_path)
    section.format_combo.setCurrentIndex(section.format_combo.findData("mp3"))
    assert section.quality() == mp3_export.QUALITY_V0
    values = [section.quality_combo.itemData(i)
              for i in range(section.quality_combo.count())]
    assert values == [mp3_export.QUALITY_V0, mp3_export.QUALITY_320,
                      mp3_export.QUALITY_V2]


def test_chosen_quality_reaches_the_job(qapp, tmp_path):
    folder = _flac_folder(tmp_path)
    section = _section(qapp, tmp_path)
    section.source_edit.setText(str(folder))
    section.output_edit.setText(str(tmp_path / "out"))
    # Quality belongs to the format, so pick MP3 first -- the regression this
    # guards is that the MP3 family still carries its variant through.
    section.format_combo.setCurrentIndex(section.format_combo.findData("mp3"))
    section.quality_combo.setCurrentIndex(1)      # 320 CBR

    _op, _flacs, _out, kwargs = section.collect_job()
    assert kwargs["profile"] == "mp3"
    assert kwargs["variant"] == mp3_export.QUALITY_320


def test_no_flacs_is_refused_with_a_reason(qapp, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    section = _section(qapp, tmp_path)
    section.source_edit.setText(str(empty))
    section.output_edit.setText(str(tmp_path / "out"))

    messages = []
    section.logMessage.connect(messages.append)
    assert section.collect_job() is None
    assert any("No FLACs found" in m for m in messages)


def test_missing_output_is_refused_with_a_reason(qapp, tmp_path):
    section = _section(qapp, tmp_path)
    section.source_edit.setText(str(_flac_folder(tmp_path)))

    messages = []
    section.logMessage.connect(messages.append)
    assert section.collect_job() is None
    assert any("Choose a folder for the MP3s" in m for m in messages)


def test_exporting_into_the_flac_folder_is_refused(qapp, tmp_path):
    """MP3s must not land among the library they were copied from."""
    folder = _flac_folder(tmp_path)
    section = _section(qapp, tmp_path)
    section.source_edit.setText(str(folder))
    section.output_edit.setText(str(folder))

    messages = []
    section.logMessage.connect(messages.append)
    assert section.collect_job() is None
    assert any("must not be the FLAC folder" in m for m in messages)


def test_running_disables_the_export_button(qapp, tmp_path):
    section = _section(qapp, tmp_path)
    section.set_running(True)
    assert not section.export_button.isEnabled()
    section.set_running(False)
    assert section.export_button.isEnabled()


# --- wiring into the window --------------------------------------------------
def test_convert_tab_has_the_section_and_retag_does_not(qapp):
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        assert isinstance(window.convert_panel.mp3_section, Mp3ExportSection)
        assert window.retag_panel.mp3_section is None
        # and it is parented into the Convert tab, not floating
        assert window.convert_panel.mp3_section.parent() is window.convert_panel
    finally:
        window.close()


def test_export_runs_through_the_same_worker_plumbing(qapp, tmp_path, monkeypatch):
    """Pressing Export starts the job via the window's normal job runner."""
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        started = []
        monkeypatch.setattr(window, "_run_job", started.append)

        folder = _flac_folder(tmp_path)
        section = window.convert_panel.mp3_section
        section.source_edit.setText(str(folder))
        section.output_edit.setText(str(tmp_path / "out"))
        section.export_button.click()

        assert len(started) == 1
        operation, flacs, output_dir, kwargs = started[0]
        assert operation is audio_export.export_audio
        assert len(flacs) == 2
        assert output_dir == tmp_path / "out"
        assert kwargs["profile"] == "alac"
    finally:
        window.close()


def test_a_running_job_disables_the_export_button(qapp):
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        window.convert_panel.set_running(True)
        assert not window.convert_panel.mp3_section.export_button.isEnabled()
        window.convert_panel.set_running(False)
        assert window.convert_panel.mp3_section.export_button.isEnabled()
    finally:
        window.close()
