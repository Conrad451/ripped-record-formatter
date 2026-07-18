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

from PySide6.QtCore import QTimer, Signal
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
    QVBoxLayout,
    QWidget,
)

from core.recorder import (
    LevelMonitor,
    Passthrough,
    Recorder,
    list_input_devices,
    list_output_devices,
)
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
        self.meters = LevelMeters(channels=2)
        self.meters.resetRequested.connect(self._reset_levels)
        level_layout.addWidget(self.meters)

        # The bars say what the input is doing *now*; the strip says what it has
        # been doing for the last half minute -- which is the question you are
        # actually asking while you set gain.
        self.history_strip = LevelHistoryStrip(channels=2)
        level_layout.addWidget(self.history_strip)

        self.hint = QLabel("Play the loudest passage of the record and adjust input "
                           "gain until peaks stay below −3 dBFS.")
        self.hint.setWordWrap(True)
        level_layout.addWidget(self.hint)

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
            self._log(f"Record: could not enumerate audio devices ({exc}).")

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

        # Offer the standard rates plus whatever the device claims, defaulting to
        # the device's native rate but letting a pinned choice win.
        rates = sorted({*_RATES, dev.samplerate})
        remembered = self.settings.config.record_samplerate
        self.rate_combo.blockSignals(True)
        self.rate_combo.clear()
        for rate in rates:
            suffix = "  (device native)" if rate == dev.samplerate else ""
            self.rate_combo.addItem(f"{rate} Hz{suffix}", rate)
        target = remembered if remembered in rates else dev.samplerate
        self.rate_combo.setCurrentIndex(max(0, self.rate_combo.findData(target)))
        self.rate_combo.blockSignals(False)

        self.record_button.setEnabled(True)
        self.record_button.setToolTip("")
        self._restart_monitor()
        self._restart_passthrough()

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
            self._log(f"Record: cannot monitor levels on this device "
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
        self.meters.update_from(telemetry)
        self.history_strip.update_from(telemetry)
        if self.recording:
            self.elapsed_label.setText(format_timestamp(telemetry.elapsed_s))
            self.size_label.setText(f"{telemetry.bytes_written / 1_048_576:.1f} MB")

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
            self._log("Record: choose a destination folder and file name first.")
            return
        if dest.exists():
            self._log(f"Record: {dest.name} already exists — rename it first "
                      "so an existing side is never overwritten.")
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
            self._log(f"Record: could not start recording — {type(exc).__name__}: {exc}")
            self._restart_monitor()
            return

        self.record_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.device_combo.setEnabled(False)
        self.rate_combo.setEnabled(False)
        self.depth_combo.setEnabled(False)
        self.recordingStateChanged.emit(True)
        self._log(f"Record: recording to {dest.name} "
                  f"({rate} Hz, {subtype.replace('PCM_', '')}-bit, {channels}ch).")

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
        self._log(f"Record: {result.path.name} — {format_timestamp(result.duration)}, "
                  f"max peak {peak}.")
        if result.clip_runs:
            self._log(f"  ! Clipping detected at {result.clip_runs} points — consider "
                      "lowering input gain and re-recording this side.")
        for warning in result.warnings:
            self._log(f"  ! {warning}")

    def shutdown(self) -> None:
        self._ui_timer.stop()
        if self._recorder is not None:
            self._stop_recording()
        self._monitor.stop()
        self._passthrough.stop()
