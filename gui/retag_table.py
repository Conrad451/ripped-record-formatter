"""The Re-tag table: every field that will be written, shown and editable.

**The table is the preview of the write.** Re-tag used to show three columns --
number, title, artist -- while writing thirteen Vorbis fields, so most of what
landed in the file was invisible until you opened the result in another program.
Everything editable is here, everything read-only is here, and what you see in a
row is what goes into that file.

Two kinds of column, deliberately distinguished:

* **Editable** -- title, artist, album, album artist, date. Yours to type.
* **Derived or identifying** -- the number (row position), the disc (from the
  sides you defined), and the MusicBrainz IDs. Shown because they are being
  written and you should be able to see that; not editable, because they are
  answers to questions asked elsewhere. An MBID is changed by choosing a
  different release, not by typing over four hex digits.

**Apply to all** exists because the alternative is typing the same album artist
fourteen times. It is offered on every column where flooding is meaningful --
which is every editable column except Title and the number, since those are
per-track by definition and a "make every title the same" gesture is only ever a
mistake. The affordance is a context menu on the cell plus a visible hint in the
tooltip: no invisible affordances.

Scoped to Re-tag rather than unifying with Full Rip's table. That unification is
worth doing, but Full Rip's table is wired into album review -- marker-driven row
counts, accept gating, per-side sync -- and dragging album review into a tagging
change would make both harder to reason about. Flagged as future work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QMenu, QTableView

from core.tracks import Tracks

(COL_NUM, COL_TITLE, COL_ARTIST, COL_ALBUM, COL_ALBUM_ARTIST, COL_DATE,
 COL_DISC, COL_MBID) = range(8)

_HEADERS = ["#", "Title", "Artist", "Album", "Album Artist", "Date", "Disc",
            "MusicBrainz"]

#: Columns a user may type into.
_EDITABLE = {COL_TITLE, COL_ARTIST, COL_ALBUM, COL_ALBUM_ARTIST, COL_DATE}

#: Columns where "apply to all rows" is a sensible gesture. Title and # are
#: per-track by definition; flooding them is only ever a mistake.
_FLOODABLE = {COL_ARTIST, COL_ALBUM, COL_ALBUM_ARTIST, COL_DATE}


@dataclass
class RetagRow:
    """One file, and everything that will be written to it."""

    source_path: Path | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    date: str = ""

    #: Derived from the defined sides; not typed. 1-based, or None for "flat".
    disc_number: int | None = None
    disc_total: int | None = None
    track_number: int | None = None        # position within its side
    track_total: int | None = None         # tracks on that side

    #: Identity, from the release. Displayed read-only.
    mb_album_id: str = ""
    mb_artist_id: str = ""
    mb_recording_id: str = ""
    mb_track_id: str = ""

    def mbid_summary(self) -> str:
        """A short, honest indication that identifiers are being written."""
        present = sum(1 for v in (self.mb_album_id, self.mb_artist_id,
                                  self.mb_recording_id, self.mb_track_id) if v)
        if not present:
            return "—"
        return f"{present}/4 IDs"

    def mbid_detail(self) -> str:
        lines = []
        for label, value in (("Album", self.mb_album_id),
                             ("Artist", self.mb_artist_id),
                             ("Recording", self.mb_recording_id),
                             ("Track", self.mb_track_id)):
            lines.append(f"{label}: {value or '—'}")
        return "\n".join(lines)


class RetagTableModel(QAbstractTableModel):
    """Rows of :class:`RetagRow`, one per file that will be written."""

    #: A cell was flooded down the column. (column, value, rows_changed)
    flooded = Signal(int, str, int)

    def __init__(self, rows: list[RetagRow] | None = None):
        super().__init__()
        self._rows: list[RetagRow] = rows or []

    # -- Qt model interface -------------------------------------------------
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
                # Per-side when sides are defined, positional otherwise -- the
                # number shown is the TRACKNUMBER that will be written.
                return str(row.track_number or index.row() + 1)
            if col == COL_TITLE:
                return row.title
            if col == COL_ARTIST:
                return row.artist
            if col == COL_ALBUM:
                return row.album
            if col == COL_ALBUM_ARTIST:
                return row.album_artist
            if col == COL_DATE:
                return row.date
            if col == COL_DISC:
                if row.disc_number is None:
                    return "—"
                total = f"/{row.disc_total}" if row.disc_total else ""
                return f"{row.disc_number}{total}"
            if col == COL_MBID:
                return row.mbid_summary()

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_MBID:
                return row.mbid_detail()
            if col == COL_DISC:
                return ("Which side this track is on. Set by Define sides, not "
                        "by typing.")
            if col == COL_NUM:
                return ("Track number within its side. Follows the sides you "
                        "define.")
            if col in _FLOODABLE:
                return (f"{_HEADERS[col]} — right-click to apply this value to "
                        "every row.")
            if row.source_path is not None and col == COL_TITLE:
                return str(row.source_path)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if index.column() not in _EDITABLE:
            return False
        row = self._rows[index.row()]
        text = str(value)
        if index.column() == COL_TITLE:
            row.title = text
        elif index.column() == COL_ARTIST:
            row.artist = text
        elif index.column() == COL_ALBUM:
            row.album = text
        elif index.column() == COL_ALBUM_ARTIST:
            row.album_artist = text
        elif index.column() == COL_DATE:
            row.date = text
        self.dataChanged.emit(index, index, [role])
        return True

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() in _EDITABLE:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return None

    # -- apply to all -------------------------------------------------------
    @staticmethod
    def can_flood(column: int) -> bool:
        return column in _FLOODABLE

    def flood_column(self, column: int, value: str) -> int:
        """Set every row's ``column`` to ``value``. Returns rows changed.

        Refuses Title and the number outright rather than quietly doing nothing:
        a caller asking to make every title identical has misunderstood, and the
        model is the place that knows it.
        """
        if column not in _FLOODABLE:
            return 0
        changed = 0
        for row in self._rows:
            current = {
                COL_ARTIST: row.artist, COL_ALBUM: row.album,
                COL_ALBUM_ARTIST: row.album_artist, COL_DATE: row.date,
            }[column]
            if current == value:
                continue
            if column == COL_ARTIST:
                row.artist = value
            elif column == COL_ALBUM:
                row.album = value
            elif column == COL_ALBUM_ARTIST:
                row.album_artist = value
            elif column == COL_DATE:
                row.date = value
            changed += 1
        if changed:
            self.dataChanged.emit(self.index(0, column),
                                  self.index(len(self._rows) - 1, column),
                                  [Qt.ItemDataRole.DisplayRole])
            self.flooded.emit(column, value, changed)
        return changed

    # -- convenience --------------------------------------------------------
    def set_rows(self, rows: list[RetagRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def clear(self) -> None:
        self.set_rows([])

    def rows(self) -> list[RetagRow]:
        return self._rows

    def remove_rows(self, indices: list[int]) -> int:
        removed = 0
        for i in sorted({i for i in indices if 0 <= i < len(self._rows)}, reverse=True):
            self.beginRemoveRows(QModelIndex(), i, i)
            del self._rows[i]
            self.endRemoveRows()
            removed += 1
        if removed and self._rows:
            self._refresh_all()
        return removed

    def _refresh_all(self) -> None:
        self.dataChanged.emit(self.index(0, 0),
                              self.index(len(self._rows) - 1, len(_HEADERS) - 1))

    def paste_titles(self, start_row: int, titles: list[str]) -> tuple[int, int]:
        if start_row < 0:
            start_row = 0
        available = max(0, len(self._rows) - start_row)
        filled = min(available, len(titles))
        for offset in range(filled):
            self._rows[start_row + offset].title = titles[offset]
        if filled:
            self.dataChanged.emit(self.index(start_row, COL_TITLE),
                                  self.index(start_row + filled - 1, COL_TITLE))
        return filled, len(titles) - filled

    def apply_release_fields(self, detail) -> None:
        """Carry a chosen release into every row, track by track, in order.

        Album-level facts (album, album artist, date, release and artist MBIDs)
        go to every row. Track-level facts (per-track artist and its MBIDs, the
        recording and release-track IDs) follow the tracklist by position, which
        is the same ordering assumption the title fill already makes.
        """
        tracks = list(getattr(detail, "tracks", ()) or ())
        for index, row in enumerate(self._rows):
            row.album = detail.title or row.album
            row.album_artist = detail.artist or row.album_artist
            row.date = getattr(detail, "year", "") or row.date
            row.mb_album_id = getattr(detail, "release_id", "") or ""
            release_artist_id = getattr(detail, "artist_id", "") or ""
            info = tracks[index] if index < len(tracks) else None
            if info is not None:
                if getattr(info, "artist", ""):
                    row.artist = info.artist
                elif not row.artist:
                    row.artist = detail.artist or ""
                row.mb_artist_id = getattr(info, "artist_id", "") or release_artist_id
                row.mb_recording_id = getattr(info, "recording_id", "") or ""
                row.mb_track_id = getattr(info, "track_mbid", "") or ""
            else:
                row.mb_artist_id = release_artist_id
        if self._rows:
            self._refresh_all()

    # -- sides --------------------------------------------------------------
    def apply_sides(self, sides) -> None:
        """Assign per-side numbering from ``sides`` (lists of row indices).

        Writes exactly what Full Rip writes: TRACKNUMBER restarts on each side,
        TRACKTOTAL is that side's count, DISCNUMBER is the side's position and
        DISCTOTAL the number of sides -- the Picard vinyl convention. Passing an
        empty list clears it back to flat numbering.
        """
        for row in self._rows:
            row.disc_number = row.disc_total = None
            row.track_number = row.track_total = None
        if sides:
            total_sides = len(sides)
            for position, indices in enumerate(sides, start=1):
                count = len(indices)
                for offset, row_index in enumerate(indices, start=1):
                    if 0 <= row_index < len(self._rows):
                        row = self._rows[row_index]
                        row.disc_number = position
                        row.disc_total = total_sides
                        row.track_number = offset
                        row.track_total = count
        if self._rows:
            self._refresh_all()

    # -- output -------------------------------------------------------------
    def build_tracks(self, *, per_row_artist: bool, default_artist: str,
                     default_album: str, use_side_letters: bool = False) -> list[Tracks]:
        """Turn the visible table into the Tracks that will be written.

        Every field shown in a row lands in that row's file. Rows with no source
        file are skipped; an empty title falls back to the filename stem, which
        is the only piece of information left at that point.
        """
        from gui.side_editor import side_letter

        tracks: list[Tracks] = []
        flat_position = 1
        for row in self._rows:
            if row.source_path is None:
                continue
            title = row.title.strip() or row.source_path.stem
            artist = (row.artist.strip() if per_row_artist and row.artist.strip()
                      else default_artist)
            number = row.track_number or flat_position
            letter = (side_letter(row.disc_number - 1)
                      if (use_side_letters and row.disc_number) else "")
            tracks.append(Tracks(
                number, title, row.album.strip() or default_album, artist,
                row.source_path,
                album_artist=row.album_artist.strip(),
                date=row.date.strip(),
                track_total=row.track_total,
                disc_number=row.disc_number,
                disc_total=row.disc_total,
                mb_album_id=row.mb_album_id,
                mb_artist_id=row.mb_artist_id,
                mb_recording_id=row.mb_recording_id,
                mb_track_id=row.mb_track_id,
                side_letter=letter,
                use_side_letters=bool(use_side_letters and letter),
            ))
            flat_position += 1
        return tracks


class RetagTableView(QTableView):
    """The table, with paste-into-Title, row deletion and apply-to-all."""

    pasted = Signal(int, int)
    rowsDeleted = Signal(int)
    floodRequested = Signal(int, str)      # (column, value)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.verticalHeader().setDefaultSectionSize(20)
        self.verticalHeader().setVisible(False)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_tracklist()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete:
            self._delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        model = self.model()
        index = self.indexAt(event.pos())
        menu = QMenu(self)

        if isinstance(model, RetagTableModel) and index.isValid() \
                and model.can_flood(index.column()):
            value = str(model.data(index, Qt.ItemDataRole.EditRole) or "")
            column_name = _HEADERS[index.column()]
            shown = value if value else "(empty)"
            action = menu.addAction(f'Apply "{shown}" to all rows ({column_name})')
            action.setToolTip("Give every track this value.")
            action.triggered.connect(
                lambda _=False, c=index.column(), v=value: self._flood(c, v))
            menu.addSeparator()

        delete_action = menu.addAction("Delete selected row(s)")
        delete_action.setEnabled(bool(self.selectionModel()
                                      and self.selectionModel().selectedIndexes()))
        delete_action.triggered.connect(self._delete_selected)
        menu.exec(event.globalPos())

    def _flood(self, column: int, value: str) -> None:
        model = self.model()
        if isinstance(model, RetagTableModel):
            model.flood_column(column, value)
        self.floodRequested.emit(column, value)

    def _delete_selected(self) -> None:
        model = self.model()
        if not isinstance(model, RetagTableModel) or self.selectionModel() is None:
            return
        rows = sorted({idx.row() for idx in self.selectionModel().selectedIndexes()})
        if not rows:
            return
        removed = model.remove_rows(rows)
        if removed:
            self.rowsDeleted.emit(removed)

    def _paste_tracklist(self) -> None:
        model = self.model()
        if not isinstance(model, RetagTableModel):
            return
        text = QGuiApplication.clipboard().text()
        titles = [line.strip() for line in text.splitlines() if line.strip()]
        if not titles:
            return
        start = max(0, self.currentIndex().row())
        filled, ignored = model.paste_titles(start, titles)
        self.pasted.emit(filled, ignored)
