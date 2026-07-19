"""The Convert tab's "Export to MP3" section.

A self-contained widget rather than more rows inside :class:`~gui.main_window.BatchPanel`,
for two reasons. It is a genuinely different job -- FLACs in, MP3s out, no track
table, no per-row titles -- so folding it into the panel's WAV->FLAC form would
mean explaining two modes in one grid. And it keeps the whole feature in one
file, so the Convert tab gains a section by *adding* a widget rather than by
rewriting the panel that the Re-tag tab also uses.

The section produces the same ``(operation, items, output_dir, kwargs)`` job
tuple the batch panel does, so the window runs an export through the identical
worker/progress/log plumbing it uses for a conversion -- see
:meth:`Mp3ExportSection.collect_job`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from core import audio_export, export_profiles, mp3_export
from gui.text_styles import apply_muted
from core.tracks import sanitize_filename_component

# The order the quality choices appear in the combo. V0 first: it is the default
# and the one most people should leave alone.
_QUALITY_ORDER = (
    mp3_export.QUALITY_V0,
    mp3_export.QUALITY_320,
    mp3_export.QUALITY_V2,
)


def derived_output_dir(root: str, artist: str, album: str,
                       format_folder: str = "MP3") -> str | None:
    """``{root}\\MP3\\{Artist}\\{Album}``, or ``None`` when it is not derivable.

    "Derivable" means we have a root folder and both an artist and an album that
    survive filename sanitizing. Anything less and we decline to guess -- an
    export landing in ``.../MP3//`` because the album field was blank is worse
    than an empty box the user has to fill in.

    The format level keeps exports visibly apart from the FLAC library under the
    same root -- and apart from *each other*, so an ALAC copy and a WAV copy of
    the same album do not land in one folder. This is a copy for a device and
    should never be mistakable for the library itself.
    """
    if not root:
        return None
    artist_part = sanitize_filename_component(artist)
    album_part = sanitize_filename_component(album)
    if not artist_part or not album_part:
        return None
    return str(Path(root) / (format_folder or "MP3") / artist_part / album_part)


class Mp3ExportSection(QGroupBox):
    """Pick a folder of FLACs, a quality, and a destination; run the export."""

    logMessage = Signal(str)
    exportRequested = Signal()

    def __init__(
        self,
        settings,
        *,
        output_root: Callable[[], str] | None = None,
        recent_album_dir: Callable[[], str] | None = None,
        metadata: Callable[[], tuple[str, str]] | None = None,
    ):
        """``output_root``/``recent_album_dir``/``metadata`` are pull-callbacks.

        The section never reaches into the rest of the window itself: the caller
        supplies three small readers -- where the library root is, where the
        album that just finished was written, and the current (artist, album).
        That keeps this widget testable on its own and means wiring it up does
        not couple it to the Full Rip tab's internals.
        """
        super().__init__("Export a copy (for Apple devices, CD burning, phones)")
        self.settings = settings
        self._output_root = output_root
        self._recent_album_dir = recent_album_dir
        self._metadata = metadata

        layout = QVBoxLayout(self)
        form = QGridLayout()

        # --- source ---------------------------------------------------------
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Folder of FLACs to export")
        form.addWidget(QLabel("FLAC folder:"), 0, 0)
        form.addWidget(self.source_edit, 0, 1)
        form.addWidget(self._browse_button(self.source_edit, "Select FLAC folder"), 0, 2)

        # --- output ----------------------------------------------------------
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Where the MP3s go")
        form.addWidget(QLabel("MP3 folder:"), 1, 0)
        form.addWidget(self.output_edit, 1, 1)
        form.addWidget(self._browse_button(self.output_edit, "Select MP3 folder"), 1, 2)

        # --- format ----------------------------------------------------------
        self.format_combo = QComboBox()
        for profile in export_profiles.PROFILES:
            self.format_combo.addItem(profile.label, profile.key)
        self.format_combo.setCurrentIndex(
            max(0, self.format_combo.findData(export_profiles.DEFAULT_PROFILE)))
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addWidget(QLabel("Format:"), 2, 0)
        form.addWidget(self.format_combo, 2, 1)

        # --- quality (only formats that have a choice) ------------------------
        self.quality_label = QLabel("Quality:")
        self.quality_combo = QComboBox()
        form.addWidget(self.quality_label, 3, 0)
        form.addWidget(self.quality_combo, 3, 1)

        # What the format costs you, said where you choose it rather than
        # discovered afterwards.
        self.caveat_label = QLabel("")
        self.caveat_label.setWordWrap(True)
        apply_muted(self.caveat_label)
        form.addWidget(self.caveat_label, 4, 1, 1, 2)
        layout.addLayout(form)

        # --- actions ---------------------------------------------------------
        actions = QHBoxLayout()
        self.use_album_button = QPushButton("Use the album just finished")
        self.use_album_button.setToolTip(
            "Fill in the folder the last completed album was written to.")
        self.use_album_button.clicked.connect(self.use_recent_album)
        actions.addWidget(self.use_album_button)

        self.suggest_button = QPushButton("Suggest a folder")
        self.suggest_button.setToolTip(
            "Offer {output root}\\MP3\\{Artist}\\{Album} from the fields above.")
        self.suggest_button.clicked.connect(self.suggest_output)
        actions.addWidget(self.suggest_button)

        actions.addStretch(1)
        self.export_button = QPushButton("Export to MP3")
        self.export_button.clicked.connect(self.exportRequested)
        actions.addWidget(self.export_button)
        layout.addLayout(actions)

        # Filling the source in is the moment we know enough to guess a
        # destination, so offer one then -- but never overwrite a path the user
        # has already typed.
        self.source_edit.editingFinished.connect(self._offer_output)

    # --- helpers -------------------------------------------------------------
    def current_profile(self):
        """The chosen export profile."""
        return export_profiles.get(
            self.format_combo.currentData() or export_profiles.DEFAULT_PROFILE)

    def _on_format_changed(self, *_args) -> None:
        """Quality choices, the caveat and the button follow the format."""
        profile = self.current_profile()
        self.quality_combo.clear()
        for key in profile.variants:
            self.quality_combo.addItem(profile.variant_labels.get(key, key), key)
        has_variants = bool(profile.variants)
        self.quality_combo.setVisible(has_variants)
        self.quality_label.setVisible(has_variants)
        self.caveat_label.setText(profile.caveat)
        self.export_button.setText(f"Export to {profile.label}")
        self.output_edit.setPlaceholderText(f"Where the {profile.label} files go")

    def _browse_button(self, target: QLineEdit, caption: str) -> QPushButton:
        button = QPushButton("Browse...")

        def choose() -> None:
            start = target.text().strip() or str(Path.home())
            chosen = QFileDialog.getExistingDirectory(self, caption, start)
            if chosen:
                target.setText(chosen)
                if target is self.source_edit:
                    self._offer_output()

        button.clicked.connect(choose)
        return button

    def quality(self) -> str:
        """The selected quality variant, or "" for formats without one."""
        return self.quality_combo.currentData() or ""

    def _suggestion(self) -> str | None:
        root = self._output_root() if self._output_root else ""
        artist, album = self._metadata() if self._metadata else ("", "")
        return derived_output_dir(root, artist, album,
                                  self.current_profile().label.split(" ")[0])

    def _offer_output(self) -> None:
        """Pre-fill the destination if we can derive one and none is set yet."""
        if self.output_edit.text().strip():
            return
        suggestion = self._suggestion()
        if suggestion:
            self.output_edit.setText(suggestion)

    # --- actions -------------------------------------------------------------
    def suggest_output(self) -> None:
        """Explicitly (re)fill the destination, overwriting what is there."""
        suggestion = self._suggestion()
        if not suggestion:
            self.logMessage.emit(
                "Cannot suggest a folder yet -- set an output folder and "
                "fill in Artist and Album above.")
            return
        self.output_edit.setText(suggestion)
        self.logMessage.emit(f"Export folder set to {suggestion}")

    def use_recent_album(self) -> None:
        """Point the source at the album Full Rip most recently completed."""
        recent = self._recent_album_dir() if self._recent_album_dir else ""
        if not recent or not Path(recent).is_dir():
            self.logMessage.emit(
                "No finished album to export yet -- run a Full Rip first, or "
                "browse to a folder of FLACs.")
            return
        self.source_edit.setText(recent)
        self._offer_output()
        self.logMessage.emit(f"Exporting from the album just finished: {recent}")

    def flac_paths(self) -> list[Path]:
        """The FLACs in the source folder, in filename order."""
        source = self.source_edit.text().strip()
        if not source or not Path(source).is_dir():
            return []
        return sorted(Path(source).glob("*.flac"))

    def collect_job(self):
        """Return ``(operation, flac_paths, output_dir, kwargs)`` or ``None``.

        The same 4-tuple shape :meth:`gui.main_window.BatchPanel.collect_job`
        returns, so the window starts an export exactly as it starts a
        conversion. Emits a log line and returns ``None`` when not ready.
        """
        flacs = self.flac_paths()
        if not flacs:
            source = self.source_edit.text().strip()
            self.logMessage.emit(
                f"No FLACs found in {source!r}." if source
                else "Choose a folder of FLACs to export.")
            return None

        output = self.output_edit.text().strip()
        if not output:
            self.logMessage.emit("Choose a folder for the MP3s.")
            return None

        if Path(output).resolve() == Path(self.source_edit.text().strip()).resolve():
            self.logMessage.emit(
                "The export folder must not be the FLAC folder -- choose a "
                "separate destination so the export cannot sit among the library.")
            return None

        profile = self.current_profile()
        kwargs = {
            "profile": profile.key,
            "variant": self.quality(),
            "max_workers": self.settings.config.encode_workers,
        }
        return audio_export.export_audio, flacs, Path(output), kwargs

    def set_running(self, running: bool) -> None:
        self.export_button.setEnabled(not running)
        self.use_album_button.setEnabled(not running)
