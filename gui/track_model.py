"""Editable table model + view for the track list.

The model holds one :class:`Row` per track (title, artist, source file). The
track number is derived from row position, so reordering/removing rows keeps
numbering contiguous. Column 2 (Artist) is shown only when the caller wants it
(soundtrack mode / re-tag); the view hides it otherwise.

The view adds the Discogs-paste workflow: paste a newline-separated tracklist
and it fills the Title column downward from the selected row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QTableView

from core.tracks import Tracks

COL_NUM, COL_TITLE, COL_ARTIST = 0, 1, 2
_HEADERS = ["#", "Title", "Artist"]


@dataclass
class Row:
    title: str = ""
    artist: str = ""
    source_path: Path | None = None


class TrackTableModel(QAbstractTableModel):
    """A small editable table of tracks."""

    def __init__(self, rows: list[Row] | None = None):
        super().__init__()
        self._rows: list[Row] = rows or []

    # --- Qt model interface -------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == COL_NUM:
                return str(index.row() + 1)
            if col == COL_TITLE:
                return row.title
            if col == COL_ARTIST:
                return row.artist
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        row = self._rows[index.row()]
        col = index.column()
        if col == COL_TITLE:
            row.title = str(value)
        elif col == COL_ARTIST:
            row.artist = str(value)
        else:
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() in (COL_TITLE, COL_ARTIST):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return None

    # --- convenience API ----------------------------------------------------
    def set_rows(self, rows: list[Row]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def clear(self) -> None:
        self.set_rows([])

    def rows(self) -> list[Row]:
        return self._rows

    def paste_titles(self, start_row: int, titles: list[str]) -> tuple[int, int]:
        """Fill the Title column from ``start_row`` downward.

        Returns ``(filled, ignored)`` -- how many existing rows were updated and
        how many pasted lines had no row to land in.
        """
        if start_row < 0:
            start_row = 0
        available = max(0, len(self._rows) - start_row)
        filled = min(available, len(titles))
        for offset in range(filled):
            self._rows[start_row + offset].title = titles[offset]
        if filled:
            top = self.index(start_row, COL_TITLE)
            bottom = self.index(start_row + filled - 1, COL_TITLE)
            self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.EditRole])
        return filled, len(titles) - filled

    def build_tracks(self, album: str, default_artist: str,
                     per_row_artist: bool) -> list[Tracks]:
        """Turn rows into :class:`Tracks`.

        ``per_row_artist`` uses each row's Artist cell; otherwise every track
        gets ``default_artist``. Rows without a source file are skipped. Empty
        titles fall back to the source filename stem.
        """
        tracks: list[Tracks] = []
        position = 1
        for row in self._rows:
            if row.source_path is None:
                continue
            title = row.title.strip() or row.source_path.stem
            artist = row.artist if per_row_artist else default_artist
            tracks.append(
                Tracks(position, title, album, artist, row.source_path)
            )
            position += 1
        return tracks


class TrackTableView(QTableView):
    """Table view with a clipboard-paste-into-Title (Discogs) handler."""

    pasted = Signal(int, int)  # (filled, ignored)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_tracklist()
            event.accept()
            return
        super().keyPressEvent(event)

    def _paste_tracklist(self) -> None:
        model = self.model()
        if not isinstance(model, TrackTableModel):
            return
        text = QGuiApplication.clipboard().text()
        titles = [line.strip() for line in text.splitlines() if line.strip()]
        if not titles:
            return
        start = self.currentIndex().row()
        if start < 0:
            start = 0
        filled, ignored = model.paste_titles(start, titles)
        self.pasted.emit(filled, ignored)
