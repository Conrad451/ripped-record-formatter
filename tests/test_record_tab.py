"""Record tab (offscreen): meters, naming auto-advance, and the Full Rip handoff.

No hardware: device enumeration is stubbed and telemetry is fed in directly, so
what is under test is the tab's own behaviour rather than PortAudio's.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.recorder import DeviceInfo, Telemetry
from gui.record_tab import next_side_name

SR = 44100


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    """Stub device enumeration and the monitor stream -- no real audio device."""
    import core.recorder as rec_mod
    import gui.record_tab as tab_mod

    devices = [
        DeviceInfo(index=7, name="Line In (Realtek)", hostapi="Windows WASAPI",
                   samplerate=192000, max_channels=2),
        DeviceInfo(index=2, name="USB Microphone", hostapi="MME",
                   samplerate=44100, max_channels=2),
    ]
    outputs = [
        DeviceInfo(index=5, name="Speakers (Realtek)", hostapi="Windows WASAPI",
                   samplerate=48000, max_channels=2),
        DeviceInfo(index=9, name="Headphones (USB)", hostapi="Windows WASAPI",
                   samplerate=44100, max_channels=2),
    ]
    monkeypatch.setattr(tab_mod, "list_input_devices", lambda: devices)
    monkeypatch.setattr(tab_mod, "list_output_devices", lambda: outputs)
    monkeypatch.setattr(rec_mod.LevelMonitor, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(rec_mod.LevelMonitor, "stop", lambda self: None)
    return devices


def _side_wav(path, seconds=0.2):
    t = np.arange(int(SR * seconds)) / SR
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             SR, subtype="PCM_16")
    return path


# --------------------------------------------------------------------------- #
# Side-aware naming
# --------------------------------------------------------------------------- #
def test_side_name_auto_advances():
    assert next_side_name("SideA.wav") == "SideB.wav"
    assert next_side_name("SideB.wav") == "SideC.wav"
    assert next_side_name("SideC.wav") == "SideD.wav"
    # A prefix is preserved -- this is how a double album stays organised.
    assert next_side_name("InUtero_SideA.wav") == "InUtero_SideB.wav"
    # Case is respected on the way in, normalised on the way out.
    assert next_side_name("sideA.wav") == "sideB.wav"


def test_non_side_names_fall_back_to_a_counter():
    assert next_side_name("take.wav") == "take_2.wav"
    assert next_side_name("take_2.wav") == "take_3.wav"
    assert next_side_name("take_9.wav") == "take_10.wav"


# --------------------------------------------------------------------------- #
# Meters
# --------------------------------------------------------------------------- #
def test_meters_update_from_telemetry(qapp):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab._on_monitor_telemetry(Telemetry(
        peaks_dbfs=[-6.0, -12.0], max_peak_dbfs=-6.0, clip_runs=0,
        elapsed_s=3.0, bytes_written=1024))
    tab._drain_telemetry()

    # The max is stated with the margin it leaves under full scale.
    assert tab.meters.max_label.text() == "max −6.0 dBFS (6.0 dB headroom)"
    assert tab.meters.clip_runs == 0
    assert "no clipping" in tab.meters.clip_label.text()


def test_clip_indicator_latches_and_counts(qapp):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab._on_monitor_telemetry(Telemetry(peaks_dbfs=[0.0, 0.0], max_peak_dbfs=0.0,
                                        clip_runs=3))
    tab._drain_telemetry()

    assert tab.meters.clip_runs == 3
    assert "CLIPPING" in tab.meters.clip_label.text()
    assert "3 run(s)" in tab.meters.clip_label.text()

    # It LATCHES: a subsequent quiet reading must not clear the warning.
    tab._on_monitor_telemetry(Telemetry(peaks_dbfs=[-40.0, -40.0], max_peak_dbfs=0.0,
                                        clip_runs=3))
    tab._drain_telemetry()
    assert "CLIPPING" in tab.meters.clip_label.text()

    # Only Reset clears it.
    tab.meters.reset()
    assert tab.meters.clip_runs == 0
    assert "no clipping" in tab.meters.clip_label.text()


def test_the_history_strip_updates_from_the_same_telemetry(qapp):
    """The strip runs off the monitor feed -- pre-roll, no recording required."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    for i in range(40):                          # 2 s of pre-roll telemetry
        tab._on_monitor_telemetry(Telemetry(
            peaks_dbfs=[-8.0, -11.0], max_peak_dbfs=-8.0, clip_runs=0,
            elapsed_s=i * 0.05))
        tab._drain_telemetry()

    xs, ys = tab.history_strip._traces[0].getData()
    assert len(xs) == 40                         # every snapshot is on the strip
    assert ys[-1] == pytest.approx(-8.0)
    assert tab.history_strip.clip_mark_count == 0


