"""The finished-album summary card -- a receipt for a completed Full Rip job.

A pure view over an :class:`core.album.AlbumSummary`: cover thumbnail (or the
amber no-art state, reusing :class:`gui.release_preview.CoverThumb`), the
artist/album heading, one line per side (tracks, duration, state -- errored and
cancelled sides in their state colour), the destination as a clickable
open-folder action, the total on disk, and a warnings roll-up shown only when a
track actually carried a warning.

The card holds no controller/job state, so it can sit in the idle review space
without blocking a fresh Start. It renders from data captured at side completion
and never touches the filesystem itself (bar opening the folder on request).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.album import SideState
from core.timefmt import format_size, format_timestamp
from gui.release_preview import CoverThumb

# Errored/cancelled sides shown honestly in their state colour; done is neutral.
# Amber (#c07000) matches the release preview's no-art "loud" state; red for a
# real failure.
_STATE_COLOR = {
    SideState.ERROR: "#c0392b",
    SideState.CANCELLED: "#c07000",
}


def _clear_layout(layout) -> None:
    """Remove and delete every item in ``layout`` (recursively)."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        else:
            child = item.layout()
            if child is not None:
                _clear_layout(child)


class AlbumSummaryCard(QFrame):
    """A dismissable receipt for a finished album, rendered from an AlbumSummary."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("albumSummaryCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._root = QVBoxLayout(self)
        self._destination: Path | None = None
        self._on_dismiss = None
        self._on_rerun = None

        # Rebuilt on every render(); exposed for tests and callers.
        self.title_label: QLabel | None = None
        self.side_labels: list[QLabel] = []
        self.open_button: QPushButton | None = None
        self.dismiss_button: QPushButton | None = None
        self.rerun_button: QPushButton | None = None
        self.warnings_button: QPushButton | None = None
        self.warnings_list: QLabel | None = None

    # -- rendering ----------------------------------------------------------- #
    def render(
        self,
        summary,
        *,
        cover=None,
        artist: str = "",
        album: str = "",
        destination: Path | str | None = None,
        on_dismiss=None,
        on_rerun=None,
    ) -> None:
        """Populate the card from ``summary``. Safe to call repeatedly."""
        self._destination = Path(destination) if destination else None
        self._on_dismiss = on_dismiss
        self._on_rerun = on_rerun
        _clear_layout(self._root)
        self.side_labels = []
        self.restoration_labels = []

        self._root.addLayout(self._build_header(summary, cover, artist, album))
        for side in summary.sides:
            self._root.addWidget(self._side_line(side))
            restoration = self._restoration_line(side)
            if restoration is not None:
                self._root.addWidget(restoration)
        self._root.addLayout(self._build_footer(summary))
        self._build_warnings(summary)

    def _build_header(self, summary, cover, artist, album) -> QHBoxLayout:
        header = QHBoxLayout()

        thumb = CoverThumb(size=64)
        thumb.set_cover(cover)
        header.addWidget(thumb, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        heading = " — ".join(p for p in (artist, album) if p) or "Album complete"
        self.title_label = QLabel(heading)
        self.title_label.setStyleSheet("QLabel { font-weight: bold; font-size: 14px; }")
        self.title_label.setWordWrap(True)
        titles.addWidget(self.title_label)
        subtitle = QLabel(summary.describe())
        subtitle.setStyleSheet("QLabel { color: palette(mid); }")
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)

        # Run again: the one-click do-over for the album you just finished, which
        # is what makes the clean-slate reset safe. Only shown when a callback is
        # wired (the tab has a snapshot to restore).
        self.rerun_button = None
        if self._on_rerun is not None:
            self.rerun_button = QPushButton("Run this album again")
            self.rerun_button.setToolTip(
                "Restore this album's mapping and release so you can run it again.")
            self.rerun_button.clicked.connect(self._rerun)
            header.addWidget(self.rerun_button, 0, Qt.AlignmentFlag.AlignTop)

        self.dismiss_button = QPushButton("×")   # ×
        self.dismiss_button.setFixedSize(24, 24)
        self.dismiss_button.setFlat(True)
        self.dismiss_button.setToolTip("Dismiss")
        self.dismiss_button.clicked.connect(self._dismiss)
        header.addWidget(self.dismiss_button, 0, Qt.AlignmentFlag.AlignTop)
        return header

    def _side_line(self, side) -> QLabel:
        n = side.track_count
        tracks = f"{n} track{'s' if n != 1 else ''}"
        duration = format_timestamp(side.duration_s)
        text = f"{side.label} — {tracks}, {duration} — {side.state.value}"
        label = QLabel(text)
        color = _STATE_COLOR.get(side.state)
        if color is not None:
            label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: bold; }}")
        self.side_labels.append(label)
        return label

    def _restoration_line(self, side) -> QLabel | None:
        """The declick receipt for a side, or ``None`` when there is none to give.

        Deliberately worded in samples, because that is what ffmpeg's adeclick
        actually reports -- "Detected clicks in 1015 of 132300 samples". It is
        not a count of clicks and it is not a quality score: the same audio in
        stereo reports double, since the total sums across channels. Stating the
        denominator alongside keeps the figure honest, and keeps a big-looking
        numerator from reading as a verdict on the record.

        Hidden when the count is zero, absent, or unparsed -- a receipt for
        nothing is noise, and "we could not read it" must never render as "0".
        """
        repaired = getattr(side, "declick_repaired_samples", None)
        total = getattr(side, "declick_total_samples", None)
        if not repaired or not total:
            return None
        pct = 100.0 * repaired / total
        label = QLabel(
            f"    Restoration: {repaired:,} of {total:,} samples declicked ({pct:.2f}%)"
        )
        label.setStyleSheet("QLabel { color: palette(mid); }")
        self.restoration_labels.append(label)
        return label

    def _build_footer(self, summary) -> QHBoxLayout:
        footer = QHBoxLayout()
        self.open_button = QPushButton("Open output folder")
        self.open_button.setEnabled(self._destination is not None)
        self.open_button.clicked.connect(self._open_folder)
        footer.addWidget(self.open_button)

        path_text = str(self._destination) if self._destination else ""
        path_label = QLabel(path_text)
        path_label.setStyleSheet("QLabel { color: palette(mid); }")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        footer.addWidget(path_label, 1)

        total = QLabel(f"{format_size(summary.total_bytes)} on disk")
        footer.addWidget(total, 0, Qt.AlignmentFlag.AlignRight)
        return footer

    def _build_warnings(self, summary) -> None:
        self.warnings_button = None
        self.warnings_list = None
        warned = summary.warned_tracks
        if not warned:
            return
        self.warnings_button = QPushButton(
            f"{warned} track{'s' if warned != 1 else ''} carried warnings — see log"
        )
        self.warnings_button.setCheckable(True)
        self.warnings_button.setStyleSheet("QPushButton { color: #c07000; text-align: left; }")
        self.warnings_button.setToolTip("\n".join(summary.warnings))

        self.warnings_list = QLabel("\n".join(summary.warnings))
        self.warnings_list.setWordWrap(True)
        self.warnings_list.setStyleSheet("QLabel { color: #c07000; }")
        self.warnings_list.setVisible(False)
        self.warnings_button.toggled.connect(self.warnings_list.setVisible)

        self._root.addWidget(self.warnings_button)
        self._root.addWidget(self.warnings_list)

    # -- actions ------------------------------------------------------------- #
    def _open_folder(self) -> None:
        if self._destination is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._destination)))

    def _dismiss(self) -> None:
        self.setVisible(False)
        if self._on_dismiss is not None:
            self._on_dismiss()

    def _rerun(self) -> None:
        if self._on_rerun is not None:
            self._on_rerun()
