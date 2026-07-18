"""Record tab: capture a side, hand it to Full Rip.

An appliance, not an editor. Beyond "which input" (once) and "press Record",
the entire UI surface is **level awareness** and **file naming** -- because those
are the only two things that can quietly ruin a rip. No monitoring (Windows'
"Listen to this device" already does that), no live waveform, no editing.

Side-aware naming is the payoff: the next-file field pre-fills ``SideA.wav`` and
auto-advances to ``SideB.wav`` after each stop, so recording a record is
*Record, flip, Record* -- and each finished capture is handed straight to the
Full Rip tab's mapping table when it is pointed at the same folder. Record side
A, flip, record side B, and the album job is already mapped.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.input_gain import EndpointGain
from core.recorder import (
    LevelMonitor,
    Passthrough,
    Recorder,
    list_input_devices,
    list_output_devices,
    supported_input_rates,
)
from core.setup_check import INFO, OK, WARN, CheckResult, check_sample_rate, check_signal


def _is_rate_error(message: str) -> bool:
    """Whether a stream-open failure is a shared-mode rate rejection (-9997)."""
    m = message.lower()
    return "9997" in m or "invalid sample rate" in m
from core.timefmt import format_timestamp
from gui.level_history import LevelHistoryStrip
from gui.meters import LevelMeters

#: Offered regardless of what the device claims natively -- a Realtek line input
#: reports 192000 under WASAPI even when the whole chain is 44.1k.
_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
_SUBTYPE_LABELS = (("16-bit", "PCM_16"), ("24-bit", "PCM_24"))

_SIDE_RE = re.compile(r"^(?P<stem>.*?Side)(?P<letter>[A-Z])(?P<tail>.*)\.wav$",
                      re.IGNORECASE)


def next_side_name(filename: str) -> str:
    """``SideA.wav`` -> ``SideB.wav``. Anything else gets a ``_2`` suffix bump.

    Naming is half the point of the tab: after a stop the field must already hold
    the name of the *next* thing you are about to record, because the physical
    act between them is flipping the record, not typing.
    """
    match = _SIDE_RE.match(filename)
    if match:
        letter = match.group("letter")
        nxt = chr(ord(letter.upper()) + 1)
        if "A" <= nxt <= "Z":
            return f"{match.group('stem')}{nxt}{match.group('tail')}.wav"

    stem = Path(filename).stem
    counter = re.match(r"^(?P<base>.*?)_(?P<n>\d+)$", stem)
    if counter:
        return f"{counter.group('base')}_{int(counter.group('n')) + 1}.wav"
    return f"{stem}_2.wav"


class RecordTab(QWidget):
    """Thin over :mod:`core.recorder`."""

    logMessage = Signal(str)
    #: A capture finished and landed. Carries the full RecordingResult (path plus
    #: warnings/clipping) so Full Rip can carry a flagged capture forward when it
    #: admits the side into a running album. Full Rip listens for this.
    recordingFinished = Signal(object)          # RecordingResult
    #: Recording started/stopped -- the window makes the state unmissable.
    recordingStateChanged = Signal(bool)

    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        cfg = settings.config

        self._devices = []
        self._recorder: Recorder | None = None
        self._monitor = LevelMonitor(on_telemetry=self._on_monitor_telemetry)
        self._latest = None                     # last Telemetry, drained by a timer
        self._active = False                    # is this tab the visible one?
        # setup-check listen window
        self._checking = False
        self._check_peak = float("-inf")
        self._check_clips = 0
        self._pending_check: list = []
        self._check_window_ms = 3000
        self._checked_device = None             # device we last auto-checked

        root = QVBoxLayout(self)

        # --- input -----------------------------------------------------------
        input_box = QGroupBox("Input")
        form = QFormLayout(input_box)

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        device_row.addWidget(self.device_combo, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_devices)
        device_row.addWidget(refresh)
        form.addRow("Device:", self._wrap(device_row))

        self.rate_combo = QComboBox()
        self.rate_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("Sample rate:", self.rate_combo)

        self.depth_combo = QComboBox()
        for label, subtype in _SUBTYPE_LABELS:
            self.depth_combo.addItem(label, subtype)
        idx = self.depth_combo.findData(cfg.record_subtype or "PCM_16")
        self.depth_combo.setCurrentIndex(max(0, idx))
        self.depth_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("Bit depth:", self.depth_combo)

        # --- software monitoring (opt-in passthrough to an output device) ------
        # Checkbox + output picker + a small live indicator share one row, so the
        # feature costs the Input group a single line.
        self._output_devices = []
        self._passthrough = Passthrough()
        monitor_row = QHBoxLayout()
        self.monitor_check = QCheckBox("Monitor")
        self.monitor_check.setChecked(bool(cfg.monitor_enabled))
        self.monitor_check.setToolTip("Hear the input on the output device below.")
        self.monitor_check.toggled.connect(self._on_monitor_changed)
        monitor_row.addWidget(self.monitor_check)
        self.monitor_combo = QComboBox()
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        monitor_row.addWidget(self.monitor_combo, 1)
        self.monitor_indicator = QLabel("● live")
        self.monitor_indicator.setStyleSheet(
            "QLabel { color: #2e7d32; font-weight: bold; }")
        self.monitor_indicator.setVisible(False)
        monitor_row.addWidget(self.monitor_indicator)
        form.addRow("Monitor output:", self._wrap(monitor_row))
        root.addWidget(input_box)

        # --- levels ------------------------------------------------------------
        level_box = QGroupBox("Levels")
        level_layout = QVBoxLayout(level_box)

        # Meters with the input-gain slider beside them: you set the knob while
        # watching the very bars it moves. The slider drives the Windows capture
        # level for the selected device (see _sync_gain_slider); it hides itself
        # when that endpoint can't be reached.
        meter_row = QHBoxLayout()
        self.meters = LevelMeters(channels=2)
        self.meters.resetRequested.connect(self._reset_levels)
        meter_row.addWidget(self.meters, 1)

        self._gain: EndpointGain | None = None
        gain_col = QVBoxLayout()
        self.gain_label = QLabel("Input")
        self.gain_label.setAlignment(Qt.AlignHCenter)
        gain_col.addWidget(self.gain_label)
        self.gain_slider = QSlider(Qt.Vertical)
        self.gain_slider.setRange(0, 100)
        self.gain_slider.setToolTip(
            "Adjusts the Windows input level. If the signal distorts even at low "
            "settings, turn down the source instead.")
        self.gain_slider.valueChanged.connect(self._on_gain_changed)
        gain_col.addWidget(self.gain_slider, 1, Qt.AlignHCenter)
        self.gain_widgets = (self.gain_label, self.gain_slider)
        meter_row.addLayout(gain_col)
        level_layout.addLayout(meter_row)

        # The bars say what the input is doing *now*; the strip says what it has
        # been doing for the last half minute -- which is the question you are
        # actually asking while you set gain.
        self.history_strip = LevelHistoryStrip(channels=2)
        level_layout.addWidget(self.history_strip)

        self.hint = QLabel("Play the loudest passage of the record and turn the input "
                           "volume up until peaks stay just below −3 dBFS.")
        self.hint.setWordWrap(True)
        level_layout.addWidget(self.hint)

        # "Check my setup" -- plain-language advice, never a gate. The button runs
        # the full check (rate + a short listen); a mis-set rate is also flagged
        # automatically when a device is chosen.
        check_row = QHBoxLayout()
        self.check_button = QPushButton("Check my setup")
        self.check_button.clicked.connect(self._run_setup_check)
        check_row.addWidget(self.check_button)
        check_row.addStretch(1)
        level_layout.addLayout(check_row)

        self.check_results = QLabel("")
        self.check_results.setWordWrap(True)
        self.check_results.setVisible(False)
        level_layout.addWidget(self.check_results)

        # Monitoring (software passthrough) shares this space; its hint points at
        # the zero-latency hardware alternative.
        self.monitor_hint = QLabel(
            "Tip: your interface's own headphone jack monitors with zero latency; "
            "this software path adds a little delay.")
        self.monitor_hint.setWordWrap(True)
        self.monitor_hint.setStyleSheet("QLabel { color: palette(mid); }")
        self.monitor_hint.setToolTip(
            "Monitor passes the input through to your chosen output device so you "
            "can hear the record without Windows' 'Listen to this device'.")
        level_layout.addWidget(self.monitor_hint)
        root.addWidget(level_box)

        # --- destination + transport -------------------------------------------
        out_box = QGroupBox("Destination")
        out_form = QFormLayout(out_box)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(cfg.record_output_dir)
        self.folder_edit.setPlaceholderText("Where the side WAVs are written")
        self.folder_edit.editingFinished.connect(
            lambda: self.settings.set(record_output_dir=self.folder_edit.text().strip()))
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        out_form.addRow("Folder:", self._wrap(folder_row))

        self.file_edit = QLineEdit(cfg.record_next_file or "SideA.wav")
        self.file_edit.setToolTip("Auto-advances after each recording: "
                                  "SideA.wav, SideB.wav, ...")
        self.file_edit.editingFinished.connect(
            lambda: self.settings.set(record_next_file=self.file_edit.text().strip()))
        out_form.addRow("Next file:", self.file_edit)
        root.addWidget(out_box)

        transport = QHBoxLayout()
        self.record_button = QPushButton("Record")
        self.record_button.clicked.connect(self._start_recording)
        transport.addWidget(self.record_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_recording)
        transport.addWidget(self.stop_button)
        self.elapsed_label = QLabel("0:00")
        transport.addWidget(self.elapsed_label)
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("QLabel { color: palette(mid); }")
        transport.addWidget(self.size_label)
        transport.addStretch(1)
        root.addLayout(transport)
        root.addStretch(1)

        # Telemetry arrives on the audio thread; repaint on ours, on a timer, so
        # the meters cannot be driven faster than the eye can use.
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)
        self._ui_timer.timeout.connect(self._drain_telemetry)
        self._ui_timer.start()

        self.refresh_devices()

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _log(self, message: str) -> None:
        self.logMessage.emit(message)

    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.recording

    # -- devices -------------------------------------------------------------
    def refresh_devices(self) -> None:
        remembered = self.settings.config.record_device
        try:
            self._devices = list_input_devices()
        except Exception as exc:
            self._devices = []
            self._log(f"Record: couldn't find any audio devices ({exc}).")

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for dev in self._devices:
            self.device_combo.addItem(dev.label(), dev.index)
        self.device_combo.blockSignals(False)

        if not self._devices:
            self.record_button.setEnabled(False)
            self.record_button.setToolTip("No audio input device found.")
            return

        # Remembered by NAME: PortAudio indices move when devices come and go.
        wanted = next((i for i, d in enumerate(self._devices)
                       if d.name == remembered), 0)
        self.device_combo.setCurrentIndex(wanted)
        self._refresh_output_devices()
        self._on_device_changed()

    def _refresh_output_devices(self) -> None:
        """Populate the monitor output picker, remembered by NAME like the input."""
        remembered = self.settings.config.monitor_device
        try:
            self._output_devices = list_output_devices()
        except Exception as exc:
            self._output_devices = []
            self._log(f"Record: could not enumerate output devices ({exc}).")

        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        for dev in self._output_devices:
            self.monitor_combo.addItem(dev.label(), dev.index)
        wanted = next((i for i, d in enumerate(self._output_devices)
                       if d.name == remembered), 0)
        self.monitor_combo.setCurrentIndex(wanted if self._output_devices else -1)
        self.monitor_combo.blockSignals(False)

    def current_device(self):
        i = self.device_combo.currentIndex()
        return self._devices[i] if 0 <= i < len(self._devices) else None

    def current_output_device(self):
        i = self.monitor_combo.currentIndex()
        return self._output_devices[i] if 0 <= i < len(self._output_devices) else None

    def _on_device_changed(self, *_args) -> None:
        dev = self.current_device()
        if dev is None:
            return
        self.settings.set(record_device=dev.name)
        self._sync_gain_slider(dev.name)

        # Probe which of the standard rates this device can actually open under
        # WASAPI shared mode -- the menu tells the truth instead of offering rates
        # that fail with -9997. The device's own rate always works.
        candidates = sorted({*_RATES, dev.samplerate})
        channels = min(2, dev.max_channels) or 1
        try:
            supported = set(supported_input_rates(dev.index, channels, candidates))
        except Exception:
            supported = set(candidates)          # probe unavailable: don't hide anything
        supported.add(dev.samplerate)

        remembered = self.settings.config.record_samplerate
        self.rate_combo.blockSignals(True)
        self.rate_combo.clear()
        for rate in candidates:
            if rate == dev.samplerate:
                label = f"{rate} Hz (device native — recommended)"
            elif rate in supported:
                label = f"{rate} Hz"
            else:
                label = f"{rate} Hz — needs a Windows Sound change"
            self.rate_combo.addItem(label, rate)
        # Default to native; only honour a remembered rate that still opens.
        target = remembered if remembered in supported else dev.samplerate
        self.rate_combo.setCurrentIndex(max(0, self.rate_combo.findData(target)))
        self.rate_combo.blockSignals(False)

        self.record_button.setEnabled(True)
        self.record_button.setToolTip("")
        self._restart_monitor()
        self._restart_passthrough()
        # Automatic pass the first time a device is chosen while the tab is up.
        if self._active and dev.name != self._checked_device:
            self._checked_device = dev.name
            self._run_setup_check()

    # -- input gain (Windows capture-endpoint level) -------------------------
    def _sync_gain_slider(self, device_name: str) -> None:
        """Point the gain slider at ``device_name``'s Windows input level.

        Restores the remembered level for this device (or reads the endpoint's
        current one), and hides the slider entirely -- with a single log line --
        when the endpoint volume can't be reached, so a machine or frozen build
        without the COM interface degrades gracefully.
        """
        self._gain = EndpointGain.for_device(device_name)
        if self._gain is None:
            for w in self.gain_widgets:
                w.setVisible(False)
            return

        remembered = self.settings.config.record_input_levels.get(device_name)
        level = remembered if remembered is not None else self._gain.get()
        if level is None:                       # endpoint opened but won't read
            for w in self.gain_widgets:
                w.setVisible(False)
            self._gain = None
            return

        if remembered is not None:              # push the saved value back to Windows
            self._gain.set(remembered)
        for w in self.gain_widgets:
            w.setVisible(True)
        self.gain_slider.blockSignals(True)
        self.gain_slider.setValue(round(level * 100))
        self.gain_slider.blockSignals(False)

    def _on_gain_changed(self, value: int) -> None:
        """Slider moved: drive the Windows level and remember it for this device."""
        if self._gain is None:
            return
        dev = self.current_device()
        if dev is None:
            return
        level = value / 100.0
        self._gain.set(level)
        levels = dict(self.settings.config.record_input_levels)
        levels[dev.name] = level
        self.settings.set(record_input_levels=levels)

    def _on_format_changed(self, *_args) -> None:
        self.settings.set(
            record_samplerate=int(self.rate_combo.currentData() or 0),
            record_subtype=str(self.depth_combo.currentData() or "PCM_16"),
        )
        self._restart_monitor()
        self._restart_passthrough()

    # -- software monitoring (passthrough) -----------------------------------
    def _on_monitor_changed(self, *_args) -> None:
        out = self.current_output_device()
        self.settings.set(
            monitor_enabled=bool(self.monitor_check.isChecked()),
            monitor_device=out.name if out is not None else "",
        )
        self._restart_passthrough()

    def _restart_passthrough(self) -> None:
        """Start/stop the input->output passthrough from the current controls.

        Runs whenever monitoring is on and the tab is up or recording -- it is
        independent of the Recorder, so it happily runs *alongside* a capture.
        Refuses a same-endpoint output (feedback) and reports an open failure,
        resetting the toggle in either case.
        """
        self._passthrough.stop()
        want = self.monitor_check.isChecked() and (self._active or self.recording)
        if not want:
            self._update_monitor_indicator()
            return
        dev = self.current_device()
        out = self.current_output_device()
        if dev is None or out is None:
            self._update_monitor_indicator()
            return
        if dev.name == out.name:
            self._log("Record: not monitoring -- the output is the same device as "
                      "the input, which would feed back. Pick a different output.")
            self._set_monitor_off()
            return
        rate = int(self.rate_combo.currentData() or dev.samplerate)
        channels = min(2, dev.max_channels, out.max_channels) or 1
        self._passthrough.start(dev.index, out.index, rate, channels)
        if self._passthrough.error:
            self._log(f"Record: could not start monitoring "
                      f"({self._passthrough.error}).")
            self._passthrough.stop()
            self._set_monitor_off()
        elif self._passthrough.latency_s > 0.15:
            # Say nothing about latency unless it is genuinely bad: Windows'
            # shared-mode audio can add well over 150 ms, which the headphone jack
            # avoids entirely.
            self._log(f"Record: monitoring has about "
                      f"{self._passthrough.latency_s * 1000:.0f} ms of delay on this "
                      "machine (Windows shared-mode audio). For in-time listening, "
                      "use your interface's headphone jack.")
        self._update_monitor_indicator()

    def _set_monitor_off(self) -> None:
        """Untick the toggle without re-entering the change handler, and persist."""
        self.monitor_check.blockSignals(True)
        self.monitor_check.setChecked(False)
        self.monitor_check.blockSignals(False)
        self.settings.set(monitor_enabled=False)
        self._update_monitor_indicator()

    def _update_monitor_indicator(self) -> None:
        self.monitor_indicator.setVisible(
            self._passthrough.running and not self._passthrough.error)

    # -- level monitoring (pre-roll gain setting) ----------------------------
    def set_active(self, active: bool) -> None:
        """The tab became visible / hidden. Meters only run when it is visible."""
        self._active = active
        if active:
            self._restart_monitor()
        elif not self.recording:
            self._monitor.stop()
        self._restart_passthrough()      # monitoring follows visibility too

    def _restart_monitor(self) -> None:
        """Run the meters whenever the tab is up and a device is chosen."""
        if self.recording:
            return                          # the Recorder owns the device now
        self._monitor.stop()
        if not self._active:
            return
        dev = self.current_device()
        if dev is None:
            return
        rate = int(self.rate_combo.currentData() or dev.samplerate)
        channels = min(2, dev.max_channels) or 1
        self._monitor.start(dev.index, rate, channels)
        self.history_strip.reset()          # a new stream is a new history
        if self._monitor.error:
            if _is_rate_error(self._monitor.error):
                self._log(f"Record: this device can't run at {rate} Hz — Windows "
                          f"has it set to {dev.samplerate} Hz. Pick that rate; your "
                          "FLACs are saved at 44,100 Hz regardless.")
            else:
                self._log(f"Record: can't show the levels for this device "
                          f"({self._monitor.error}).")

    def _on_monitor_telemetry(self, telemetry) -> None:
        self._latest = telemetry            # audio thread: just hand it over

    def _reset_levels(self) -> None:
        """Reset must reset the *source*, not just the label.

        The meters are redrawn from telemetry every 50 ms, so clearing the label
        alone would show a cleared max for one frame and then put the old one
        straight back. Mid-capture there is nothing to clear: the recorder's max
        is a fact about the file, and the file does not un-clip.
        """
        self._monitor.reset_peaks()
        self.history_strip.reset()

    def _drain_telemetry(self) -> None:
        """GUI thread: repaint at most 20x/second, from the newest sample only."""
        # Monitor health: an output that vanished mid-passthrough sets .error on
        # the audio thread. Notice it here, reset the toggle, and say so plainly.
        # The capture, being a separate object, is untouched.
        if self._passthrough.running and self._passthrough.error:
            self._log(f"Record: monitoring stopped ({self._passthrough.error}).")
            self._passthrough.stop()
            self._set_monitor_off()
        telemetry = self._latest
        if telemetry is None:
            return
        self._latest = None
        if self._checking:                       # accumulate the listen window
            self._check_peak = max(self._check_peak, telemetry.max_peak_dbfs)
            self._check_clips = telemetry.clip_runs
        self.meters.update_from(telemetry)
        self.history_strip.update_from(telemetry)
        if self.recording:
            self.elapsed_label.setText(format_timestamp(telemetry.elapsed_s))
            self.size_label.setText(f"{telemetry.bytes_written / 1_048_576:.1f} MB")

    # -- setup check ---------------------------------------------------------
    def _run_setup_check(self) -> None:
        """Rate check now, then listen for a few seconds and judge the signal.

        Advice only -- it never disables Record. Re-runnable at any time.
        """
        dev = self.current_device()
        if dev is None:
            self._render_checks([CheckResult(WARN, "Choose an input device first.")])
            return
        results: list = []
        rate = check_sample_rate(device_rate=dev.samplerate,
                                 output_rate=self.settings.config.output_sample_rate)
        if rate is not None:
            results.append(rate)
        self._pending_check = results

        # Measure only the listen window: reset the meters, then accumulate.
        self._monitor.reset_peaks()
        self._check_peak = float("-inf")
        self._check_clips = 0
        self._checking = True
        self._render_checks(results + [CheckResult(
            INFO, "Listening for a few seconds — play the loudest part of the record...")])
        QTimer.singleShot(self._check_window_ms, self._finish_setup_check)

    def _finish_setup_check(self) -> None:
        if not self._checking:
            return
        self._checking = False
        peak = self._check_peak if self._check_peak != float("-inf") else -120.0
        signal = check_signal(clip_runs=self._check_clips, peak_dbfs=peak)
        self._render_checks(self._pending_check + [signal])

    def _render_checks(self, results: list) -> None:
        icons = {OK: "✓", WARN: "⚠", INFO: "…"}
        colors = {OK: "#2e7d32", WARN: "#c07000", INFO: "gray"}
        import html

        lines = []
        for r in results:
            lines.append(
                f'<div style="color:{colors.get(r.status, "gray")}; margin-bottom:4px;">'
                f'{icons.get(r.status, "•")} {html.escape(r.message)}</div>')
        self.check_results.setText("".join(lines))
        self.check_results.setVisible(bool(lines))

    # -- transport -----------------------------------------------------------
    def _browse_folder(self) -> None:
        start = self.folder_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Where should recordings go?", start)
        if chosen:
            self.folder_edit.setText(chosen)
            self.settings.set(record_output_dir=chosen)

    def destination(self) -> Path | None:
        folder = self.folder_edit.text().strip()
        name = self.file_edit.text().strip()
        if not folder or not name:
            return None
        if not name.lower().endswith(".wav"):
            name += ".wav"
        return Path(folder) / name

    def _start_recording(self) -> None:
        if self.recording:
            return
        dev = self.current_device()
        if dev is None:
            self._log("Record: choose an input device first.")
            return
        dest = self.destination()
        if dest is None:
            self._log("Record: choose a folder and a file name first.")
            return
        if dest.exists():
            self._log(f"Record: a file named {dest.name} is already here. Rename it "
                      "first so you don't record over it.")
            return

        self._monitor.stop()                # hand the device to the recorder
        self.meters.reset()
        self.history_strip.reset()          # the take starts with a clean strip

        rate = int(self.rate_combo.currentData() or dev.samplerate)
        subtype = str(self.depth_combo.currentData() or "PCM_16")
        channels = min(2, dev.max_channels) or 1

        self._recorder = Recorder(on_telemetry=self._on_monitor_telemetry)
        try:
            self._recorder.start(dev.index, dest, rate, channels, subtype)
        except Exception as exc:
            self._recorder = None
            if _is_rate_error(str(exc)):
                self._log(f"Record: this device can't record at {rate} Hz — Windows "
                          f"has it set to {dev.samplerate} Hz. Pick that rate; your "
                          "FLACs are saved at 44,100 Hz regardless.")
            else:
                self._log(f"Record: couldn't start recording. {exc}")
            self._restart_monitor()
            return

        self.record_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.device_combo.setEnabled(False)
        self.rate_combo.setEnabled(False)
        self.depth_combo.setEnabled(False)
        self.recordingStateChanged.emit(True)
        self._log(f"Record: recording to {dest.name}. Press Stop at the end of the side.")

    def _stop_recording(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        result = recorder.stop()

        self.record_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.device_combo.setEnabled(True)
        self.rate_combo.setEnabled(True)
        self.depth_combo.setEnabled(True)
        self.recordingStateChanged.emit(False)
        self.elapsed_label.setText(format_timestamp(result.duration))

        self._report(result)

        # Name the next side before the user has finished flipping the record.
        advanced = next_side_name(result.path.name)
        self.file_edit.setText(advanced)
        self.settings.set(record_next_file=advanced)

        self.meters.set_clip_runs(result.clip_runs)
        self.recordingFinished.emit(result)
        self._restart_monitor()

    def _report(self, result) -> None:
        peak = ("—" if result.max_peak_dbfs in (None, float("-inf"))
                else f"{result.max_peak_dbfs:+.1f} dBFS")
        self._log(f"Record: saved {result.path.name} ({format_timestamp(result.duration)}). "
                  f"Loudest point: {peak}.")
        if result.clip_runs:
            self._log(f"  ! The sound was too loud and distorted in {result.clip_runs} "
                      "spot(s). Turn the input volume down and record this side again.")
        for warning in result.warnings:
            self._log(f"  ! {warning}")

    def shutdown(self) -> None:
        self._ui_timer.stop()
        if self._recorder is not None:
            self._stop_recording()
        self._monitor.stop()
        self._passthrough.stop()
