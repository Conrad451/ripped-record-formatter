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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import config as core_config
from core import converter
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

        if self.kind == "convert":
            return converter.convert_wavs_to_flacs, tracks, Path(output), {}
        delete = bool(self.delete_check and self.delete_check.isChecked())
        return converter.retag_flacs, tracks, Path(output), {"delete_source": delete}

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.load_button.setEnabled(not running)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ripped Record Formatter")
        self.resize(760, 620)
        self.setAcceptDrops(True)

        self.settings = Settings()
        self.pool = QThreadPool.globalInstance()

        central = QWidget()
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        self.convert_panel = BatchPanel("convert", self.settings)
        self.retag_panel = BatchPanel("retag", self.settings)
        self.tabs.addTab(self.convert_panel, "Convert")
        self.tabs.addTab(self.retag_panel, "Re-tag")
        root.addWidget(self.tabs, 1)

        for panel in (self.convert_panel, self.retag_panel):
            panel.logMessage.connect(self._log)
            panel.runRequested.connect(lambda p=panel: self._start_job(p))

        self._last_output_dir: Path | None = None
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        progress_row.addWidget(self.progress, 1)
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self._open_output_folder)
        progress_row.addWidget(self.open_output_button)
        root.addLayout(progress_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        root.addWidget(self.log, 1)

        self.setCentralWidget(central)
        self._log("Ready. Load files, edit the table, then Convert or Re-tag.")

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

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)

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
