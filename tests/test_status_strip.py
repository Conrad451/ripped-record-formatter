"""The status strip: one line, one voice, on every tab.

The log pane was a developer console at the bottom of a consumer app. These
cover the replacement -- that it says the right thing per state, that a problem
colours it instead of scrolling past, and that the history is still there.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gui import status_strip
from gui.status_strip import READY, StatusStrip


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_the_strip_rests_at_ready(qapp):
    strip = StatusStrip()
    assert strip.status() == READY
    assert strip.level() == status_strip.INFO


def test_the_strip_states_each_kind_of_work(qapp):
    """The vocabulary: what is happening, to what, and how far in."""
    strip = StatusStrip()
    for line in ("Recording Side C — 2:14, peaks −8.1",
                 "Encoding Side A — 3 of 5 tracks",
                 "Exporting to MP3 — 7 of 12 tracks",
                 "Saved SideC.wav — 19:42"):
        strip.set_status(line)
        assert strip.status() == line
        assert strip.level() == status_strip.INFO


def test_a_warning_flashes_the_strip_and_stays(qapp):
    """A problem must not scroll past into a collapsed console."""
    strip = StatusStrip()
    strip.set_status("Finished with 2 warning(s) — see details", status_strip.WARN)

    assert strip.level() == status_strip.WARN
    assert "warning" in strip.status()
    # It stays put -- nothing clears it but the next status.
    assert strip.status() == "Finished with 2 warning(s) — see details"

    strip.set_status("Could not read the device", status_strip.ERROR)
    assert strip.level() == status_strip.ERROR


def test_an_empty_message_falls_back_to_ready(qapp):
    strip = StatusStrip()
    strip.set_status("")
    assert strip.status() == READY


def test_the_history_toggle_reports_and_reflects(qapp):
    strip = StatusStrip()
    seen = []
    strip.historyToggled.connect(seen.append)

    strip.history_button.setChecked(True)
    assert seen == [True]
    assert "Hide" in strip.history_button.text()

    # Reflecting the pane's state must not re-emit -- that would loop.
    strip.set_history_visible(False)
    assert seen == [True]
    assert "Show" in strip.history_button.text()


# --------------------------------------------------------------------------- #
# In the window: one component, every tab, and the log still there
# --------------------------------------------------------------------------- #
def test_the_log_is_collapsed_by_default_but_never_lost(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qapp.processEvents()

    assert not w.log_visible(), "the console should not greet the user"
    # Nothing is removed from logging -- the history is written either way.
    w._log("a line that happened while collapsed")
    assert "a line that happened while collapsed" in w.log.toPlainText()

    w.set_log_visible(True)
    assert w.log_visible()
    assert "a line that happened while collapsed" in w.log.toPlainText()
    w.close()


def test_the_log_expansion_is_remembered(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    w.set_log_visible(True)
    assert w.settings.config.log_expanded is True
    w.close()

    reopened = MainWindow()
    reopened.show()
    qapp.processEvents()
    assert reopened.log_visible(), "the user's choice did not survive a restart"
    reopened.set_log_visible(False)
    reopened.close()


def test_the_strip_is_the_same_component_on_every_tab(qapp):
    """One story, one voice: the strip belongs to the window, not to a tab.

    Per-tab variants are how an app ends up with five voices, so there is
    exactly one instance and switching tabs does not swap it.
    """
    from gui.main_window import MainWindow

    w = MainWindow()
    w.show()
    qapp.processEvents()

    strip = w.status_strip
    for index in range(w.tabs.count()):
        w.tabs.setCurrentIndex(index)
        qapp.processEvents()
        assert w.status_strip is strip
    w.close()


def test_the_recording_line_names_the_side_the_time_and_the_peak(qapp):
    from core.recorder import Telemetry
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.file_edit.setText("SideC.wav")

    line = tab.recording_status(Telemetry(
        peaks_dbfs=[-8.1, -12.0], max_peak_dbfs=-8.1, elapsed_s=134.0))

    assert line == "Recording SideC — 2:14, peaks −8.1"


def test_the_recording_line_copes_with_silence(qapp):
    from core.recorder import Telemetry
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.file_edit.setText("SideA.wav")

    line = tab.recording_status(Telemetry(
        peaks_dbfs=[float("-inf")], max_peak_dbfs=float("-inf"), elapsed_s=5.0))

    assert line == "Recording SideA — 0:05"     # no peak rather than "-inf"


def test_a_job_error_colours_the_strip(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    w._on_error("ffmpeg is not where we left it")

    assert w.status_strip.level() == status_strip.ERROR
    assert "ffmpeg" in w.status_strip.status()
    # ...and the detail is still in the log for whoever wants it.
    assert "ffmpeg is not where we left it" in w.log.toPlainText()


def test_the_greeting_does_not_name_a_tab_that_no_longer_exists(qapp):
    """The app's first line to the user named the removed Metadata tab, and
    described the app as three tools you pick between rather than one story."""
    from gui.main_window import MainWindow

    w = MainWindow()
    first = w.log.toPlainText()

    assert "Metadata" not in first
    tabs = {w.tabs.tabText(i) for i in range(w.tabs.count())}
    for word in ("Record", "Full Rip"):
        assert word in tabs
    assert "Record" in first          # points at the beginning of the pipeline
    w.close()


def test_the_status_verb_matches_the_button_that_was_pressed(qapp):
    """Press "Convert", get told "Converting" -- not "Encoding".

    A status line that uses a different word for the thing you just clicked
    makes the app sound like it went off and did something else.
    """
    from core import converter, mp3_export
    from gui.main_window import MainWindow

    w = MainWindow()
    for operation, expected in ((converter.convert_wavs_to_flacs, "Converting"),
                                (mp3_export.export_mp3, "Exporting to MP3"),
                                (converter.retag_flacs, "Re-tagging")):
        w._run_job((operation, [], "/tmp/out", {}))
        assert w.status_strip.status().startswith(expected), w.status_strip.status()
    w.close()
