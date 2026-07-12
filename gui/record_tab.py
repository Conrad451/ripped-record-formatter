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

from core.recorder import LevelMonitor, Recorder, list_input_devices
from core.timefmt import format_timestamp
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
    #: A capture finished and landed at this path. Full Rip listens for this.
    recordingFinished = Signal(object)          # Path
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
        root.addWidget(input_box)

        # --- levels ------------------------------------------------------------
        level_box = QGroupBox("Levels")
        level_layout = QVBoxLayout(level_box)
        self.meters = LevelMeters(channels=2)
        level_layout.addWidget(self.meters)
        hint = QLabel("Meters run whenever this tab is open — set your input gain "
                      "before you press Record. Aim for peaks around −6 dBFS.")
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: palette(mid); }")
        level_layout.addWidget(hint)
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
        self._on_device_changed()

    def current_device(self):
        i = self.device_combo.currentIndex()
        return self._devices[i] if 0 <= i < len(self._devices) else None

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

    def _on_format_changed(self, *_args) -> None:
        self.settings.set(
            record_samplerate=int(self.rate_combo.currentData() or 0),
            record_subtype=str(self.depth_combo.currentData() or "PCM_16"),
        )
        self._restart_monitor()

    # -- level monitoring (pre-roll gain setting) ----------------------------
    def set_active(self, active: bool) -> None:
        """The tab became visible / hidden. Meters only run when it is visible."""
        self._active = active
        if active:
            self._restart_monitor()
        elif not self.recording:
            self._monitor.stop()

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
        if self._monitor.error:
            self._log(f"Record: cannot monitor levels on this device "
                      f"({self._monitor.error}).")

    def _on_monitor_telemetry(self, telemetry) -> None:
        self._latest = telemetry            # audio thread: just hand it over

    def _drain_telemetry(self) -> None:
        """GUI thread: repaint at most 20x/second, from the newest sample only."""
        telemetry = self._latest
        if telemetry is None:
            return
        self._latest = None
        self.meters.update_from(telemetry)
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
        self.recordingFinished.emit(result.path)
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