def test_a_clip_run_is_marked_on_the_strip_as_well_as_latched(qapp):
    """The latch says *whether*; the strip says *when*. Both, not either."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab._on_monitor_telemetry(Telemetry(peaks_dbfs=[-20.0, -20.0],
                                        max_peak_dbfs=-20.0, elapsed_s=0.0))
    tab._drain_telemetry()
    tab._on_monitor_telemetry(Telemetry(peaks_dbfs=[0.0, 0.0], max_peak_dbfs=0.0,
                                        clip_runs=1, elapsed_s=1.0))
    tab._drain_telemetry()

    assert tab.history_strip.clip_mark_count == 1     # marked in time...
    assert "CLIPPING" in tab.meters.clip_label.text() # ...and still latched
    assert tab.meters.clip_runs == 1


def test_reset_clears_the_source_not_just_the_label(qapp):
    """A reset that the next telemetry frame undoes is not a reset."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab._on_monitor_telemetry(Telemetry(peaks_dbfs=[-2.0, -2.0], max_peak_dbfs=-2.0,
                                        clip_runs=1, elapsed_s=1.0))
    tab._drain_telemetry()
    assert "2.0 dB headroom" in tab.meters.max_label.text()

    resets = []
    tab._monitor.reset_peaks = lambda: resets.append(True)
    tab.meters.reset_button.click()

    assert resets == [True]                      # the monitor's running max, gone
    assert tab.history_strip.clip_mark_count == 0    # and the strip with it
    assert tab.meters.max_label.text() == "max —"


