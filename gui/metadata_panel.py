"""A self-contained widget for looking up album metadata online.

:class:`MetadataPanel` is a standalone :class:`QWidget`: type an artist and
album, hit *Search*, pick a pressing from the results table, and the panel
fetches that release's full tracklist and front cover on a background thread and
emits :attr:`MetadataPanel.releaseSelected` carrying a
:class:`core.metadata_lookup.ReleaseDetail`.

It owns no application state and imports nothing from the rest of the GUI, so it
can be exercised on its own::

    python -m gui.metadata_panel

All network work runs on the global :class:`QThreadPool`; the GUI thread only
ever touches already-fetched data, so the UI never blocks on the network.
"""

from __future__ import annotations

import sys
import traceback

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.metadata_lookup import (
    MetadataError,
    MetadataProvider,
    ReleaseDetail,
    ReleaseResult,
)


# ---------------------------------------------------------------------------
# Background tasks -- one QRunnable per network operation.
# ---------------------------------------------------------------------------


class _TaskSignals(QObject):
    done = Signal(object)   # payload depends on the task
    error = Signal(str)


class _CallableTask(QRunnable):
    """Run ``func(*args)`` in a pool thread; emit the result or a message.

    Keeping this generic means the panel spawns the same task type for both the
    search and the detail fetch -- the difference is only which provider method
    is passed in.
    """

    def __init__(self, func, *args):
        super().__init__()
        self._func = func
        self._args = args
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self._func(*self._args)
            self.signals.done.emit(result)
        except MetadataError as exc:
            self.signals.error.emit(str(exc))
        except Exception as exc:  # never let a pool thread crash the app
            self.signals.error.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# The panel.
# ---------------------------------------------------------------------------

_RESULT_COLUMNS = ("Title", "Artist", "Year", "Country", "Format", "Tracks")
_TRACK_COLUMNS = ("Side", "#", "Title", "Length")


