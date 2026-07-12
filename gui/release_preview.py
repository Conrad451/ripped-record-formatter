"""Compact "which release is loaded" summary, shared by Full Rip and the lookup.

The absent-cover state is the point of this widget. Cover art was fetched
silently and its absence only became apparent *after* an album had been encoded
-- by which time the FLACs were already written without a picture. So "this
release has no art" is rendered loudly, in both places a release is chosen:

* the lookup dialog, so it is visible *before* you commit to a release, and
* the Full Rip metadata row, so it stays visible for as long as it is loaded.

Deliberately a thumbnail-plus-three-lines row, not a panel -- the waveform needs
the vertical space more than this does.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

#: The one wording for "this release has no art", used everywhere it is said.
NO_COVER_TEXT = "No cover art on this release"
NO_COVER_HINT = "tracks will be tagged without a picture"
UNREADABLE_COVER_TEXT = "Cover art unreadable"

# Warning colours: this state has to read as a problem, not as a neutral blank.
_WARN_STYLE = (
    "QLabel { border: 1px solid palette(mid); color: palette(mid); "
    "background: palette(alternate-base); }"
)
_LOUD_STYLE = (
    "QLabel { border: 2px dashed #c07000; color: #c07000; "
    "background: palette(alternate-base); font-weight: bold; }"
)


class CoverThumb(QLabel):
    """The cover, or a loud placeholder saying there isn't one."""

    def __init__(self, size: int = 64) -> None:
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.clear_cover()

    def clear_cover(self) -> None:
        self.setPixmap(QPixmap())
        self.setText("")
        self.setStyleSheet(_WARN_STYLE)

    def set_cover(self, cover) -> bool:
        """Show ``cover``; return whether real art was rendered.

        ``cover`` may be ``None`` (the release genuinely has no art) or carry
        bytes that fail to decode. Both end up loud rather than blank.
        """
        if cover is None:
            self.setPixmap(QPixmap())
            # The thumbnail is too small for the full sentence; the row next to
            # it spells it out. Here we just need it to shout.
            self.setText("NO\nART")
            self.setStyleSheet(_LOUD_STYLE)
            return False

        pixmap = QPixmap()
        if not pixmap.loadFromData(cover.data):
            self.setPixmap(QPixmap())
            self.setText("BAD\nART")
            self.setStyleSheet(_LOUD_STYLE)
            return False

        self.setText("")
        self.setStyleSheet(_WARN_STYLE)
        self.setPixmap(
            pixmap.scaled(
                self._size, self._size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        return True


def describe_release(detail) -> tuple[str, str, str]:
    """The three lines: who/what, when/where/format, and how it is laid out."""
    title_line = f"{detail.artist} - {detail.title}"

    formats: list[str] = []
    for medium in detail.media:
        if medium.format and medium.format not in formats:
            formats.append(medium.format)
    bits = [b for b in (detail.year, detail.country, " + ".join(formats)) if b]
    detail_line = "  ".join(bits)

    sides = len(detail.media)
    tracks = detail.track_count
    layout_line = (
        f"{sides} side{'s' if sides != 1 else ''}, "
        f"{tracks} track{'s' if tracks != 1 else ''}"
    )
    return title_line, detail_line, layout_line


class ReleasePreview(QWidget):
    """Thumbnail + three lines. Compact by design."""

    def __init__(self, thumb_size: int = 64) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.thumb = CoverThumb(thumb_size)
        row.addWidget(self.thumb)

        lines = QVBoxLayout()
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(1)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("QLabel { font-weight: bold; }")
        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("QLabel { color: palette(mid); }")
        self.cover_label = QLabel("")
        self.cover_label.setWordWrap(True)
        for label in (self.title_label, self.detail_label, self.cover_label):
            lines.addWidget(label)
        lines.addStretch(1)
        row.addLayout(lines, 1)

        self.clear()

    def clear(self) -> None:
        self.thumb.clear_cover()
        self.title_label.setText("No release selected")
        self.detail_label.setText("Look up a release for titles, durations and cover art.")
        self.cover_label.setText("")
        self.cover_label.setStyleSheet("")
        self.setVisible(False)

    def set_release(self, detail) -> None:
        title_line, detail_line, layout_line = describe_release(detail)
        self.title_label.setText(title_line)

        has_art = self.thumb.set_cover(detail.cover)
        if has_art:
            self.detail_label.setText(f"{detail_line}  |  {layout_line}")
            self.cover_label.setText("")
            self.cover_label.setStyleSheet("")
        else:
            unreadable = detail.cover is not None
            self.detail_label.setText(f"{detail_line}  |  {layout_line}")
            self.cover_label.setText(
                f"{UNREADABLE_COVER_TEXT} - {NO_COVER_HINT}" if unreadable
                else f"{NO_COVER_TEXT} - {NO_COVER_HINT}"
            )
            self.cover_label.setStyleSheet("QLabel { color: #c07000; font-weight: bold; }")
        self.setVisible(True)