def test_the_levels_hint_is_legible_and_says_the_useful_thing(qapp):
    """It was dark-on-dark, and it named the wrong number."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    assert "palette(mid)" not in tab.hint.styleSheet()   # normal contrast
    assert "loudest passage" in tab.hint.text()
    assert "−3 dBFS" in tab.hint.text()                  # the number to aim under


def test_device_is_remembered_by_name_not_index(qapp, no_hardware):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(1)              # the USB mic
    assert tab.settings.config.record_device == "USB Microphone"

    # A fresh tab picks it up by name even though the index order differs.
    tab2 = MainWindow().record_tab
    assert tab2.current_device().name == "USB Microphone"


def test_rate_defaults_to_device_native_but_offers_441(qapp):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(0)              # Realtek, native 192000
    assert tab.rate_combo.currentData() == 192000    # defaults to native...
    rates = [tab.rate_combo.itemData(i) for i in range(tab.rate_combo.count())]
    assert 44100 in rates                            # ...but 44.1k is right there

    tab.rate_combo.setCurrentIndex(tab.rate_combo.findData(44100))
    assert tab.settings.config.record_samplerate == 44100   # and it is persisted


# --------------------------------------------------------------------------- #
# The payoff: record -> rip handoff
# --------------------------------------------------------------------------- #
def test_recorded_side_lands_in_the_full_rip_mapping_table(qapp, tmp_path):
    """Record side A, flip, record side B -- and the album job is mapped."""
    from core.metadata_lookup import MediumInfo, ReleaseDetail, TrackInfo
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip

    # Full Rip is working from the folder the Record tab writes into.
    release = ReleaseDetail("x", "In Utero", "Nirvana", media=(
        MediumInfo(1, "Vinyl", tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"A{i + 1}", 180000) for i in range(3))),
        MediumInfo(2, "Vinyl", tracks=tuple(
            TrackInfo(i + 1, str(i + 1), f"B{i + 1}", 180000) for i in range(3))),
    ))
    fr._apply_release(release)

    side_a = _side_wav(tmp_path / "SideA.wav")
    fr._album_wavs = [side_a]
    fr._rebuild_mapping_table()
    assert fr.mapping_table.rowCount() == 1
    assert fr._album_mapping == [0]                  # SideA -> Side A

    # Now side B is recorded into the same folder.
    side_b = _side_wav(tmp_path / "SideB.wav")
    adopted = fr.add_recorded_wav(side_b)

    assert adopted is True
    assert fr.mapping_table.rowCount() == 2          # it appeared on its own
    names = [fr.mapping_table.item(r, 0).text() for r in range(2)]
    assert names == ["SideA.wav", "SideB.wav"]
    assert fr._album_mapping == [0, 1]               # ...and mapped to Side B
    # The whole album is now mapped without the user touching the table.


def test_a_recording_into_another_folder_is_not_adopted(qapp, tmp_path):
    """A capture that has nothing to do with the loaded album is left alone."""
    from gui.main_window import MainWindow

    fr = MainWindow().full_rip
    here = tmp_path / "album"
    here.mkdir()
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()

    fr._album_wavs = [_side_wav(here / "SideA.wav")]
    fr._rebuild_mapping_table()

    stray = _side_wav(elsewhere / "SideB.wav")
    assert fr.add_recorded_wav(stray) is False
    assert fr.mapping_table.rowCount() == 1          # untouched


def test_the_window_wires_the_handoff_end_to_end(qapp, tmp_path):
    """recordingFinished -> Full Rip mapping table, through MainWindow."""
    from core.recorder import RecordingResult
    from gui.main_window import MainWindow

    w = MainWindow()
    fr = w.full_rip
    fr._album_wavs = [_side_wav(tmp_path / "SideA.wav")]
    fr._rebuild_mapping_table()

    side_b = _side_wav(tmp_path / "SideB.wav")
    result = RecordingResult(path=side_b, duration=1.0, samplerate=44100,
                             subtype="PCM_16", max_peak_dbfs=-3.0, clip_runs=0)
    w.record_tab.recordingFinished.emit(result)      # as a real stop would
    qapp.processEvents()

    assert fr.mapping_table.rowCount() == 2
    assert "added to the Full Rip mapping" in w.log.toPlainText()


# --------------------------------------------------------------------------- #
# Recording state is unmissable
# --------------------------------------------------------------------------- #
def test_recording_state_is_unmissable(qapp):
    from gui.main_window import MainWindow

    w = MainWindow()
    index = w.tabs.indexOf(w.record_tab)
    assert w.tabs.tabText(index) == "Record"

    w.record_tab.recordingStateChanged.emit(True)
    qapp.processEvents()
    assert w.tabs.tabText(index) == "● Record"
    assert "RECORDING" in w.windowTitle()
    assert "#c0392b" in w.styleSheet()                # red border on the pane

    w.record_tab.recordingStateChanged.emit(False)
    qapp.processEvents()
    assert w.tabs.tabText(index) == "Record"
    assert "RECORDING" not in w.windowTitle()
    assert w.styleSheet() == ""


def test_refuses_to_overwrite_an_existing_side(qapp, tmp_path):
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    existing = _side_wav(tmp_path / "SideA.wav")
    tab.folder_edit.setText(str(tmp_path))
    tab.file_edit.setText("SideA.wav")

    tab._start_recording()

    assert not tab.recording
    assert "already exists" in w.log.toPlainText()
    assert existing.read_bytes()                     # untouched


# --------------------------------------------------------------------------- #
# Software monitoring (passthrough): toggle, feedback guard, failure isolation
# --------------------------------------------------------------------------- #
class _FakePassthrough:
    """Stands in for core.recorder.Passthrough -- no real audio streams."""

    def __init__(self):
        self.running = False
        self.error = ""
        self.latency_s = 0.05
        self.started_with = None

    def start(self, in_dev, out_dev, rate, channels):
        self.started_with = (in_dev, out_dev, rate, channels)
        self.running = True

    def stop(self):
        self.running = False


def _idx_by_name(combo, name):
    for i in range(combo.count()):
        if name in combo.itemText(i):
            return i
    return 0


def test_monitor_toggle_starts_passthrough_and_persists(qapp, no_hardware):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.set_active(True)
    tab._passthrough = _FakePassthrough()

    # Distinct input and output (no feedback), then switch monitoring on.
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.monitor_combo.setCurrentIndex(_idx_by_name(tab.monitor_combo, "Speakers"))
    tab.monitor_check.setChecked(True)

    assert tab.settings.config.monitor_enabled is True
    assert tab.settings.config.monitor_device == "Speakers (Realtek)"
    assert tab._passthrough.running
    assert tab.monitor_indicator.isVisibleTo(tab)


def test_monitor_refuses_the_same_endpoint(qapp, monkeypatch, no_hardware):
    import gui.record_tab as tab_mod
    from gui.main_window import MainWindow

    # An output whose NAME matches an input -> monitoring it would feed back.
    same = [DeviceInfo(index=7, name="Line In (Realtek)", hostapi="Windows WASAPI",
                       samplerate=192000, max_channels=2)]
    monkeypatch.setattr(tab_mod, "list_output_devices", lambda: same)

    tab = MainWindow().record_tab
    tab.set_active(True)
    tab._passthrough = _FakePassthrough()
    logged: list[str] = []
    tab.logMessage.connect(logged.append)

    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.monitor_combo.setCurrentIndex(0)             # "Line In (Realtek)" output
    tab.monitor_check.setChecked(True)

    assert not tab.monitor_check.isChecked()         # the guard reset the toggle
    assert not tab._passthrough.running
    assert any("feed back" in m or "same device" in m for m in logged)


def test_monitor_output_vanishing_resets_the_toggle(qapp, no_hardware):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.set_active(True)
    tab._passthrough = _FakePassthrough()
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.monitor_combo.setCurrentIndex(_idx_by_name(tab.monitor_combo, "Speakers"))
    tab.monitor_check.setChecked(True)
    assert tab._passthrough.running

    # The output device vanishes: the passthrough sets .error on its audio thread.
    tab._passthrough.error = "PortAudioError: device unavailable"
    logged: list[str] = []
    tab.logMessage.connect(logged.append)
    tab._drain_telemetry()                           # the health check notices it

    assert not tab.monitor_check.isChecked()         # toggle reset, cleanly
    assert not tab._passthrough.running
    assert any("monitoring stopped" in m for m in logged)