class MetadataPanel(QWidget):
    """Search-and-select album metadata, emitting the chosen release.

    :param provider: any :class:`MetadataProvider`. Defaults to lazily creating
        a :class:`~core.metadata_lookup.MusicBrainzProvider` on first search, so
        importing this module never requires the network library.
    """

    #: Emitted with a fully-populated ReleaseDetail once the user picks a result.
    releaseSelected = Signal(object)
    #: Emitted with human-readable progress/error text (host app can log it).
    statusMessage = Signal(str)

    def __init__(self, provider: MetadataProvider | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Album metadata lookup")
        self._provider = provider
        self._pool = QThreadPool.globalInstance()
        self._results: list[ReleaseResult] = []
        self._busy = False

        root = QVBoxLayout(self)

        # --- search form ----------------------------------------------------
        form = QFormLayout()
        self.artist_edit = QLineEdit()
        self.album_edit = QLineEdit()
        self.artist_edit.setPlaceholderText("e.g. Miles Davis")
        self.album_edit.setPlaceholderText("e.g. Kind of Blue")
        self.artist_edit.returnPressed.connect(self._start_search)
        self.album_edit.returnPressed.connect(self._start_search)
        form.addRow("Artist:", self.artist_edit)
        form.addRow("Album:", self.album_edit)
        root.addLayout(form)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._start_search)
        root.addWidget(self.search_button)

        # --- results table --------------------------------------------------
        self.results_table = QTableWidget(0, len(_RESULT_COLUMNS))
        self.results_table.setHorizontalHeaderLabels(_RESULT_COLUMNS)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.results_table.itemSelectionChanged.connect(self._update_choose_enabled)
        self.results_table.doubleClicked.connect(self._start_fetch_detail)
        root.addWidget(self.results_table, 1)

        self.choose_button = QPushButton("Use selected release")
        self.choose_button.setEnabled(False)
        self.choose_button.clicked.connect(self._start_fetch_detail)
        root.addWidget(self.choose_button)

        # --- preview: tracklist + cover ------------------------------------
        preview = QGroupBox("Selected release")
        preview_layout = QHBoxLayout(preview)

        self.track_table = QTableWidget(0, len(_TRACK_COLUMNS))
        self.track_table.setHorizontalHeaderLabels(_TRACK_COLUMNS)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        preview_layout.addWidget(self.track_table, 1)

        self.cover_label = QLabel("No cover")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setFixedWidth(200)
        self.cover_label.setMinimumHeight(200)
        self.cover_label.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        preview_layout.addWidget(self.cover_label)
        root.addWidget(preview, 1)

        # --- status ---------------------------------------------------------
        self.status_label = QLabel("Enter an artist and/or album, then Search.")
        root.addWidget(self.status_label)

    # -- provider ------------------------------------------------------------
    def _get_provider(self) -> MetadataProvider:
        if self._provider is None:
            from core.metadata_lookup import MusicBrainzProvider

            self._provider = MusicBrainzProvider()
        return self._provider

    # -- status helper -------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.statusMessage.emit(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.search_button.setEnabled(not busy)
        self.choose_button.setEnabled(not busy and self._selected_row() is not None)

    # -- search --------------------------------------------------------------
    def _start_search(self) -> None:
        if self._busy:
            return
        artist = self.artist_edit.text().strip()
        album = self.album_edit.text().strip()
        if not artist and not album:
            self._set_status("Type an artist or album to search.")
            return
        self._set_busy(True)
        query = " - ".join(part for part in (artist, album) if part)
        self._set_status(f"Searching for {query!r}...")
        try:
            provider = self._get_provider()
        except Exception as exc:  # provider construction failed (e.g. missing dep)
            self._set_busy(False)
            self._set_status(f"Cannot start lookup: {exc}")
            return
        task = _CallableTask(provider.search_releases, artist, album)
        task.signals.done.connect(self._on_search_done)
        task.signals.error.connect(self._on_error)
        self._pool.start(task)

    def _on_search_done(self, results: list[ReleaseResult]) -> None:
        self._results = results
        self.results_table.setRowCount(0)
        for result in results:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            cells = (
                result.title,
                result.artist,
                result.year,
                result.country,
                result.formats,
                str(result.track_count) if result.track_count else "",
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if result.disambiguation and col == 0:
                    item.setToolTip(result.disambiguation)
                self.results_table.setItem(row, col, item)
        self._set_busy(False)
        if results:
            self._set_status(f"{len(results)} release(s) found. Pick one, then choose it.")
        else:
            self._set_status("No releases matched that search.")

    # -- detail fetch --------------------------------------------------------
    def _selected_row(self) -> int | None:
        rows = self.results_table.selectionModel().selectedRows() if self.results_table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        return row if 0 <= row < len(self._results) else None

    def _update_choose_enabled(self) -> None:
        self.choose_button.setEnabled(not self._busy and self._selected_row() is not None)

    def _start_fetch_detail(self, *args) -> None:
        if self._busy:
            return
        row = self._selected_row()
        if row is None:
            return
        result = self._results[row]
        self._set_busy(True)
        self._set_status(f"Fetching tracklist for {result.title!r}...")
        task = _CallableTask(self._get_provider().get_release, result.release_id)
        task.signals.done.connect(self._on_detail_done)
        task.signals.error.connect(self._on_error)
        self._pool.start(task)

    def _on_detail_done(self, detail: ReleaseDetail) -> None:
        self._populate_tracks(detail)
        self._populate_cover(detail)
        self._set_busy(False)
        art = "with cover" if detail.cover else "no cover art"
        self._set_status(
            f"{detail.title} - {detail.track_count} track(s), {art}. Ready to use."
        )
        self.releaseSelected.emit(detail)

    def _populate_tracks(self, detail: ReleaseDetail) -> None:
        self.track_table.setRowCount(0)
        for medium in detail.media:
            side = str(medium.position) + (f" ({medium.format})" if medium.format else "")
            for track in medium.tracks:
                row = self.track_table.rowCount()
                self.track_table.insertRow(row)
                for col, text in enumerate(
                    (side, track.number, track.title, track.length_display())
                ):
                    self.track_table.setItem(row, col, QTableWidgetItem(text))

    def _populate_cover(self, detail: ReleaseDetail) -> None:
        if not detail.cover:
            self.cover_label.setText("No cover")
            self.cover_label.setPixmap(QPixmap())
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(detail.cover.data):
            self.cover_label.setPixmap(
                pixmap.scaled(
                    self.cover_label.width(),
                    self.cover_label.minimumHeight(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.cover_label.setText("")
        else:
            self.cover_label.setText("Cover unreadable")

    # -- errors --------------------------------------------------------------
    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        # Keep the status line short; full detail (with any traceback) is logged.
        first_line = message.splitlines()[0] if message else "unknown error"
        self._set_status(f"Lookup failed: {first_line}")
        self.statusMessage.emit(message)


def main() -> int:
    """Open the panel on its own for manual testing."""
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    panel = MetadataPanel()
    panel.resize(720, 640)
    panel.releaseSelected.connect(
        lambda detail: print(
            f"[releaseSelected] {detail.artist} - {detail.title}: "
            f"{detail.track_count} tracks, cover={'yes' if detail.cover else 'no'}"
        )
    )
    panel.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
