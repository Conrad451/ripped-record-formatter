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
from core.version import __version__
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
        if kind == "convert":
            self.soundtrack_check = QCheckBox("Soundtrack mode (per-track artist)")
            self.soundtrack_check.toggled.connect(self._update_artist_column)
            controls.addWidget(self.soundtrack_check)
        else:
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

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.load_button.setEnabled(not running)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
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
        from gui.metadata_panel import MetadataPanel
        from gui.record_tab import RecordTab
        from gui.settings_panel import SettingsPanel

        self.tabs = QTabWidget()
        self.convert_panel = BatchPanel("convert", self.settings)
        self.retag_panel = BatchPanel("retag", self.settings)
        self.full_rip = FullRipTab(self.settings)
        self.metadata_panel = MetadataPanel(settings=self.settings)
        self.settings_panel = SettingsPanel(self.settings)
        self.record_tab = RecordTab(self.settings)
        self.record_tab.logMessage.connect(self._log)
        # The payoff: a finished side walks straight into Full Rip's mapping table.
        self.record_tab.recordingFinished.connect(self._on_recording_finished)
        self.record_tab.recordingStateChanged.connect(self._on_recording_state)
        # ...and the other end of that flow: the user declares the album done.
        self.record_tab.processAlbumRequested.connect(self._on_process_album_requested)
        # The between-albums clean slate reaches the Record tab's session state too.
        self.full_rip.identityReset.connect(self.record_tab.reset_session)
        self.tabs.addTab(self.full_rip, "Full Rip")
        self.tabs.addTab(self.record_tab, "Record")
        self.tabs.addTab(self.convert_panel, "Convert")
        self.tabs.addTab(self.retag_panel, "Re-tag")
        self.tabs.addTab(self.metadata_panel, "Metadata")
        self.tabs.addTab(self.settings_panel, "Settings")

        for panel in (self.convert_panel, self.retag_panel):
            panel.logMessage.connect(self._log)
            panel.runRequested.connect(lambda p=panel: self._start_job(p))

        self.full_rip.logMessage.connect(self._log)
        self.metadata_panel.statusMessage.connect(self._log)
        self.metadata_panel.releaseSelected.connect(self._on_release_selected)
        self._last_batch_panel = self.convert_panel
        self.tabs.currentChanged.connect(self._on_tab_changed)
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

        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.addWidget(self.tabs)
        self._main_splitter.addWidget(self.log)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setCollapsible(0, False)
        self._main_splitter.setCollapsible(1, True)
        self._main_splitter.splitterMoved.connect(self._save_main_split)
        root.addWidget(self._main_splitter, 1)

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
        self._log("Ready. Full Rip a side, or Convert/Re-tag folders. "
                  "Use Metadata to pull tracklists + cover art.")

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
        index = self.tabs.indexOf(self.record_tab)
        self.tabs.setTabText(index, "● Record" if recording else "Record")
        self.setStyleSheet(
            "QTabWidget::pane { border: 2px solid #c0392b; }" if recording else "")
        self.setWindowTitle(
            f"Ripped Record Formatter {__version__}"
            + (" — RECORDING" if recording else ""))

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        # Meters run whenever the Record tab is the visible one -- so you can set
        # input gain before you ever press Record.
        self.record_tab.set_active(widget is self.record_tab)
        if widget in (self.convert_panel, self.retag_panel):
            self._last_batch_panel = widget

    def _on_release_selected(self, detail) -> None:
        # The standalone Metadata tab feeds the last-used batch panel only;
        # Full Rip has its own embedded lookup (scoped to itself).
        panel = getattr(self, "_last_batch_panel", self.convert_panel)
        panel.apply_release(detail)
        which = "Convert" if panel is self.convert_panel else "Re-tag"
        self._log(f"Release selected: {detail.artist} - {detail.title} "
                  f"({detail.track_count} track(s), cover={'yes' if detail.cover else 'no'}) "
                  f"-> applied to the {which} panel.")

    def closeEvent(self, event) -> None:
        self.full_rip.cleanup()
        super().closeEvent(event)

    # --- job orchestration -------------------------------------------------
    def _start_job(self, panel: BatchPanel) -> None:
        job = panel.collect_job()
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

    def _on_finished(self, result) -> None:
        self._log(result.summary())
        for warning in result.warnings:
            self._log(f"  ! {warning}")
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
