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
    # Line In (48k device from the field report) supports only 48k/192k; the USB
    # mic supports 44.1/48. No real hardware probe.
    supported = {7: {48000, 192000}, 2: {44100, 48000}}

    def fake_rates(device, channels, candidates, **kw):
        ok = supported.get(device, set(candidates))
        return [r for r in candidates if r in ok]

    monkeypatch.setattr(tab_mod, "list_input_devices", lambda: devices)
    monkeypatch.setattr(tab_mod, "list_output_devices", lambda: outputs)
    monkeypatch.setattr(tab_mod, "supported_input_rates", fake_rates)
    monkeypatch.setattr(rec_mod.LevelMonitor, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(rec_mod.LevelMonitor, "stop", lambda self: None)
    # No COM audio endpoint by default -- the gain slider stays hidden. The
    # dedicated gain test overrides this with a fake endpoint.
    monkeypatch.setattr(tab_mod.EndpointGain, "for_device",
                        classmethod(lambda cls, name, **kw: None))
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


def test_rate_picker_recommends_native_and_marks_unsupported(qapp):
    """The picker tells the truth: native is recommended; a rate WASAPI can't open
    (44.1k on this 48k/192k device) is marked, not silently offered."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(0)              # Line In, native 192000
    assert tab.rate_combo.currentData() == 192000    # defaults to native

    labels = [tab.rate_combo.itemText(i) for i in range(tab.rate_combo.count())]
    native = next(t for t in labels if t.startswith("192000"))
    assert "device native — recommended" in native
    unsupported = next(t for t in labels if t.startswith("44100"))
    assert "needs a Windows Sound change" in unsupported   # 44.1k won't open here
    supported = next(t for t in labels if t.startswith("48000"))
    assert "needs a Windows Sound change" not in supported  # 48k does open


def test_a_remembered_rate_that_no_longer_opens_falls_back_to_native(qapp):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.settings.set(record_samplerate=44100)         # saved against another device
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab._on_device_changed()
    assert tab.rate_combo.currentData() == 192000     # 44.1k can't open here


def test_an_unopenable_rate_is_refused_in_plain_words_not_a_portaudio_error(
        qapp, no_hardware, monkeypatch, tmp_path):
    """Picking a rate WASAPI won't grant must produce one explanatory line -- never
    a raw PortAudioError -9997 in the user's face."""
    import sounddevice as sd

    import core.recorder as rec_mod
    from gui.main_window import MainWindow

    def explode(self, *a, **kw):
        raise sd.PortAudioError(
            "Error opening InputStream: Invalid sample rate [PaErrorCode -9997]")

    monkeypatch.setattr(rec_mod.Recorder, "start", explode)

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.folder_edit.setText(str(tmp_path))
    logged: list[str] = []
    monkeypatch.setattr(tab, "_log", logged.append)

    tab._start_recording()

    assert logged, "the failure must say something"
    line = logged[-1]
    assert "can't record at" in line                  # plain words...
    assert "192000 Hz" in line                        # ...naming the rate to pick
    assert "44,100 Hz regardless" in line             # ...and reassuring about output
    assert "PaErrorCode" not in line and "-9997" not in line   # never the raw error
    assert not tab.recording


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
    assert "already here" in w.log.toPlainText()     # plain-language overwrite refusal
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


# --------------------------------------------------------------------------- #
# "Check my setup" (9.5)
# --------------------------------------------------------------------------- #
def test_setup_check_reassures_about_a_non_44100_device(qapp, no_hardware):
    """The rewritten check: a 48k/192k device is FINE -- reassure that the FLACs
    resample to 44.1k, rather than offering the broken 'Use 44100' stream fix."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.settings.set(output_sample_rate="44100")
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))  # 192000
    tab._run_setup_check()

    text = tab.check_results.text()
    assert "device is set to 192000" in text     # (apostrophe in the HTML is escaped)
    assert "saved at 44,100 Hz automatically" in text
    assert not hasattr(tab, "rate_fix_button")   # the broken one-click fix is gone
    assert tab.record_button.isEnabled()         # advice, never a gate


def test_setup_check_listen_reports_no_signal(qapp, no_hardware):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    # Drive the finish directly with an accumulated silent window.
    tab._pending_check = []
    tab._checking = True
    tab._check_peak = -95.0
    tab._check_clips = 0
    tab._finish_setup_check()
    assert "No signal detected" in tab.check_results.text()


def test_setup_check_listen_reports_too_hot(qapp, no_hardware):
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab._pending_check = []
    tab._checking = True
    tab._check_peak = -1.0
    tab._check_clips = 3
    tab._finish_setup_check()
    assert "much too hot" in tab.check_results.text()


def test_setup_check_never_gates_recording(qapp, no_hardware, tmp_path):
    """Advice, not a wall: a flagged setup does not disable Record."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab._run_setup_check()                        # flags 192k
    assert tab.record_button.isEnabled()          # still recordable


def test_monitor_and_setup_check_coexist_on_the_tab(qapp, no_hardware):
    """v2.3.1 merge seam: the monitor toggle (9.4) and Check my setup (9.5) both
    render on the Record tab, neither displaced by the other."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    # Monitoring controls (feature/monitor).
    assert tab.monitor_check.isVisibleTo(tab)
    assert tab.monitor_combo.isVisibleTo(tab)
    # Setup-check controls (feature/first-run).
    assert tab.check_button.isVisibleTo(tab)
    assert tab.check_results is not None
    assert tab.monitor_hint.isVisibleTo(tab)     # the hardware-jack hint, kept in the merge


# --------------------------------------------------------------------------- #
# Input-gain slider (9.9 part 3)
# --------------------------------------------------------------------------- #
class _FakeEndpoint:
    def __init__(self, level=0.5):
        self.level = level

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, value, _ctx):
        self.level = value


def test_gain_slider_round_trips_a_mocked_endpoint(qapp, no_hardware, monkeypatch):
    """The slider reflects the endpoint's level, drives it on move, and persists
    the setting per device name."""
    import gui.record_tab as tab_mod
    from core.input_gain import EndpointGain
    from gui.main_window import MainWindow

    ep = _FakeEndpoint(0.5)
    monkeypatch.setattr(tab_mod.EndpointGain, "for_device",
                        classmethod(lambda cls, name, **kw: EndpointGain(ep)))

    tab = MainWindow().record_tab
    tab.settings.set(record_input_levels={})      # no saved level: read the endpoint
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "USB Microphone"))
    tab._on_device_changed()

    assert tab.gain_slider.isVisibleTo(tab)
    assert tab.gain_slider.value() == 50          # reads the endpoint's current level

    tab.gain_slider.setValue(80)                  # move it
    assert abs(ep.level - 0.80) < 1e-9            # drove the Windows level
    name = tab.current_device().name
    assert abs(tab.settings.config.record_input_levels[name] - 0.80) < 1e-9  # remembered


def test_gain_slider_restores_a_remembered_level(qapp, no_hardware, monkeypatch):
    """A device with a saved level pushes it back to Windows on selection."""
    import gui.record_tab as tab_mod
    from core.input_gain import EndpointGain
    from gui.main_window import MainWindow

    ep = _FakeEndpoint(0.2)
    monkeypatch.setattr(tab_mod.EndpointGain, "for_device",
                        classmethod(lambda cls, name, **kw: EndpointGain(ep)))

    tab = MainWindow().record_tab
    tab.settings.set(record_input_levels={"USB Microphone": 0.9})
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "USB Microphone"))
    tab._on_device_changed()   # explicit: the combo may already sit on this device

    assert tab.gain_slider.value() == 90          # slider shows the saved value
    assert abs(ep.level - 0.90) < 1e-9            # and it was pushed to the endpoint


def test_gain_slider_hidden_when_endpoint_inaccessible(qapp, no_hardware):
    """No reachable endpoint (the autouse stub returns None) -> slider hidden,
    no crash."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "USB Microphone"))
    tab._on_device_changed()
    assert not tab.gain_slider.isVisibleTo(tab)
    assert tab._gain is None


# --------------------------------------------------------------------------- #
# The monitor is a session feature, not a tab feature (9.14 Part 1f)
# --------------------------------------------------------------------------- #
def _monitoring_window(qapp):
    """A window with monitoring on, driven through the real toggle path."""
    from gui.main_window import MainWindow

    w = MainWindow()
    tab = w.record_tab
    # Genuinely *active* on the Record tab first, or "switch away" later proves
    # nothing. Since the pipeline reorder Record is already the current tab, so
    # setCurrentWidget would be a no-op and never fire activation -- showing the
    # window is what activates the landing tab now.
    w.show()
    qapp.processEvents()
    assert tab._active, "the fixture must start active on the Record tab"
    tab._passthrough = _FakePassthrough()
    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.monitor_combo.setCurrentIndex(_idx_by_name(tab.monitor_combo, "Speakers"))
    # monitor_enabled persists, and the config is shared across the session --
    # so if an earlier test left it on, setChecked(True) is a no-op that never
    # fires toggled and the passthrough is never started. Force the transition.
    tab.monitor_check.setChecked(False)
    tab.monitor_check.setChecked(True)
    assert tab._passthrough.running, "the fixture failed to start monitoring"
    return w, tab


def test_the_monitor_survives_a_tab_switch(qapp, no_hardware):
    """The defect this replaces: monitoring stopped when the Record tab lost
    focus, which pushed people onto Windows' "Listen to this device" as a
    fallback -- and two monitors running at once was misread in the field as a
    ~250 ms channel skew. Audio the user turned on stays on.
    """
    w, tab = _monitoring_window(qapp)
    assert tab._passthrough.running

    # Look at another tab. The real path: the window drives set_active(False).
    w.tabs.setCurrentWidget(w.full_rip)
    qapp.processEvents()

    assert tab._passthrough.running, "the monitor stopped when the tab lost focus"
    assert tab.monitoring


def test_metering_dormancy_does_not_silence_the_passthrough(qapp, no_hardware):
    """Split lifecycles: the meters may go dormant on an inactive tab (cheap,
    and nothing but a paused display), but that must never reach the audio."""
    w, tab = _monitoring_window(qapp)

    stopped = []
    tab._monitor.stop = lambda: stopped.append(True)   # metering only

    tab.set_active(False)

    assert stopped, "metering should go dormant on an inactive tab"
    assert tab._passthrough.running, "metering dormancy silenced the monitor"


def test_the_monitoring_indicator_is_visible_from_a_non_record_tab(qapp, no_hardware):
    """Running audio must never be invisible. The Record tab's own dot is
    hidden with the tab, so the *window* carries the state."""
    w, tab = _monitoring_window(qapp)
    w.tabs.setCurrentWidget(w.full_rip)
    qapp.processEvents()

    assert "MONITORING" in w.windowTitle()
    index = w.tabs.indexOf(tab)
    assert "♪" in w.tabs.tabText(index)


def test_the_monitor_can_be_switched_off_from_anywhere(qapp, no_hardware):
    w, tab = _monitoring_window(qapp)
    w.tabs.setCurrentWidget(w.settings_panel)
    qapp.processEvents()
    assert tab._passthrough.running

    tab.monitor_check.setChecked(False)          # the toggle, from another tab
    qapp.processEvents()

    assert not tab._passthrough.running
    assert not tab.monitoring
    assert "MONITORING" not in w.windowTitle()


def test_recording_and_monitoring_marks_do_not_erase_each_other(qapp, no_hardware):
    """They overlap constantly -- monitoring while recording is the normal case."""
    w, tab = _monitoring_window(qapp)
    w._on_recording_state(True)

    assert "RECORDING" in w.windowTitle()
    assert "MONITORING" in w.windowTitle()

    w._on_recording_state(False)
    assert "RECORDING" not in w.windowTitle()
    assert "MONITORING" in w.windowTitle()       # the monitor plays on


def test_a_monitor_open_failure_is_plain_words_not_a_portaudio_error(
        qapp, no_hardware, monkeypatch):
    """Observed in the field after a replug: "Insufficient memory
    [PaErrorCode -9992]", which is what WASAPI says when an endpoint refuses.
    Same treatment as the rate refusal -- raw text to the debug log only."""
    from gui.main_window import MainWindow

    tab = MainWindow().record_tab
    tab.set_active(True)

    class _FailingPassthrough(_FakePassthrough):
        def start(self, in_dev, out_dev, rate, channels):
            self.error = "PortAudioError: Insufficient memory [PaErrorCode -9992]"

    tab._passthrough = _FailingPassthrough()
    logged: list[str] = []
    monkeypatch.setattr(tab, "_log", logged.append)

    tab.device_combo.setCurrentIndex(_idx_by_name(tab.device_combo, "Line In"))
    tab.monitor_combo.setCurrentIndex(_idx_by_name(tab.monitor_combo, "Speakers"))
    tab.monitor_check.setChecked(False)      # guarantee a real transition
    tab.monitor_check.setChecked(True)

    assert logged, "the failure must say something"
    line = next(m for m in logged if "monitor" in m.lower())
    assert "couldn't start the monitor" in line
    assert "Speakers (Realtek)" in line              # names the device
    assert "Refresh" in line                         # ...and what to do
    assert "PaErrorCode" not in line and "-9992" not in line
    assert not tab.monitor_check.isChecked()         # toggle reset, cleanly
