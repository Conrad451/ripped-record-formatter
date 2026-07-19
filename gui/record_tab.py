"""Record tab: capture a side, hand it to Full Rip.

An appliance, not an editor. Beyond "which input" (once) and "press Record",
the entire UI surface is **level awareness** and **file naming** -- because those
are the only two things that can quietly ruin a rip. No live waveform, no
editing.

Monitoring is the one thing here that is *not* scoped to this tab. The
passthrough runs at app level once switched on -- it survives tab switches and
focus loss, and only the user, app exit or a dead output device stops it. The
meters may go dormant when the tab is hidden; the audio may not. See
:meth:`RecordTab.set_active`.

Side-aware naming is the payoff: the next-file field pre-fills ``SideA.wav`` and
auto-advances to ``SideB.wav`` after each stop, so recording a record is
*Record, flip, Record* -- and each finished capture is handed straight to the
Full Rip tab's mapping table when it is pointed at the same folder. Record side
A, flip, record side B, and the album job is already mapped.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
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
from core.timefmt import format_timestamp
from core.tracks import safe_part
from gui.text_styles import apply_body, apply_muted
from gui.gain_fader import GainFader
from gui.level_history import LevelHistoryStrip
from gui.meters import LevelMeters, format_channel_peak
from gui.release_preview import ReleasePreview

log = logging.getLogger(__name__)


class ElidedPathLabel(QLabel):
    """A one-line path display that shortens in the middle to fit.

    A NAS path is easily wider than the tab, and this label sits in a place with
    no vertical budget to spend on wrapping. The two ends of a path are the
    parts that identify it -- the drive and the file name -- so the middle is
    what gets dropped. :meth:`full_text` stays authoritative regardless of the
    widget's width, and is what the tooltip carries.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._full = text
        self.setWordWrap(False)
        self.setMinimumWidth(120)

    def setFullText(self, text: str) -> None:
        self._full = text
        self._apply_elision()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        width = self.width()
        if width <= 0:                     # not laid out yet: show it whole
            super().setText(self._full)
            return
        super().setText(
            self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, width))


def _is_rate_error(message: str) -> bool:
    """Whether a stream-open failure is a shared-mode rate rejection (-9997)."""
    m = message.lower()
    return "9997" in m or "invalid sample rate" in m


#: Offered regardless of what the device claims natively -- a Realtek line input
#: reports 192000 under WASAPI even when the whole chain is 44.1k.
_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
_SUBTYPE_LABELS = (("16-bit", "PCM_16"), ("24-bit", "PCM_24"))

_SIDE_RE = re.compile(r"^(?P<stem>.*?Side)(?P<letter>[A-Z])(?P<tail>.*)\.wav$",
                      re.IGNORECASE)


def side_labels(release) -> list[str]:
    """The release's *own* labels for its sides, where it has any.

    MusicBrainz media usually carry no title for a vinyl side, in which case
    this is a list of empty strings and naming falls back to plain lettering.
    Only a medium that actually names itself gets to name a file.
    """
    if release is None:
        return []
    return [(getattr(m, "title", "") or "").strip() for m in release.media]


