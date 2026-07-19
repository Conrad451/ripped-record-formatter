"""The collection, as a dialog rather than a tab.

The five tabs are the pipeline, in the order the work happens. A ledger is not a
beat in that pipeline -- it is something you consult *about* the pipeline -- so
it does not get a tab, and diluting the Record → Full Rip → Convert → Re-tag
story to hold a list would cost more than the list is worth.

It has two doors. One on the album receipt, where the collection's newest entry
was just born and the thought "have I done the rest of this box yet" actually
occurs. And one standing door in the status row, because a ledger reachable only
in the minutes after a rip finishes is not a ledger -- it is a notification.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core import collection
from gui.text_styles import apply_muted

_STATUS_TEXT = {
    collection.RIPPED: "Ripped",
    collection.WANTED: "Not yet ripped",
    collection.MISSING: "Files not found",
}
_STATUS_COLOUR = {
    collection.RIPPED: "#3aa655",
    collection.MISSING: "#c07000",
}


class CollectionDialog(QDialog):
    """Ripped versus wanted, reconciled against the filesystem on open."""

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Collection")
        self.resize(640, 460)

        root = QVBoxLayout(self)

        self.summary_label = QLabel("")
        root.addWidget(self.summary_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Artist", "Album", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self._open_selected_folder)
        root.addWidget(self.table, 1)

        hint = QLabel(
            "Records are added here automatically when a rip finishes. Add one "
            "by hand for a record you own but have not ripped yet. Double-click "
            "a ripped album to open its folder.")
        hint.setWordWrap(True)
        apply_muted(hint)
        root.addWidget(hint)

        add_row = QHBoxLayout()
        self.artist_edit = QLineEdit()
        self.artist_edit.setPlaceholderText("Artist")
        add_row.addWidget(self.artist_edit, 1)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Album")
        add_row.addWidget(self.title_edit, 1)
        self.add_button = QPushButton("Add to collection")
        self.add_button.clicked.connect(self._add)
        add_row.addWidget(self.add_button)
        root.addLayout(add_row)

        footer = QHBoxLayout()
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove_selected)
        footer.addWidget(self.remove_button)
        footer.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)

        self.refresh()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read and re-reconcile. Cheap, and always current on open."""
        entries = collection.entries(self._store)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            artist = QTableWidgetItem(entry.artist or "—")
            artist.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.table.setItem(row, 0, artist)
            self.table.setItem(row, 1, QTableWidgetItem(entry.title or "—"))

            status = QTableWidgetItem(_STATUS_TEXT.get(entry.status, entry.status))
            colour = _STATUS_COLOUR.get(entry.status)
            if colour:
                from PySide6.QtGui import QBrush, QColor

                status.setForeground(QBrush(QColor(colour)))
            if entry.status == collection.MISSING:
                status.setToolTip(
                    f"This was ripped to {entry.destination}, which is not there "
                    "now. The folder may have been moved or renamed — the files "
                    "on disk are what count, so this is what we can see.")
            elif entry.destination:
                status.setToolTip(entry.destination)
            self.table.setItem(row, 2, status)

        tally = collection.counts(self._store)
        parts = [f"{tally.get(collection.RIPPED, 0)} ripped",
                 f"{tally.get(collection.WANTED, 0)} still to do"]
        if tally.get(collection.MISSING):
            parts.append(f"{tally[collection.MISSING]} with files not found")
        self.summary_label.setText(" · ".join(parts))

    # -- actions ------------------------------------------------------------
    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        return next((e for e in collection.entries(self._store) if e.id == entry_id), None)

    def _add(self) -> None:
        artist = self.artist_edit.text().strip()
        title = self.title_edit.text().strip()
        if not (artist or title):
            return
        collection.add_wanted(self._store, artist=artist, title=title)
        self.artist_edit.clear()
        self.title_edit.clear()
        self.refresh()

    def _remove_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        answer = QMessageBox.question(
            self, "Remove from collection?",
            f"Remove “{entry.display()}” from the list?\n\n"
            "This only removes the entry. No files are deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            collection.remove(self._store, entry.id)
            self.refresh()

    def _open_selected_folder(self, _item=None) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.is_ripped or not entry.destination:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(entry.destination))
