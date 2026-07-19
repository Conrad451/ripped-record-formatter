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
    QSplitter,
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
from core.timefmt import format_timestamp
from gui.release_preview import (
    NO_COVER_HINT,
    NO_COVER_TEXT,
    UNREADABLE_COVER_TEXT,
)

_COVER_STYLE = "QLabel { border: 1px solid palette(mid); }"
# A release with no art has to look like a problem, not an empty box.
_NO_COVER_STYLE = (
    "QLabel { border: 2px dashed #c07000; color: #c07000; font-weight: bold; "
    "background: palette(alternate-base); }"
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

    def __init__(self, provider: MetadataProvider | None = None, parent: QWidget | None = None,
                 settings=None, store=None):
        super().__init__(parent)
        #: The state database, for the release cache. Optional: without one
        #: every lookup is simply live, which is how it always worked.
        self._store = store
        self.setWindowTitle("Album metadata lookup")
        self._provider = provider
        self._settings = settings
        self._pool = QThreadPool.globalInstance()
        self._results: list[ReleaseResult] = []
        self._busy = False
        # Highlighting a result previews it; cache so committing to it, or coming
        # back to it, costs no extra round trip (MusicBrainz allows 1 req/s).
        self._detail_cache: dict[str, ReleaseDetail] = {}
        self._preview_id: str | None = None

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

        # --- results table (top of a draggable splitter) -------------------
        self.results_table = QTableWidget(0, len(_RESULT_COLUMNS))
        self.results_table.setHorizontalHeaderLabels(_RESULT_COLUMNS)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.verticalHeader().setDefaultSectionSize(20)  # compact rows
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.results_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.results_table.doubleClicked.connect(self._start_fetch_detail)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.results_table, 1)
        self.choose_button = QPushButton("Use selected release")
        self.choose_button.setEnabled(False)
        self.choose_button.clicked.connect(self._start_fetch_detail)
        top_layout.addWidget(self.choose_button)

        # --- preview: tracklist + cover (bottom of the splitter) -----------
        preview = QGroupBox("Selected release")
        preview_layout = QHBoxLayout(preview)

        self.track_table = QTableWidget(0, len(_TRACK_COLUMNS))
        self.track_table.setHorizontalHeaderLabels(_TRACK_COLUMNS)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.verticalHeader().setDefaultSectionSize(20)  # compact rows
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        preview_layout.addWidget(self.track_table, 1)

        self.cover_label = QLabel("")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setWordWrap(True)
        self.cover_label.setFixedWidth(200)
        self.cover_label.setMinimumHeight(200)
        # The fourth no-art site. A release with nothing in the archive is
        # common; the warning carries the fix rather than ending there.
        self.choose_cover_button = QPushButton("Choose cover image…")
        self.choose_cover_button.setToolTip(
            "Use a JPEG or PNG from your own disk as this release's cover.")
        self.choose_cover_button.clicked.connect(self._choose_cover)
        self.choose_cover_button.setVisible(False)
        self.cover_label.setStyleSheet(_COVER_STYLE)
        preview_layout.addWidget(self.cover_label)
        preview_layout.addWidget(self.choose_cover_button)

        self._split = QSplitter(Qt.Orientation.Vertical)
        self._split.addWidget(top)
        self._split.addWidget(preview)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 1)
        self._split.splitterMoved.connect(self._save_split)
        root.addWidget(self._split, 1)
        self._restore_split()

        # --- status ---------------------------------------------------------
        self.status_label = QLabel("Enter an artist and/or album, then Search.")
        root.addWidget(self.status_label)

    def search_on_open(self) -> bool:
        """Search straight away if the fields were seeded. Returns whether it ran.

        A caller that opens this panel with an artist/album already filled in has
        an intent; re-typing nothing and pressing Search adds a click and no
        information. With both fields empty there is nothing to search for, so we
        wait for input as before.
        """
        if not (self.artist_edit.text().strip() or self.album_edit.text().strip()):
            return False
        self._start_search()
        return True

    # -- splitter persistence ------------------------------------------------
    def _restore_split(self) -> None:
        if self._settings is None:
            return
        cfg = self._settings.config
        if cfg.meta_split_top > 0 and cfg.meta_split_bottom > 0:
            self._split.setSizes([cfg.meta_split_top, cfg.meta_split_bottom])

    def _save_split(self, *_args) -> None:
        if self._settings is None:
            return
        sizes = self._split.sizes()
        if len(sizes) == 2:
            self._settings.set(meta_split_top=sizes[0], meta_split_bottom=sizes[1])

    # -- provider ------------------------------------------------------------
    def _get_provider(self) -> MetadataProvider:
        if self._provider is None:
            from core.metadata_lookup import MusicBrainzProvider

            # The contact identifying this traffic is the user's, from Settings.
            # Without a Settings object we simply have none -- lookups still work.
            contact = (
                self._settings.config.metadata_contact
                if self._settings is not None
                else ""
            )
            provider = MusicBrainzProvider(
                contact=contact,
                notice=self.statusMessage.emit,   # -> the host's log, once
            )
            if self._store is not None:
                # Wrapped, not replaced: a release already fetched is answered
                # from disk, and anything the cache cannot do falls straight
                # through to the live provider underneath.
                from core.release_cache import CachingProvider

                provider = CachingProvider(provider, self._store,
                                           on_log=self.statusMessage.emit)
            self._provider = provider
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

    # -- preview on highlight ------------------------------------------------
    def _on_selection_changed(self) -> None:
        """Fetch and preview the highlighted release -- before it is committed to.

        The preview box used to be filled only by _on_detail_done, which fires on
        "Use selected release" and immediately emits releaseSelected -- and the
        Full Rip host closes the dialog on that signal. So the cover was painted
        into a widget that was already going away: the art was rendered for
        roughly zero frames, and the picker looked as though it never had any.
        Cover presence was undiscoverable at exactly the moment it mattered.

        Now highlighting a row previews it. Fetches are cached per release and
        run on the thread pool; a stale reply (the user moved on) is discarded.
        """
        self._update_choose_enabled()
        row = self._selected_row()
        if row is None:
            return
        release_id = self._results[row].release_id

        cached = self._detail_cache.get(release_id)
        if cached is not None:
            self._show_preview(cached)
            return

        self._preview_id = release_id
        self.track_table.setRowCount(0)
        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("Loading cover...")
        self.cover_label.setStyleSheet(_COVER_STYLE)

        task = _CallableTask(self._get_provider().get_release, release_id)
        task.signals.done.connect(self._on_preview_done)
        task.signals.error.connect(self._on_preview_error)
        self._pool.start(task)

    def _on_preview_done(self, detail: ReleaseDetail) -> None:
        self._detail_cache[detail.release_id] = detail
        if detail.release_id != self._preview_id:
            return                      # the user has moved on; stale reply
        self._show_preview(detail)

    def _on_preview_error(self, message: str) -> None:
        self.cover_label.setPixmap(QPixmap())
        self.cover_label.setText("Could not load preview")
        self.cover_label.setStyleSheet(_NO_COVER_STYLE)
        self._set_status(message)

    def _choose_cover(self) -> None:
        """Attach a cover from disk to the release being previewed.

        Replaces the previewed detail with one carrying the chosen art, so
        committing the release hands the host exactly what is on screen.
        """
        import dataclasses

        from gui.cover_picker import choose_cover_file

        detail = self._detail_cache.get(self._preview_id) if self._preview_id else None
        if detail is None:
            return
        cover, problem = choose_cover_file(self)
        if problem:
            self._set_status(problem)
            return
        if cover is None:
            return
        updated = dataclasses.replace(detail, cover=cover)
        self._detail_cache[self._preview_id] = updated
        self._populate_cover(updated)
        self._set_status(f"{updated.title} - cover art: your own image.")

    def _show_preview(self, detail: ReleaseDetail) -> None:
        self._populate_tracks(detail)
        self._populate_cover(detail)
        art = "with cover art" if detail.cover else NO_COVER_TEXT
        self._set_status(f"{detail.title} - {detail.track_count} track(s), {art}.")

    # -- commit --------------------------------------------------------------
    def _start_fetch_detail(self, *args) -> None:
        if self._busy:
            return
        row = self._selected_row()
        if row is None:
            return
        result = self._results[row]

        # Highlighting already fetched this; commit without a second round trip.
        cached = self._detail_cache.get(result.release_id)
        if cached is not None:
            self._on_detail_done(cached)
            return

        self._set_busy(True)
        self._set_status(f"Fetching tracklist for {result.title!r}...")
        task = _CallableTask(self._get_provider().get_release, result.release_id)
        task.signals.done.connect(self._on_detail_done)
        task.signals.error.connect(self._on_error)
        self._pool.start(task)

    def _on_detail_done(self, detail: ReleaseDetail) -> None:
        self._detail_cache[detail.release_id] = detail
        self._populate_tracks(detail)
        self._populate_cover(detail)
        self._set_busy(False)
        art = "with cover" if detail.cover else "no cover art"
        self._set_status(
            f"{detail.title} - {detail.track_count} track(s), {art}. Ready to use."
        )
        # Carries the cover bytes with it, so the host's preview row shows the
        # same art (or the same loud warning) the dialog just showed.
        self.releaseSelected.emit(detail)

    def _populate_tracks(self, detail: ReleaseDetail) -> None:
        self.track_table.setRowCount(0)
        for medium in detail.media:
            side = str(medium.position) + (f" ({medium.format})" if medium.format else "")
            for track in medium.tracks:
                row = self.track_table.rowCount()
                self.track_table.insertRow(row)
                length = format_timestamp(track.length_ms / 1000) if track.length_ms else ""
                for col, text in enumerate(
                    (side, track.number, track.title, length)
                ):
                    self.track_table.setItem(row, col, QTableWidgetItem(text))

    def _populate_cover(self, detail: ReleaseDetail) -> None:
        """Render the art, or say loudly that there is none.

        This runs when a release is *fetched*, before the user commits to it, so
        a release with no art can be rejected here rather than discovered after
        the FLACs have already been written without a picture.
        """
        if not detail.cover:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText(f"{NO_COVER_TEXT}\n\n({NO_COVER_HINT})")
            self.cover_label.setStyleSheet(_NO_COVER_STYLE)
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
            self.cover_label.setStyleSheet(_COVER_STYLE)
        else:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText(f"{UNREADABLE_COVER_TEXT}\n\n({NO_COVER_HINT})")
            self.cover_label.setStyleSheet(_NO_COVER_STYLE)

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
