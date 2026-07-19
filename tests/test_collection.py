"""The collection ledger: a list, reconciled against disk, behind two doors.

The one question it exists to answer is "have I done this one yet?". So the
tests are mostly about the ways that answer could quietly become wrong -- a
folder that moved, a re-rip that duplicated a row, a database trusted over the
filesystem.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core import collection
from core.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rrf.db")
    yield s
    s.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------------- #
def test_a_finished_album_registers_itself(store, tmp_path):
    out = tmp_path / "Discovery"
    out.mkdir()
    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(out), release_mbid="mbid-1")

    entries = collection.entries(store)
    assert len(entries) == 1
    assert entries[0].artist == "Daft Punk"
    assert entries[0].status == collection.RIPPED
    assert entries[0].is_ripped


def test_a_record_you_own_can_be_added_by_hand(store):
    collection.add_wanted(store, artist="Kansas", title="Leftoverture")

    entries = collection.entries(store)
    assert entries[0].status == collection.WANTED
    assert not entries[0].is_ripped


def test_re_ripping_updates_the_row_rather_than_duplicating_it(store, tmp_path):
    """Two rows for one record makes the question harder, not easier."""
    first = tmp_path / "v1"
    first.mkdir()
    second = tmp_path / "v2"
    second.mkdir()

    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(first), release_mbid="mbid-1")
    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(second), release_mbid="mbid-1")

    entries = collection.entries(store)
    assert len(entries) == 1
    assert entries[0].destination == str(second)


def test_ripping_something_already_on_the_wanted_list_promotes_it(store, tmp_path):
    """The gap between the shelf and the library closing is the whole point."""
    out = tmp_path / "Leftoverture"
    out.mkdir()
    collection.add_wanted(store, artist="Kansas", title="Leftoverture")

    collection.register_ripped(store, artist="Kansas", title="Leftoverture",
                               destination=str(out))

    entries = collection.entries(store)
    assert len(entries) == 1
    assert entries[0].status == collection.RIPPED


def test_adding_the_same_wanted_record_twice_is_a_no_op(store):
    collection.add_wanted(store, artist="Kansas", title="Leftoverture")
    collection.add_wanted(store, artist="Kansas", title="Leftoverture")

    assert len(collection.entries(store)) == 1


def test_an_empty_entry_is_refused(store):
    assert collection.add_wanted(store, artist="", title="") is None
    assert collection.entries(store) == []


# --------------------------------------------------------------------------- #
# The filesystem wins
# --------------------------------------------------------------------------- #
def test_a_ripped_album_whose_folder_vanished_reads_as_missing(store, tmp_path):
    """A ledger insisting an album is ripped after its folder moved answers the
    one question it exists for confidently and wrongly."""
    out = tmp_path / "Discovery"
    out.mkdir()
    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(out), release_mbid="mbid-1")
    assert collection.entries(store)[0].status == collection.RIPPED

    out.rmdir()

    entry = collection.entries(store)[0]
    assert entry.status == collection.MISSING
    assert not entry.is_ripped
    # The row is not rewritten -- the folder may come back.
    row = store.read().execute("SELECT status FROM collection").fetchone()
    assert row["status"] == collection.RIPPED


def test_a_folder_that_comes_back_reads_as_ripped_again(store, tmp_path):
    out = tmp_path / "Discovery"
    out.mkdir()
    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(out))
    out.rmdir()
    assert collection.entries(store)[0].status == collection.MISSING

    out.mkdir()
    assert collection.entries(store)[0].status == collection.RIPPED


def test_counts_are_reconciled_too(store, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    collection.register_ripped(store, artist="A", title="One", destination=str(here))
    collection.register_ripped(store, artist="B", title="Two",
                               destination=str(tmp_path / "gone"))
    collection.add_wanted(store, artist="C", title="Three")

    tally = collection.counts(store)

    assert tally[collection.RIPPED] == 1
    assert tally[collection.MISSING] == 1
    assert tally[collection.WANTED] == 1


def test_no_store_is_an_empty_ledger_not_a_crash():
    assert collection.entries(None) == []
    assert collection.register_ripped(None, artist="A", title="B",
                                      destination="x") is None


# --------------------------------------------------------------------------- #
# The two doors
# --------------------------------------------------------------------------- #
def test_the_status_row_carries_a_standing_door(qapp, tmp_path):
    """A ledger reachable only after a rip finishes is a notification."""
    from gui.main_window import MainWindow

    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    window.show()
    qapp.processEvents()

    assert window.collection_button is not None
    assert not window.collection_button.isHidden()
    assert window.collection_button.text() == "Collection"
    window.close()


def test_the_receipt_carries_the_other_door(qapp, tmp_path):
    from types import SimpleNamespace

    from gui.summary_card import AlbumSummaryCard

    card = AlbumSummaryCard()
    summary = SimpleNamespace(sides=(), total=0, total_bytes=0, warnings=(),
                              warned_tracks=0, done=True,
                              describe=lambda: "done")
    opened = []
    card.render(summary, destination=tmp_path, on_open_collection=lambda: opened.append(1))

    assert card.collection_button is not None
    card.collection_button.click()
    assert opened == [1]


def test_no_sixth_tab(qapp, tmp_path):
    """The binding constraint of the placement ruling."""
    from gui.main_window import MainWindow

    window = MainWindow(store=Store(tmp_path / "rrf.db"))
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]

    assert labels == ["Record", "Full Rip", "Convert", "Re-tag", "Settings"]
    assert "Collection" not in labels
    window.close()


# --------------------------------------------------------------------------- #
# The dialog
# --------------------------------------------------------------------------- #
def test_the_dialog_lists_and_summarises(qapp, tmp_path):
    from gui.collection_view import CollectionDialog

    store = Store(tmp_path / "rrf.db")
    here = tmp_path / "here"
    here.mkdir()
    collection.register_ripped(store, artist="Daft Punk", title="Discovery",
                               destination=str(here))
    collection.add_wanted(store, artist="Kansas", title="Leftoverture")

    dialog = CollectionDialog(store)
    try:
        assert dialog.table.rowCount() == 2
        assert "1 ripped" in dialog.summary_label.text()
        assert "1 still to do" in dialog.summary_label.text()
    finally:
        dialog.close()
        store.close()


def test_the_dialog_flags_files_that_are_not_there(qapp, tmp_path):
    from gui.collection_view import CollectionDialog

    store = Store(tmp_path / "rrf.db")
    collection.register_ripped(store, artist="A", title="Gone",
                               destination=str(tmp_path / "vanished"))

    dialog = CollectionDialog(store)
    try:
        assert "files not found" in dialog.summary_label.text()
        assert dialog.table.item(0, 2).text() == "Files not found"
        assert "moved or renamed" in dialog.table.item(0, 2).toolTip()
    finally:
        dialog.close()
        store.close()


def test_adding_from_the_dialog_lands_in_the_ledger(qapp, tmp_path):
    from gui.collection_view import CollectionDialog

    store = Store(tmp_path / "rrf.db")
    dialog = CollectionDialog(store)
    try:
        dialog.artist_edit.setText("Kansas")
        dialog.title_edit.setText("Leftoverture")
        dialog.add_button.click()

        assert dialog.table.rowCount() == 1
        assert collection.entries(store)[0].title == "Leftoverture"
        assert dialog.artist_edit.text() == ""      # ready for the next one
    finally:
        dialog.close()
        store.close()
