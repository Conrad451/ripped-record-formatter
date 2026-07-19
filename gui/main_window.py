"""The main application window.

Layout: a tab widget with two :class:`BatchPanel` tabs (Convert / Re-tag) over a
shared progress bar and log pane. Each panel gathers directories, album/artist,
and a track table; the window runs the selected job on a background thread and
routes progress/log/errors into the shared widgets.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import config as core_config
from core import converter
from core import mp3_export
from core.version import __version__
from gui import status_strip
from gui.resume_bar import ResumeBar
from gui.status_strip import StatusStrip
from gui.track_model import COL_ARTIST, Row, TrackTableModel, TrackTableView


class Settings:
    """Thin wrapper over :mod:`core.config` that saves on every change."""

    def __init__(self) -> None:
        self.config = core_config.load()

    def set(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self.config, key, value)
        core_config.save(self.config)


class BatchPanel(QWidget):
    """One tab: directory pickers, metadata fields, and a track table.

    ``kind`` is ``"convert"`` (scan WAVs, produce FLACs) or ``"retag"`` (scan
    existing FLACs, rewrite tags).
    """

    logMessage = Signal(str)
    runRequested = Signal()

    def __init__(self, kind: str, settings: Settings):
        super().__init__()
        self.kind = kind
        self.settings = settings
        self._file_glob = "*.wav" if kind == "convert" else "*.flac"
        self._cover = None   # set from a selected MusicBrainz release
        self.mp3_section = None   # Convert only; attached by :class:`MainWindow`

        cfg = settings.config
        layout = QVBoxLayout(self)

        # --- directory + metadata form -------------------------------------
        form = QGridLayout()
        self.source_edit = QLineEdit(cfg.source_dir)
        self.output_edit = QLineEdit(cfg.output_dir)
        self.artist_edit = QLineEdit(cfg.last_artist)
        self.album_edit = QLineEdit(cfg.last_album)

        source_label = "Source WAV folder:" if kind == "convert" else "Source FLAC folder:"
        form.addWidget(QLabel(source_label), 0, 0)
        form.addWidget(self.source_edit, 0, 1)
        form.addWidget(self._browse_button(self.source_edit, "source_dir"), 0, 2)

        form.addWidget(QLabel("Output folder:"), 1, 0)
        form.addWidget(self.output_edit, 1, 1)
        form.addWidget(self._browse_button(self.output_edit, "output_dir"), 1, 2)

        form.addWidget(QLabel("Artist:"), 2, 0)
        form.addWidget(self.artist_edit, 2, 1)
        form.addWidget(QLabel("Album:"), 3, 0)
        form.addWidget(self.album_edit, 3, 1)
        layout.addLayout(form)

        # persist fields on edit
        self.source_edit.editingFinished.connect(
            lambda: self.settings.set(source_dir=self.source_edit.text().strip())
        )
        self.output_edit.editingFinished.connect(
            lambda: self.settings.set(output_dir=self.output_edit.text().strip())
        )
        self.artist_edit.editingFinished.connect(
            lambda: self.settings.set(last_artist=self.artist_edit.text().strip())
        )
        self.album_edit.editingFinished.connect(
            lambda: self.settings.set(last_album=self.album_edit.text().strip())
        )

        # --- mode-specific controls ----------------------------------------
        controls = QHBoxLayout()
        self.load_button = QPushButton(
            "Load WAVs" if kind == "convert" else "Load FLACs"
        )
        self.load_button.clicked.connect(self.load_files)
        controls.addWidget(self.load_button)

        self.soundtrack_check: QCheckBox | None = None
        self.delete_check: QCheckBox | None = None
        self.lookup_button: QPushButton | None = None
        if kind == "convert":
            self.soundtrack_check = QCheckBox("Soundtrack mode (per-track artist)")
            self.soundtrack_check.toggled.connect(self._update_artist_column)
            controls.addWidget(self.soundtrack_check)
        else:
            # Re-tagging a folder is precisely when you want a tracklist, and
            # until now the only way in was the standalone Metadata tab -- which
            # applies to whichever batch panel you happened to visit last. This
            # opens the same panel scoped to this tab, so the release lands here.
            self.lookup_button = QPushButton("Look up release...")
            self.lookup_button.clicked.connect(self._open_lookup)
            controls.addWidget(self.lookup_button)

            self.delete_check = QCheckBox("Delete source files after re-tag")
            self.delete_check.setChecked(False)
            controls.addWidget(self.delete_check)

        controls.addStretch(1)
        self.run_button = QPushButton(
            "Convert" if kind == "convert" else "Re-tag"
        )
        self.run_button.clicked.connect(self.runRequested)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        # --- track table ----------------------------------------------------
        self.model = TrackTableModel()
        self.table = TrackTableView()
        self.table.setModel(self.model)
        self.table.pasted.connect(self._on_pasted)
        self.table.rowsDeleted.connect(
            lambda n: self.logMessage.emit(f"Removed {n} row(s).")
        )
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self._update_artist_column()

    # --- helpers -----------------------------------------------------------
    def _browse_button(self, target: QLineEdit, config_key: str) -> QPushButton:
        button = QPushButton("Browse...")

        def choose() -> None:
            start = target.text().strip() or str(Path.home())
            chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
            if chosen:
                target.setText(chosen)
                self.settings.set(**{config_key: chosen})

        button.clicked.connect(choose)
        return button

    def _wants_per_row_artist(self) -> bool:
        if self.kind == "retag":
            return True
        return bool(self.soundtrack_check and self.soundtrack_check.isChecked())

    def _update_artist_column(self) -> None:
        self.table.setColumnHidden(COL_ARTIST, not self._wants_per_row_artist())

    def _on_pasted(self, filled: int, ignored: int) -> None:
        msg = f"Pasted {filled} title(s)."
        if ignored:
            msg += f" {ignored} line(s) had no matching row and were ignored."
        self.logMessage.emit(msg)

    # --- public API used by MainWindow -------------------------------------
    def add_export_section(self, section) -> None:
        """Append the MP3 export section below the track table (Convert only).

        The section is built by :class:`MainWindow` rather than here because it
        reads from the Full Rip tab (for "the album just finished"), which a
        panel has no business knowing about.
        """
        self.mp3_section = section
        self.layout().addWidget(section)

    def set_source_dir(self, path: str) -> None:
        self.source_edit.setText(path)
        self.settings.set(source_dir=path)

    def load_files(self) -> None:
        source = self.source_edit.text().strip()
        if not source or not Path(source).is_dir():
            self.logMessage.emit(f"Source folder not found: {source!r}")
            return
        files = sorted(Path(source).glob(self._file_glob))
        default_artist = self.artist_edit.text().strip()
        rows = [Row(title=f.stem, artist=default_artist, source_path=f) for f in files]
        self.model.set_rows(rows)
        self._update_artist_column()
        self.logMessage.emit(
            f"Loaded {len(rows)} {self._file_glob} file(s) from {source}"
        )

    def collect_job(self):
        """Return ``(operation, tracks, output_dir, kwargs)`` or ``None``.

        Emits a log message and returns ``None`` when the panel is not ready.
        """
        output = self.output_edit.text().strip()
        if not output:
            self.logMessage.emit("Please choose an output folder.")
            return None
        album = self.album_edit.text().strip()
        artist = self.artist_edit.text().strip()
        tracks = self.model.build_tracks(
            album, artist, self._wants_per_row_artist()
        )
        if not tracks:
            self.logMessage.emit("No tracks to process -- load files first.")
            return None

        max_workers = self.settings.config.encode_workers
        if self.kind == "convert":
            # A plain Convert encodes without restoration, so provenance is
            # known: RRF_RESTORATION is `none` (an empty stage list), RRF_VERSION
            # is stamped. Re-tag, below, never touches RRF fields.
            kwargs = {"max_workers": max_workers, "restoration_stages": [],
                      "output_sample_rate": self.settings.config.output_sample_rate}
            if self._cover is not None:
                kwargs["cover"] = self._cover
            return converter.convert_wavs_to_flacs, tracks, Path(output), kwargs
        delete = bool(self.delete_check and self.delete_check.isChecked())
        kwargs = {"delete_source": delete, "max_workers": max_workers}
        if self._cover is not None:
            kwargs["cover"] = self._cover
        return converter.retag_flacs, tracks, Path(output), kwargs

    def apply_release(self, detail) -> None:
        """Fill artist/album, replace track titles by order, stash cover art."""
        self.artist_edit.setText(detail.artist)
        self.settings.set(last_artist=detail.artist)
        self.album_edit.setText(detail.title)
        self.settings.set(last_album=detail.title)
        titles = [t.title for t in detail.tracks]
        if titles and self.model.rowCount():
            self.model.paste_titles(0, titles)
        self._cover = detail.cover

    def _open_lookup(self) -> None:
        """Open the shared MetadataPanel as a modal, wired to this panel only."""
        from gui.metadata_panel import MetadataPanel

        dialog = QDialog(self)
        dialog.setWindowTitle("Look up release")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        # settings carries the user's MusicBrainz contact (and the panel's
        # splitter position); without it the lookup would identify itself as
        # having no contact even when one is configured.
        panel = MetadataPanel(settings=self.settings,
                              store=getattr(self, 'store', None))
        panel.artist_edit.setText(self.artist_edit.text())
        panel.album_edit.setText(self.album_edit.text())
        panel.statusMessage.connect(self.logMessage)
        panel.releaseSelected.connect(
            lambda detail: (self.apply_release(detail), dialog.accept()))
        layout.addWidget(panel)
        # Opening with artist/album already filled in *is* the search intent;
        # making the user press Search again is friction. An empty open waits.
        panel.search_on_open()
        dialog.exec()

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.load_button.setEnabled(not running)
        # Both optional: a BatchPanel is built with or without the release
        # lookup (Re-tag has it, Convert does not) and with or without the MP3
        # section (Convert has it, Re-tag does not). Guarding each independently
        # is what lets one panel carry either, both or neither.
        if self.lookup_button is not None:
            self.lookup_button.setEnabled(not running)
        if self.mp3_section is not None:
            self.mp3_section.set_running(running)


class MainWindow(QMainWindow):
    def __init__(self, store=None) -> None:
        super().__init__()
        #: The state database, or None when running without one. Optional on
        #: purpose: state is recoverable, so the window must come up and work
        #: with no store at all -- that is the path tests take, and the path a
        #: user takes if rrf.db cannot be opened.
        self.store = store
        self.setWindowTitle(f"Ripped Record Formatter {__version__}")
        # Default geometry follows the screen rather than a hardcoded 920x760,
        # which squeezed the Full Rip tab (source group + metadata + waveform +
        # track table) into uselessness. Take a generous share of the *available*
        # area -- so a 1080p desktop gets a tall window and a 1600x900 laptop
        # still gets one that fits. The user's own size wins once they set it.
        self.resize(*self._default_window_size())
        self.setAcceptDrops(True)

        self.settings = Settings()
        self.pool = QThreadPool.globalInstance()

        central = QWidget()
        root = QVBoxLayout(central)

        from gui.full_rip import FullRipTab
        from gui.record_tab import RecordTab
        from gui.settings_panel import SettingsPanel

        self.tabs = QTabWidget()
        self.convert_panel = BatchPanel("convert", self.settings)
        self.retag_panel = BatchPanel("retag", self.settings)
        self.full_rip = FullRipTab(self.settings)
        self.settings_panel = SettingsPanel(self.settings)
        # The release cache reaches the lookup wherever it is opened from.
        for surface in (self.convert_panel, self.retag_panel, self.full_rip):
            surface.store = store
        self.record_tab = RecordTab(self.settings)
        self.record_tab.store = store
        self.record_tab.logMessage.connect(self._log)
        self.record_tab.statusMessage.connect(self.set_status)
        # The payoff: a finished side walks straight into Full Rip's mapping table.
        self.record_tab.recordingFinished.connect(self._on_recording_finished)
        self.record_tab.recordingStateChanged.connect(self._on_recording_state)
        self.record_tab.monitoringStateChanged.connect(self._on_monitoring_state)
        # ...and the other end of that flow: the user declares the album done.
        self.record_tab.processAlbumRequested.connect(self._on_process_album_requested)
        # The between-albums clean slate reaches the Record tab's session state too.
        self.full_rip.identityReset.connect(self.record_tab.reset_session)
        # The tab order *is* the pipeline: Record -> Full Rip (tag, restore,
        # split, save) -> the two folder tools -> Settings. The app opens at the
        # beginning of the story rather than in the middle of it, and someone
        # working left to right is following the workflow rather than guessing
        # at it.
        self.tabs.addTab(self.record_tab, "Record")
        self.tabs.addTab(self.full_rip, "Full Rip")
        self.tabs.addTab(self.convert_panel, "Convert")
        self.tabs.addTab(self.retag_panel, "Re-tag")
        self.tabs.addTab(self.settings_panel, "Settings")

        for panel in (self.convert_panel, self.retag_panel):
            panel.logMessage.connect(self._log)
            panel.runRequested.connect(lambda p=panel: self._start_job(p))

        # Export to MP3 lives under the Convert tab: it is the same "turn a
        # folder into another folder" shape, and it is a convenience for devices
        # rather than a mode of its own. Built here because it reads the Full Rip
        # tab's last output; run through the very same worker as a conversion.
        from gui.mp3_export import Mp3ExportSection

        self.mp3_section = Mp3ExportSection(
            self.settings,
            output_root=lambda: (self.settings.config.default_output_dir
                                 or self.convert_panel.output_edit.text().strip()),
            recent_album_dir=lambda: getattr(self.full_rip, "_album_output_root", ""),
            metadata=lambda: (self.convert_panel.artist_edit.text().strip(),
                              self.convert_panel.album_edit.text().strip()),
        )
        self.mp3_section.logMessage.connect(self._log)
        self.mp3_section.exportRequested.connect(self._start_export)
        self.convert_panel.add_export_section(self.mp3_section)

        self.full_rip.logMessage.connect(self._log)
        self.full_rip.statusMessage.connect(self.set_status)
        self.full_rip.openCollectionRequested.connect(self.open_collection)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        #: The Record tab is activated on first show, not here -- see showEvent.
        self._activated_landing_tab = False
        # Leaving Full Rip stops any audition and releases the staged file.
        self.tabs.currentChanged.connect(
            lambda _i: self.full_rip._stop_playback())

        self._last_output_dir: Path | None = None

        # Log pane: compact (~5 lines), collapsible, and it must never reclaim
        # space on a new message -- a QSplitter, not stretch, controls its size.
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        line_h = self.log.fontMetrics().lineSpacing()
        self.log.setMinimumHeight(line_h * 2)
        self._default_log_height = line_h * 4 + 12

        # Above the tabs, because it is about the session rather than any one
        # tab, and dismissible because an offer that cannot be ignored is a
        # demand.
        self.resume_bar = ResumeBar()
        self.resume_bar.resumeRequested.connect(self._resume_session)
        self.resume_bar.discardRequested.connect(self._discard_session)
        root.addWidget(self.resume_bar)

        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.addWidget(self.tabs)
        self._main_splitter.addWidget(self.log)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setCollapsible(0, False)
        self._main_splitter.setCollapsible(1, True)
        self._main_splitter.splitterMoved.connect(self._save_main_split)
        root.addWidget(self._main_splitter, 1)

        # The console becomes an event surface: one plain sentence saying what
        # is happening, with the full history one click away. Nothing is removed
        # from logging -- every line still goes to self.log -- this is about
        # presence, not content.
        self.status_strip = StatusStrip()
        self.status_strip.historyToggled.connect(self.set_log_visible)
        # The standing door to the ledger. This row is already where ambient
        # utilities live, and a collection reachable only in the minutes after a
        # rip finishes would be a notification rather than a ledger.
        self.collection_button = QPushButton("Collection")
        self.collection_button.setFlat(True)
        self.collection_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collection_button.setStyleSheet(
            "QPushButton { border: none; color: palette(link); "
            "text-decoration: underline; padding: 0 6px; }")
        self.collection_button.setToolTip(
            "Which records you have ripped, and which you still mean to.")
        self.collection_button.clicked.connect(self.open_collection)
        self.status_strip.add_action(self.collection_button)
        root.addWidget(self.status_strip)

        # Collapsed by default, and the user's choice is remembered.
        self.set_log_visible(bool(self.settings.config.log_expanded))

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        progress_row.addWidget(self.progress, 1)
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self._open_output_folder)
        progress_row.addWidget(self.open_output_button)
        root.addLayout(progress_row)

        self._restore_main_split()
        self.setCentralWidget(central)
        # Last, so the central widget's size hints are in place and cannot
        # argue with the frame we just restored.
        self._restore_geometry()
        self._log("Ready. Record a side, or open Full Rip if you already have "
                  "the WAVs.")

    # --- metadata wiring ---------------------------------------------------
    def _on_recording_finished(self, result) -> None:
        """Hand the capture to Full Rip, if it is working in the same folder.

        Carries the recording's warnings (dropouts, clipping) forward so a flagged
        capture stays visible when Full Rip admits the side into a live album, and
        the album the Record tab declared, so Full Rip receives files, mapping and
        identity together rather than being told who this is later, elsewhere.

        Reports the outcome back to the Record tab: it needs to know both where
        the side landed (for its saved-summary line) and that *something* landed
        (which is what arms its "process this album" bridge).
        """
        # The strip states the outcome, coloured if the capture had problems --
        # a clipped or dropped side must not read the same as a clean one.
        from core.timefmt import format_timestamp as _fmt
        if result.warnings or result.clipped:
            self.set_status(
                f"Saved {result.path.name} — {_fmt(result.duration)}, "
                "with warnings — see details", status_strip.WARN)
        else:
            self.set_status(f"Saved {result.path.name} — {_fmt(result.duration)}")

        path = result.path
        warnings = list(result.warnings)
        if getattr(result, "clipped", False):
            warnings.append(f"clipping detected ({result.clip_runs} run(s))")

        # Identity travels with the first side, and only fills a vacuum -- a
        # release already chosen in Full Rip is the user's more recent word.
        release = self.record_tab.release
        if release is not None and self.full_rip._release is None:
            self.full_rip._apply_release(release)

        landed = self.full_rip.add_recorded_wav(path, warnings=warnings)
        if landed:
            self._log(f"Recorded side '{Path(path).name}' added to the Full Rip mapping.")
        else:
            self._log(f"Recorded '{Path(path).name}'. Select its folder in Full Rip "
                      "to include it in an album.")
        self.record_tab.note_handoff(
            landed,
            side_label=self.full_rip.mapped_side_label(path) if landed else None,
            album=release.title if release is not None else None,
        )

    def _on_process_album_requested(self) -> None:
        """The record-to-rip bridge: carry the session to Full Rip and stop there.

        Files and mapping are already staged (they arrived side by side); this
        adds identity if the Record tab has it, shows the user where they now are,
        and puts the emphasis on the next thing to press. It never presses it --
        no processing starts that the user did not ask for.
        """
        release = self.record_tab.release
        if release is not None and self.full_rip._release is None:
            self.full_rip._apply_release(release)
        self.tabs.setCurrentWidget(self.full_rip)
        target = self.full_rip.focus_next_action()
        self._log("Ready to process this album in Full Rip. "
                  + ("Press Start album when you're ready."
                     if target is self.full_rip.start_album_btn
                     else "Look up the release first, then press Start album."))

    def _on_recording_state(self, recording: bool) -> None:
        """Recording must be unmissable, whichever tab you are looking at."""
        # Full Rip defers its between-albums reset while a capture is under way.
        self.full_rip.set_recording_active(recording)
        self._recording = recording
        self.setStyleSheet(
            "QTabWidget::pane { border: 2px solid #c0392b; }" if recording else "")
        if not recording:
            # The per-frame "Recording ..." line stops arriving at Stop; without
            # this the strip would sit on the last one forever.
            self.status_strip.set_ready()
        self._refresh_record_state()

    def _on_monitoring_state(self, monitoring: bool) -> None:
        """The monitor runs at app level, so its state belongs to the window.

        A monitor started on the Record tab keeps playing when the user moves to
        another tab -- which means this indicator is the only thing standing
        between them and audio with no visible source.
        """
        self._monitoring = monitoring
        self._refresh_record_state()

    def _refresh_record_state(self) -> None:
        """Title and tab marker for recording and monitoring together.

        Composed in one place because they overlap -- recording while
        monitoring is the normal case, and whichever handler ran last must not
        erase the other's mark.
        """
        recording = getattr(self, "_recording", False)
        monitoring = getattr(self, "_monitoring", False)

        marks = ("●" if recording else "") + ("♪" if monitoring else "")
        index = self.tabs.indexOf(self.record_tab)
        self.tabs.setTabText(index, f"{marks} Record" if marks else "Record")

        suffix = (" — RECORDING" if recording else "") + (
            " — MONITORING" if monitoring else "")
        self.setWindowTitle(f"Ripped Record Formatter {__version__}{suffix}")

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        # Meters run whenever the Record tab is the visible one -- so you can set
        # input gain before you ever press Record.
        self.record_tab.set_active(widget is self.record_tab)

    def _offer_resume_if_interrupted(self) -> None:
        """On launch: was a job still open when we last wrote?

        Only ever an offer. A session that cannot be read, or holds nothing
        unfinished, simply does not produce a bar.
        """
        from core import session_journal

        journal = session_journal.interrupted(self.store)
        if journal is None:
            return
        if session_journal.unfinished_side(journal) is None:
            session_journal.close_all_open(self.store)   # nothing left to offer
            return
        self._pending_journal = journal
        self.resume_bar.offer(session_journal.describe(journal))

    def _resume_session(self) -> None:
        """Hand the journal to Full Rip and say precisely what is happening."""
        from core import session_journal

        journal, self._pending_journal = getattr(self, "_pending_journal", None), None
        if journal is None:
            return
        side = session_journal.unfinished_side(journal) or {}
        wav = side.get("wav") or "its WAV"
        # The precise version, for the audience that wants precision.
        self._log(f"Resume: staging gone, re-analysing {side.get('label', 'the side')} "
                  f"from {Path(wav).name if wav else 'its WAV'}.")
        session_journal.close_all_open(self.store)
        self.tabs.setCurrentWidget(self.full_rip)
        self.full_rip.resume_from_journal(journal)

    def _discard_session(self) -> None:
        from core import session_journal

        self._pending_journal = None
        session_journal.close_all_open(self.store)
        self._log("Resume: discarded. Nothing on disk was touched.")

    def open_collection(self) -> None:
        """Both doors arrive here: the status row, and the album receipt."""
        from gui.collection_view import CollectionDialog

        CollectionDialog(self.store, self).exec()

    def set_log_visible(self, visible: bool) -> None:
        """Open or collapse the full log pane, and remember the choice.

        The strip and the pane are two views of the same stream, so they are
        kept in step here rather than each tracking its own idea of the state.
        """
        self.log.setVisible(bool(visible))
        self.status_strip.set_history_visible(bool(visible))
        self.settings.set(log_expanded=bool(visible))

    def log_visible(self) -> bool:
        return self.log.isVisible()

    def set_status(self, message: str, level: str = status_strip.INFO) -> None:
        """Say what is happening, in one line, on every tab."""
        self.status_strip.set_status(message, level)

    def showEvent(self, event) -> None:
        """Activate the landing tab the first time the window is actually shown.

        The app opens at the story's beginning, and a Record tab shown with its
        meters dormant is the "asleep controls" state the pipeline ordering
        exists to avoid. ``currentChanged`` never fires for the tab that is
        already index 0, so it has to be done explicitly.

        On *show* rather than in the constructor, deliberately: constructing a
        window that is never displayed must not open an input stream. That is
        not a test convenience -- a window with no visible tab has nothing to
        meter, and reaching for the audio device before anyone can see the bars
        is work done for no one.
        """
        super().showEvent(event)
        if not self._activated_landing_tab:
            self._activated_landing_tab = True
            self.record_tab.set_active(self.tabs.currentWidget() is self.record_tab)
            self._offer_resume_if_interrupted()

    def closeEvent(self, event) -> None:
        self._save_geometry()
        # The Record tab owns a COM interface, two PortAudio streams and a
        # timer, and its shutdown() was never actually being called -- all of it
        # was left to interpreter teardown. For the COM pointer in particular
        # that is the launch crash in miniature: comtypes runs CoUninitialize
        # from an atexit handler, so anything finalised after that releases into
        # an apartment that is already gone. Tear it down here, on the GUI
        # thread, while everything is still alive to tear down.
        self.record_tab.shutdown()
        self.full_rip.cleanup()
        super().closeEvent(event)

    # --- job orchestration -------------------------------------------------
    def _start_job(self, panel: BatchPanel) -> None:
        self._run_job(panel.collect_job())

    def _start_export(self) -> None:
        """Run the Convert tab's MP3 export -- same plumbing, different job."""
        self._run_job(self.mp3_section.collect_job())

    def _run_job(self, job) -> None:
        if job is None:
            return
        operation, tracks, output_dir, kwargs = job

        self._last_output_dir = output_dir
        self.open_output_button.setEnabled(False)
        self.convert_panel.set_running(True)
        self.retag_panel.set_running(True)
        self.progress.setMaximum(len(tracks))
        self.progress.setValue(0)
        self._log(f"Starting: {len(tracks)} track(s) -> {output_dir}")
        # The strip echoes the button that was pressed. Saying "Encoding" to
        # someone who clicked "Convert" makes the app sound like it went off and
        # did something else.
        if operation is converter.convert_wavs_to_flacs:
            self._job_verb = "Converting"
        elif operation is mp3_export.export_mp3:
            self._job_verb = "Exporting to MP3"
        else:
            self._job_verb = "Re-tagging"
        self.set_status(f"{self._job_verb} — starting {len(tracks)} track(s)")

        from gui.worker import ConversionWorker

        worker = ConversionWorker(operation, tracks, output_dir, **kwargs)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.log.connect(self._log)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.error.connect(self._on_error)
        self.pool.start(worker)

    def _on_progress(self, current: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        verb = getattr(self, "_job_verb", "Working")
        self.set_status(f"{verb} — {current} of {total} tracks")

    def _on_finished(self, result) -> None:
        self._log(result.summary())
        for warning in result.warnings:
            self._log(f"  ! {warning}")
        # A warning colours the line and stays there. The old behaviour was to
        # write it into a collapsed console, where "it finished, with problems"
        # and "it finished" looked identical.
        if result.warnings:
            n = len(result.warnings)
            self.set_status(f"Finished with {n} warning(s) — see details",
                            status_strip.WARN)
        else:
            self.set_status(result.summary())
        if self._last_output_dir is not None and Path(self._last_output_dir).is_dir():
            self.open_output_button.setEnabled(True)
        self._jobs_done()

    def _open_output_folder(self) -> None:
        if self._last_output_dir is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._last_output_dir))
            )

    def _on_error(self, message: str) -> None:
        self._log(f"ERROR: {message}")
        self.set_status(message, status_strip.ERROR)
        self._jobs_done()

    def _jobs_done(self) -> None:
        self.convert_panel.set_running(False)
        self.retag_panel.set_running(False)

    @staticmethod
    def _default_window_size() -> tuple[int, int]:
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:                       # headless / offscreen
            return 1180, 820
        available = screen.availableGeometry()
        width = min(1280, int(available.width() * 0.72))
        height = min(1000, int(available.height() * 0.92))
        return max(1000, width), max(700, height)

    # --- window geometry persistence ---------------------------------------
    #: A restored window must overlap a screen by at least this fraction of its
    #: own area. A sliver on screen is not "visible": the user cannot reliably
    #: grab a title bar that is 20px wide, and a window they cannot grab is a
    #: window they cannot rescue. Generous enough that a large window on a small
    #: laptop screen still passes (1280x1000 onto a 800x600 desktop is ~38%).
    _MIN_ON_SCREEN_FRACTION = 0.25

    @staticmethod
    def _rect_is_on_a_screen(rect) -> bool:
        """True when *rect* meaningfully overlaps some connected screen.

        Checked against every screen's *available* geometry (so the taskbar does
        not count as somewhere a window may live), because the monitor a window
        was closed on may be gone -- unplugged dock, laptop undocked, projector
        detached -- leaving a saved position that lands in dead space.
        """
        from PySide6.QtGui import QGuiApplication

        area = rect.width() * rect.height()
        if area <= 0:
            return False
        for screen in QGuiApplication.screens():
            overlap = screen.availableGeometry().intersected(rect)
            if overlap.width() * overlap.height() >= area * MainWindow._MIN_ON_SCREEN_FRACTION:
                return True
        return False

    def _center_on_primary(self) -> None:
        """Fall back to a sane default size, centred on the primary screen."""
        from PySide6.QtGui import QGuiApplication

        width, height = self._default_window_size()
        self.resize(width, height)
        screen = QGuiApplication.primaryScreen()
        if screen is None:                       # headless / offscreen
            return
        available = screen.availableGeometry()
        self.move(available.x() + (available.width() - width) // 2,
                  available.y() + (available.height() - height) // 2)

    def _restore_geometry(self) -> None:
        """Restore the saved frame, but only where the user can actually see it."""
        from PySide6.QtCore import QRect

        cfg = self.settings.config
        if cfg.window_w <= 0 or cfg.window_h <= 0:      # nothing saved yet
            self._center_on_primary()
            return

        saved = QRect(cfg.window_x, cfg.window_y, cfg.window_w, cfg.window_h)
        if not self._rect_is_on_a_screen(saved):
            self._center_on_primary()
            return

        self.setGeometry(saved)
        if cfg.window_maximized:
            from PySide6.QtCore import Qt as _Qt

            # setWindowState, not showMaximized(): this runs inside __init__ and
            # showing the window is app.py's call to make, not the constructor's.
            # The state is remembered and applied when show() eventually happens.
            self.setWindowState(self.windowState() | _Qt.WindowState.WindowMaximized)

    def _save_geometry(self) -> None:
        """Record the frame on close, as one write rather than four."""
        maximized = self.isMaximized()
        # A maximized window's geometry() is the maximized rect; saving that and
        # then un-maximizing would strand the user with no smaller size to
        # return to. normalGeometry() is the frame underneath.
        rect = self.normalGeometry() if maximized else self.geometry()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self.settings.set(window_x=rect.x(), window_y=rect.y(),
                          window_w=rect.width(), window_h=rect.height(),
                          window_maximized=maximized)

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)

    # --- splitter persistence ----------------------------------------------
    def _restore_main_split(self) -> None:
        cfg = self.settings.config
        if cfg.main_split_top > 0 and cfg.main_split_bottom > 0:
            self._main_splitter.setSizes([cfg.main_split_top, cfg.main_split_bottom])
        else:
            self._main_splitter.setSizes([10000, self._default_log_height])

    def _save_main_split(self, *_args) -> None:
        sizes = self._main_splitter.sizes()
        if len(sizes) == 2:
            self.settings.set(main_split_top=sizes[0], main_split_bottom=sizes[1])

    # --- drag and drop -----------------------------------------------------
    def _dropped_dir(self, event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).is_dir():
                return local
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_dir(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        folder = self._dropped_dir(event)
        if not folder:
            return
        panel = self.tabs.currentWidget()
        if isinstance(panel, BatchPanel):
            panel.set_source_dir(folder)
            self._log(f"Source folder set from drop: {folder}")
            event.acceptProposedAction()