def next_side_name(filename: str, labels: "list[str] | None" = None) -> str:
    """``SideA.wav`` -> ``SideB.wav``. Anything else gets a ``_2`` suffix bump.

    Naming is half the point of the tab: after a stop the field must already hold
    the name of the *next* thing you are about to record, because the physical
    act between them is flipping the record, not typing.

    ``labels`` are the selected release's side labels, honoured where they exist.
    They are advisory and never a limit: the release's side count says nothing
    about how many sides are really on the platter (CD collapses and pressing
    differences make it wrong often enough to distrust), so running past the end
    of the list simply falls through to lettering rather than stopping or
    complaining. See task 9.10's ruling: MusicBrainz content is trustworthy,
    MusicBrainz shape is advisory.
    """
    named = [lbl for lbl in (labels or []) if lbl]
    if named:
        stem = Path(filename).stem
        for i, label in enumerate(named[:-1]):
            if stem.lower() == safe_part(label).lower():
                return f"{safe_part(named[i + 1])}.wav"
        # Past the release's last named side: fall through, never cap.

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
    #: The monitor started/stopped. It outlives this tab's visibility, so the
    #: *window* has to show it: audio running from a tab you cannot see is
    #: invisible sound, which is exactly how the double-monitor confusion
    #: started. The window carries this into the title.
    monitoringStateChanged = Signal(bool)
    #: "Done recording -- process this album": the user says the album is
    #: finished. A bridge to the Full Rip tab, never a trigger for processing.
    processAlbumRequested = Signal()
    #: One plain sentence for the window's status strip. Separate from
    #: logMessage: the log is the history, this is the present tense.
    statusMessage = Signal(str)

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

        # -- session/album state (9.10) ---------------------------------------
        # The album the user has declared, if any. Optional throughout: an
        # anonymous session is a first-class flow, not a degraded one.
        self._release = None
        #: Last monitoring state pushed to the window, so the signal fires on
        #: transitions rather than on every passthrough restart.
        self._monitor_shown = False
        #: Did the user type this folder themselves? A suggestion must never
        #: overwrite a hand-entered path, so we only ever fill a field the user
        #: has left alone.
        self._folder_hand_edited = False
        #: Completed recordings that actually landed in Full Rip's mapping this
        #: session -- what arms the "process this album" bridge.
        self._landed = 0
        #: (side label, album name or None) for the most recent handoff, so the
        #: post-stop summary line can say where the side went.
        self._last_mapping = None

        root = QVBoxLayout(self)
        # The Record tab is the tallest thing in the app and it exhausts an
        # 800px window (measured in 9.9, and again in 9.14 when the meters grew
        # into instruments). The height the meters need is *function* -- gain is
        # the highest-stakes judgment here -- so it is bought from chrome, which
        # is not: tighter gaps between groups and inside the forms. The history
        # lanes are never touched. See the 9.14 report's budget accounting.
        root.setSpacing(4)
        root.setContentsMargins(6, 4, 6, 4)

        # --- input -----------------------------------------------------------
        input_box = QGroupBox("Input")
        form = QFormLayout(input_box)
        form.setVerticalSpacing(4)
        form.setContentsMargins(8, 6, 8, 6)

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        device_row.addWidget(self.device_combo, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_devices)
        device_row.addWidget(refresh)

        # "Check my setup" is diagnostics, not a step. It used to sit as a
        # full-width button in the middle of the tab, which put a troubleshooting
        # tool directly in the path of the ordinary flow -- and the checks that
        # actually matter (no signal, too hot, rate reassurance) run on their own
        # anyway, on device selection and during a take. So it demotes to a link
        # beside the device it inspects: findable when something is wrong,
        # invisible when nothing is.
        self.check_button = QPushButton("Check my setup")
        self.check_button.setFlat(True)
        self.check_button.setCursor(Qt.PointingHandCursor)
        self.check_button.setStyleSheet(
            "QPushButton { border: none; color: palette(link); "
            "text-decoration: underline; padding: 0 6px; }")
        self.check_button.setToolTip(
            "Check the sample rate and listen for a few seconds, then say in "
            "plain words what to fix. Advice only -- it never blocks recording.")
        self.check_button.clicked.connect(self._run_setup_check)
        device_row.addWidget(self.check_button)
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
        level_layout.setSpacing(4)
        level_layout.setContentsMargins(8, 6, 8, 6)

        # Meters with the input-gain slider beside them: you set the knob while
        # watching the very bars it moves. The slider drives the Windows capture
        # level for the selected device (see _sync_gain_slider); it hides itself
        # when that endpoint can't be reached.
        self.meters = LevelMeters(channels=2)
        self.meters.resetRequested.connect(self._reset_levels)
        level_layout.addWidget(self.meters)

        # The gain fader sits directly under the bars it moves, full width,
        # carrying its own level ribbon: setting gain is a closed loop -- drag,
        # watch, drag -- and for as long as the knob was a thin vertical slider
        # off to one side, that loop spanned the gap between two widgets. See
        # gui.gain_fader for why the -3 dBFS mark lives on the ribbon and not on
        # the gain axis.
        self._gain: EndpointGain | None = None
        self.gain_fader = GainFader()
        self.gain_fader.valueChanged.connect(self._on_gain_changed)
        level_layout.addWidget(self.gain_fader)
        self.gain_widgets = (self.gain_fader,)

        # The bars say what the input is doing *now*; the strip says what it has
        # been doing for the last half minute -- which is the question you are
        # actually asking while you set gain.
        self.history_strip = LevelHistoryStrip(channels=2)
        level_layout.addWidget(self.history_strip)

        self.hint = QLabel("Play the loudest passage of the record and turn the input "
                           "volume up until peaks stay just below −3 dBFS.")
        self.hint.setWordWrap(True)
        level_layout.addWidget(self.hint)

        self.check_results = QLabel("")
        self.check_results.setWordWrap(True)
        self.check_results.setVisible(False)
        level_layout.addWidget(self.check_results)

        # Monitoring (software passthrough) shares this space; its hint points at
        # the zero-latency hardware alternative.
        # Three sentences, each retiring a specific field confusion: the delay,
        # the double-monitor echo (which cost a stakeholder a debugging session
        # -- they diagnosed two concurrent monitors as channel skew), and the
        # belief that you can judge levels by ear through this path.
        self.monitor_hint = QLabel(
            "Tip: your interface's own headphone jack monitors with zero latency; "
            "this software path adds a little delay. If you also have Windows "
            "'Listen to this device' enabled, turn it off — two monitors at once "
            "sounds like an echo. The monitor is for hearing the record play — "
            "judge levels by the meters, not by ear.")
        self.monitor_hint.setWordWrap(True)
        apply_muted(self.monitor_hint)
        self.monitor_hint.setToolTip(
            "Monitor passes the input through to your chosen output device so you "
            "can hear the record without Windows' 'Listen to this device'.")
        level_layout.addWidget(self.monitor_hint)
        root.addWidget(level_box)

        # --- destination + transport -------------------------------------------
        out_box = QGroupBox("Destination")
        out_form = QFormLayout(out_box)
        out_form.setVerticalSpacing(4)
        out_form.setContentsMargins(8, 6, 8, 6)

        # --- album (optional) --------------------------------------------------
        # Saying what is on the platter is worth doing *before* the capture, not
        # after: it names the folder, names the files, and rides the handoff so
        # Full Rip gets identity together with the audio. Entirely optional --
        # leave it alone and the tab behaves exactly as it always has.
        album_row = QHBoxLayout()
        self.lookup_button = QPushButton("Look up release...")
        self.lookup_button.setToolTip(
            "Say which record you're about to play. Optional -- it fills in the "
            "folder and file names, and carries over to Full Rip.")
        self.lookup_button.clicked.connect(self._open_lookup)
        album_row.addWidget(self.lookup_button)
        self.clear_release_button = QPushButton("Clear")
        self.clear_release_button.setVisible(False)
        self.clear_release_button.clicked.connect(self._clear_release_clicked)
        album_row.addWidget(self.clear_release_button)
        # The preview shares the button's row rather than taking one of its own:
        # hidden it costs nothing, and shown it grows the row it already had.
        self.release_preview = ReleasePreview(thumb_size=40)
        album_row.addWidget(self.release_preview, 1)
        album_row.addStretch(1)
        out_form.addRow("Album:", self._wrap(album_row))

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(cfg.record_output_dir)
        self.folder_edit.setPlaceholderText("Where the side WAVs are written")
        self.folder_edit.editingFinished.connect(
            lambda: self.settings.set(record_output_dir=self.folder_edit.text().strip()))
        # textEdited fires only for *user* keystrokes, never for setText -- which
        # is exactly the line between "the user chose this path" and "we offered
        # it". Once hand-edited, no suggestion touches it again.
        self.folder_edit.textEdited.connect(self._note_folder_hand_edited)
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        # States its function, not its type: "Folder" names the widget, this
        # names the job it does.
        out_form.addRow("Recordings save to:", self._wrap(folder_row))

        self.file_edit = QLineEdit(cfg.record_next_file or "SideA.wav")
        self.file_edit.setToolTip("Auto-advances after each recording: "
                                  "SideA.wav, SideB.wav, ...")
        self.file_edit.editingFinished.connect(
            lambda: self.settings.set(record_next_file=self.file_edit.text().strip()))
        out_form.addRow("Next file:", self.file_edit)

        # Where the file actually lands, resolved and shown before the action
        # that lands it -- the same doctrine as the frozen "Encoding to:" line.
        # A stakeholder could not identify the output folder from their own
        # screenshot of this tab: a folder box and a name box are two halves of
        # a path the user is left to assemble in their head, and the answer to
        # "where is my recording going" should not require mental string
        # concatenation.
        # One line, elided in the middle, with the whole path in the tooltip. A
        # NAS path can be long enough to wrap to three lines, and the Record tab
        # has no vertical budget to spend on a caption that grows with the
        # user's folder names.
        self.destination_label = ElidedPathLabel("—")
        apply_muted(self.destination_label)
        out_form.addRow("", self.destination_label)
        root.addWidget(out_box)

        self.folder_edit.textChanged.connect(self._refresh_destination)
        self.file_edit.textChanged.connect(self._refresh_destination)
        self._refresh_destination()

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
        apply_muted(self.size_label)
        transport.addWidget(self.size_label)
        transport.addStretch(1)

        # The end of the record-first flow. Stop finishes a *side*; this says the
        # *album* is done -- the one judgement only the user can make, since a
        # release's side count is too often wrong to infer it from (9.10 ruling).
        # It carries the session over to Full Rip and stops there: a bridge, not
        # a trigger.
        self.process_button = QPushButton("Done recording — process this album")
        self.process_button.setEnabled(False)
        self.process_button.setToolTip(
            "Take this session's recordings to the Full Rip tab, ready to "
            "process. Nothing starts until you say so there.")
        self.process_button.clicked.connect(self._request_process)
        transport.addWidget(self.process_button)
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
    def _release_gain(self) -> None:
        """Close the endpoint handle, if we hold one. Idempotent."""
        gain, self._gain = self._gain, None
        if gain is not None:
            gain.close()

    def _sync_gain_slider(self, device_name: str) -> None:
        """Point the gain slider at ``device_name``'s Windows input level.

        Restores the remembered level for this device (or reads the endpoint's
        current one), and hides the slider entirely -- with a single log line --
        when the endpoint volume can't be reached, so a machine or frozen build
        without the COM interface degrades gracefully.
        """
        # Hand back the previous device's endpoint before binding the next one.
        # Rebinding self._gain used to drop the old pointer for the garbage
        # collector, which is exactly the lifetime bug: switching devices a few
        # times left a pile of COM pointers waiting to be released at whatever
        # moment GC next ran, on whatever thread it happened to be on.
        self._release_gain()

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
            self._release_gain()                # opened it, so give it back
            return

        if remembered is not None:              # push the saved value back to Windows
            self._gain.set(remembered)
        for w in self.gain_widgets:
            w.setVisible(True)
        # Silent: this value came *from* the endpoint, so echoing it back would
        # be a write we did not ask for.
        self.gain_fader.set_value(round(level * 100), silent=True)

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
        # Deliberately NOT gated on self._active. Monitoring is a *session*
        # feature, not a tab feature: it runs at app level until the user turns
        # it off, the app closes, or the output device dies. It used to stop
        # when the Record tab lost focus, which pushed people to Windows'
        # "Listen to this device" as a fallback -- and with both running at once
        # the doubled audio was misread in the field as ~250 ms channel skew.
        want = self.monitor_check.isChecked()
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
            # Plain words, per 9.9: the raw PortAudio text is for the debug log,
            # never the user's face. The field report was "Insufficient memory
            # [PaErrorCode -9992]" -- which is what WASAPI says when an endpoint
            # refuses, typically after the device was replugged, and which tells
            # the operator nothing they can act on.
            log.info("Monitor open failed on %s: %s", out.name, self._passthrough.error)
            self._log(f"Record: couldn't start the monitor on {out.name} — it may "
                      "be in use or was recently unplugged. Press Refresh and try "
                      "again.")
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
        live = self._passthrough.running and not self._passthrough.error
        self.monitor_indicator.setVisible(live)
        # ...and tell the window, which shows it from every tab.
        if live != self._monitor_shown:
            self._monitor_shown = live
            self.monitoringStateChanged.emit(live)

    @property
    def monitoring(self) -> bool:
        """Whether the passthrough is live -- regardless of which tab is up."""
        return self._passthrough.running and not self._passthrough.error

    # -- level monitoring (pre-roll gain setting) ----------------------------
    def set_active(self, active: bool) -> None:
        """The tab became visible / hidden.

        Metering and passthrough have deliberately *separate* lifecycles here.
        The meters are a tab feature -- letting the level streams go dormant on
        an inactive tab is cheap and costs nothing but a paused display. The
        monitor is a session feature and is untouched by this: audio the user
        turned on must not stop because they looked at another tab.
        """
        self._active = active
        if active:
            self._restart_monitor()
        elif not self.recording:
            self._monitor.stop()         # metering only; the passthrough plays on

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
        # The fader's ribbon shows the loudest channel: while you are setting
        # gain the question is whether *anything* is too hot, not which side.
        peaks = [p for p in telemetry.peaks_dbfs if p is not None]
        self.gain_fader.set_level(max(peaks) if peaks else float("-inf"))
        if self.recording:
            self.elapsed_label.setText(format_timestamp(telemetry.elapsed_s))
            self.size_label.setText(f"{telemetry.bytes_written / 1_048_576:.1f} MB")
            self.statusMessage.emit(self.recording_status(telemetry))

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

    # -- album (optional identity for the session) ----------------------------
    @property
    def release(self):
        """The release the user declared for this session, or ``None``."""
        return self._release

    def _open_lookup(self) -> None:
        """The existing MetadataPanel, opened as a modal. One lookup UI, reused."""
        from gui.metadata_panel import MetadataPanel

        dialog = QDialog(self)
        dialog.setWindowTitle("Look up release")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        # Pass settings through so the panel sees the user's MusicBrainz contact.
        panel = MetadataPanel(settings=self.settings,
                              store=getattr(self, 'store', None))
        panel.statusMessage.connect(self._log)
        panel.releaseSelected.connect(
            lambda detail: (self.apply_release(detail), dialog.accept()))
        layout.addWidget(panel)
        dialog.exec()

    def apply_release(self, detail) -> None:
        """Adopt ``detail`` as this session's album and offer what follows from it."""
        self._release = detail
        self.release_preview.set_release(detail)
        self.clear_release_button.setVisible(True)
        self._suggest_folder()
        self._log(f"Record: recording {detail.artist} — {detail.title}.")

    def clear_release(self) -> None:
        """Forget the album. Called by the 9.7 clean slate when one concludes."""
        self._release = None
        self.release_preview.clear()
        self.clear_release_button.setVisible(False)

    def _clear_release_clicked(self) -> None:
        self.clear_release()
        self._log("Record: album cleared — recording without a release.")

    def _note_folder_hand_edited(self, _text: str) -> None:
        self._folder_hand_edited = True

    def _folder_root(self) -> str:
        """The trunk that derived recording folders hang off.

        The **WAV root**, not the FLAC root. This used to derive from
        ``default_output_dir`` -- the finished-library root -- so a capture
        offered to save itself into the tree of finished FLACs, and the
        stakeholder was correcting the path by hand every session. The raw WAV
        is the master and lives with masters; the finished library is a
        different place with a different lifecycle, and only Full Rip writes
        there.

        Falls back to whatever folder the tab is already pointed at. Suggestions
        are never written back to settings, so this stays a root rather than
        creeping down into the last album's folder.
        """
        cfg = self.settings.config
        return (cfg.default_source_dir or cfg.record_output_dir
                or self.folder_edit.text().strip())

    def _suggest_folder(self) -> None:
        """Offer ``{root}/{Artist}/{Album}`` -- prefilled, editable, never forced.

        Skipped entirely once the user has typed a path of their own: an offer
        that overwrites a deliberate choice is not an offer.
        """
        if self._release is None or self._folder_hand_edited:
            return
        root = self._folder_root()
        if not root:
            return
        suggestion = Path(root) / safe_part(self._release.artist) / safe_part(
            self._release.title)
        self.folder_edit.setText(str(suggestion))

    # -- the record-to-rip bridge --------------------------------------------
    def note_handoff(self, landed: bool, side_label=None, album=None) -> None:
        """The window reports where the just-finished recording went.

        Called between the handoff and :meth:`_report`, so the saved-summary line
        can name the side it mapped to, and so the bridge button knows whether
        this session has anything worth processing.
        """
        self._last_mapping = (side_label, album) if landed and side_label else None
        if landed:
            self._landed += 1
        self.process_button.setEnabled(self._landed > 0)

    def reset_session(self) -> None:
        """A new record: forget the album and disarm the bridge (9.7 clean slate)."""
        self.clear_release()
        self._landed = 0
        self._last_mapping = None
        self.process_button.setEnabled(False)

    def _request_process(self) -> None:
        self.processAlbumRequested.emit()

    # -- transport -----------------------------------------------------------
    def _browse_folder(self) -> None:
        start = self.folder_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Where should recordings go?", start)
        if chosen:
            self.folder_edit.setText(chosen)
            self._folder_hand_edited = True      # browsing is choosing
            self.settings.set(record_output_dir=chosen)

    def destination(self) -> Path | None:
        folder = self.folder_edit.text().strip()
        name = self.file_edit.text().strip()
        if not folder or not name:
            return None
        if not name.lower().endswith(".wav"):
            name += ".wav"
        return Path(folder) / name

    def _refresh_destination(self) -> None:
        """Show the exact path the next recording will be written to.

        Driven from :meth:`destination` rather than re-deriving the join, so
        the line cannot disagree with the file that actually gets written --
        including the ``.wav`` the field does not make you type.
        """
        dest = self.destination()
        if dest is None:
            self.destination_label.setFullText("— choose a folder and a file name")
            self.destination_label.setToolTip("")
            apply_muted(self.destination_label, italic=True)
            return

        self.destination_label.setFullText(f"→ {dest}")
        self.destination_label.setToolTip(str(dest))
        # Not muted: this is the answer to "where is my recording going", which
        # is a thing to read, not a caption.
        self.destination_label.setStyleSheet("QLabel { font-family: monospace; }")

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

        self.meters.set_clip_runs(result.clip_runs)

        # The handoff runs *before* the summary line, so the line can say where
        # the side actually landed. The window answers via note_handoff().
        self._last_mapping = None
        self.recordingFinished.emit(result)

        self._report(result)

        # Name the next side before the user has finished flipping the record.
        advanced = next_side_name(result.path.name, side_labels(self._release))
        self.file_edit.setText(advanced)
        self.settings.set(record_next_file=advanced)

        self._restart_monitor()

    def recording_status(self, telemetry) -> str:
        """"Recording Side C — 2:14, peaks −8.1" -- the live status line.

        Names the side rather than the file, because the side is what the user
        is holding. The peak is the number they are watching for, so it travels
        with the elapsed time rather than living only on the meters.
        """
        side = Path(self.file_edit.text().strip() or "this side").stem
        elapsed = format_timestamp(telemetry.elapsed_s)
        peaks = [p for p in telemetry.peaks_dbfs if p is not None]
        loudest = max(peaks) if peaks else None
        if loudest is None or math.isinf(loudest) or math.isnan(loudest):
            return f"Recording {side} — {elapsed}"
        return (f"Recording {side} — {elapsed}, "
                f"peaks {format_channel_peak(loudest)}")

    def _report(self, result) -> None:
        peak = ("—" if result.max_peak_dbfs in (None, float("-inf"))
                else f"{result.max_peak_dbfs:+.1f} dBFS")
        # Where it went matters as much as that it saved -- without this the flow
        # dead-ends at Stop and the mapping is invisible from here (9.10).
        where = ""
        if self._last_mapping is not None:
            side, album = self._last_mapping
            where = (f" — mapped to {side} of {album} in Full Rip" if album
                     else f" — mapped to {side} in Full Rip")
        self._log(f"Record: saved {result.path.name} ({format_timestamp(result.duration)}). "
                  f"Loudest point: {peak}{where}.")
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
        # Explicitly, on the GUI thread, while the interpreter is still healthy.
        # Left to interpreter teardown this becomes the same crash in a smaller
        # window: comtypes runs CoUninitialize from an atexit handler, and a
        # pointer finalised after that point is released into an apartment that
        # no longer exists.
        self._release_gain()
