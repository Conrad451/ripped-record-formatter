"""Full Rip tab: one side-long WAV -> restored -> split -> tagged FLAC tracks.

Self-sufficient: it has its own embedded release lookup (a modal seeded with the
current artist/album), so the whole side->tracks flow lives on one tab. A single
restoration pass feeds both the split proposal and the final encode.

The tab reads top-to-bottom in the order the work happens: **Source** (a folder
of side WAVs, one row per WAV, mapped to sides), **Metadata**, **Side**, then the
**Review** area, which stays hidden behind an empty state until a side has been
analysed. There is one way in -- the Source group. A single WAV is just a one-row
mapping table.

Two-step Accept: **Accept splits** cuts the restored WAV and fills an editable
track table; **Encode N tracks** then hands the side (with the edited titles) to
the AlbumController, which encodes+tags+covers it in the background while later
sides are still being analysed. Staging is always cleaned, and no internal
staging path is ever shown in a user-facing field. Every user-facing time is
rendered via :func:`core.timefmt.format_timestamp`.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import job_settings
from core.album import (
    DURATION_MATCH_TOLERANCE,
    AlbumController,
    NeedsAttention,
    SideJob,
    SideState,
    SideSummary,
    guess_side_index,
    measure_outputs,
    probe_duration_ms,
    propose_side_map,
    sides_from_proposal,
)
from core.side_partition import Side
from core.split_review import detect_progressive_drift, segment_deviations, wrong_side_suspected
from core.timefmt import format_timestamp
from core.tracks import safe_part, track_filename
from gui.text_styles import apply_body, apply_muted
from gui.playback import AuditionPlayer
from gui.release_preview import NO_COVER_TEXT, ReleasePreview
from gui.side_editor import SideEditorDialog, side_letter
from gui.summary_card import AlbumSummaryCard
from gui.track_model import Row, TrackTableModel, TrackTableView
from gui.waveform import WaveformView

# Shown in the side dropdown for a WAV that is not part of this album. It is the
# default for anything the heuristics are not confident about.
SKIP_LABEL = "— skip —"

# Mapping-table geometry: 4 rows visible without scrolling (header + 4 * row) --
# enough for a double album plus a stray, while leaving the review area the
# dominant share of the tab.
_MAP_ROW_H = 22
_MAP_TABLE_ROWS = 4
_MAP_TABLE_H = _MAP_ROW_H * _MAP_TABLE_ROWS + 26


class _StateRelay(QObject):
    """Marshals AlbumController state callbacks (worker threads) to the GUI."""

    changed = Signal(object)
    #: The album reached a terminal state. Carries an AlbumSummary. Marshalled
    #: like `changed`, because the last side to finish announces it from whichever
    #: pool thread it happened to be on -- and the handler touches widgets.
    finished = Signal(object)


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
@dataclass
class AnalyzeResult:
    restoration: object
    proposal: object
    envelope: object
    restored_path: Path
    # The actual restoration Stage objects applied to this side's audio, kept so
    # the encode step can stamp provenance (RRF_RESTORATION) from what really ran
    # -- not from config, which may change between analysis and encode. Empty
    # when no stages were enabled (encoded without restoration).
    stages: list = field(default_factory=list)


class _Signals(QObject):
    progress = Signal(str)
    finished = Signal(object)
    error = Signal(str)
    cancelled = Signal()


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #
class FullRipTab(QWidget):
    logMessage = Signal(str)
    #: The between-albums clean slate ran (9.7). Other tabs holding session state
    #: -- the Record tab's declared album -- clear themselves off this.
    identityReset = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.pool = QThreadPool.globalInstance()

        # state
        self._release = None
        #: Has the user typed an output path themselves? A derived suggestion
        #: never overwrites a deliberate choice (9.10).
        self._output_hand_edited = False
        #: The widget the record-to-rip bridge last pointed the user at.
        self._emphasised = None
        self._cover = None
        self._work_dir: Path | None = None
        self._analysis: AnalyzeResult | None = None
        self._flat_titles: list[str] = []
        self._flat_durations_ms: list[int] = []
        self._flat_track_infos: list = []          # TrackInfo per flat track
        self._side_track_infos: dict = {}          # side.index -> [TrackInfo]
        self._total_sides = 0
        self._review_track_infos: list = []
        self._review_side_position = 1
        self._sides: list[Side] = []
        self._expected_n: int | None = None
        self._expected_titles: list[str] = []
        self._expected_durations_s: list[float] = []
        self._unresolved: list = []
        self._gap_idx = 0
        self._busy = False
        self._cancel = threading.Event()
        # Where the file dialogs open. Deliberately NOT a display field: the
        # old 'Side-long WAV' QLineEdit could end up showing an internal
        # staging path (%TEMP%\tmp...\src). Nothing internal is ever shown.
        self._browse_start: str = (settings.config.default_source_dir
                                   or settings.config.source_dir or "")

        # album mode
        self._album: AlbumController | None = None
        self._album_wavs: list[Path] = []
        self._album_mapping: list = []
        # WAVs the user hand-set in the mapping table (path -> chosen side, or None
        # for a deliberate skip). Re-proposal never overrides these.
        self._pinned_map: dict[Path, int | None] = {}
        self._album_work_dir: Path | None = None
        self._album_output_root = ""
        self._album_meta: dict = {}
        self._album_review_index: int | None = None
        #: Set while a scoped single-side re-do is running, so the finish
        #: handler can report a side rather than pretending an album ran, and so
        #: a second job can be refused with a line that names what is in the way.
        self._redoing_side: int | None = None
        # clean-slate-between-albums
        self._recording_active = False       # a capture is under way (told by MainWindow)
        self._reset_deferred = False         # album concluded mid-recording; wait to clear
        self._rerun_snapshot: dict | None = None  # what "Run again" restores
        self._relay = _StateRelay()
        self._relay.changed.connect(self._on_side_state)
        self._relay.finished.connect(self._on_album_finished)

        root = QVBoxLayout(self)

        # The tab reads top-to-bottom in the order the user actually works:
        #   1. Source   -- the folder of WAVs, and which side each one is
        #   2. Metadata -- who made this record
        #   3. Side     -- how the release is carved into sides
        #   4. Review   -- appears once a side has been analysed
        # There is exactly one way in (the Source group). The old standalone
        # "Side-long WAV" browse field and its Analyze/Cancel row were a second,
        # competing entry point; single-WAV use now goes through
        # "Add single WAV..." and a one-row mapping table.

        # --- 1. Source: the primary and only entry point ------------------------
        self.album_box = QGroupBox("Source")
        album_layout = QVBoxLayout(self.album_box)
        pick_row = QHBoxLayout()
        folder_btn = QPushButton("Select folder...")
        folder_btn.clicked.connect(self._album_select_folder)
        wavs_btn = QPushButton("Add single WAV...")
        wavs_btn.clicked.connect(self._album_add_wavs)
        pick_row.addWidget(folder_btn)
        pick_row.addWidget(wavs_btn)
        pick_row.addStretch(1)
        self.start_album_btn = QPushButton("Start album")
        self.start_album_btn.clicked.connect(self._start_album)
        self.cancel_album_btn = QPushButton("Cancel album")
        self.cancel_album_btn.setEnabled(False)
        self.cancel_album_btn.clicked.connect(self._cancel_album)
        # One gesture for two situations: a side that failed, and a side that
        # succeeded but came out wrong. Both mean "do this one again", and
        # having them as separate affordances (one of them disabled all session,
        # which reads as broken) was describing our internals rather than the
        # user's intent.
        self.retry_side_btn = QPushButton("Re-do side")
        self.retry_side_btn.setEnabled(False)
        self.retry_side_btn.setToolTip(
            "Re-run the selected side from its source WAV, back through review.")
        self.retry_side_btn.clicked.connect(self._retry_selected_side)
        pick_row.addWidget(self.start_album_btn)
        pick_row.addWidget(self.retry_side_btn)
        pick_row.addWidget(self.cancel_album_btn)
        album_layout.addLayout(pick_row)

        self.mapping_hint = QLabel(
            "Select the folder containing your ripped WAV files. Assign each file "
            "to a side of the record — files left on “skip” are ignored, so you "
            "can keep multiple albums in one folder and process one at a time."
        )
        self.mapping_hint.setWordWrap(True)
        album_layout.addWidget(self.mapping_hint)

        map_side_row = QHBoxLayout()
        self.mapping_table = QTableWidget(0, 2)
        self.mapping_table.setHorizontalHeaderLabels(["WAV file", "Side"])
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_table.verticalHeader().setDefaultSectionSize(_MAP_ROW_H)
        # ~6 rows visible before it scrolls: enough for a double album's sides
        # plus a couple of strays, without the source group eating the waveform.
        self.mapping_table.setMinimumHeight(_MAP_TABLE_H)
        self.mapping_table.setMaximumHeight(_MAP_TABLE_H)
        map_side_row.addWidget(self.mapping_table, 2)
        self.side_list = QListWidget()
        self.side_list.setMinimumHeight(_MAP_TABLE_H)
        self.side_list.setMaximumHeight(_MAP_TABLE_H)
        self.side_list.itemClicked.connect(self._on_side_list_click)
        self.side_list.currentItemChanged.connect(lambda *_: self._update_retry_enabled())
        map_side_row.addWidget(self.side_list, 1)
        album_layout.addLayout(map_side_row)
        root.addWidget(self.album_box)

        # --- 2. Metadata, 3. Side context, output ------------------------------
        form = QFormLayout()

        meta_row = QHBoxLayout()
        # Real Qt placeholders (greyed, not content). Prefill only when the
        # remembered value is actually something -- an empty string leaves the
        # placeholder showing rather than an empty-looking populated field.
        self.artist_edit = QLineEdit(settings.config.last_artist or "")
        self.artist_edit.setPlaceholderText("Artist")
        self.album_edit = QLineEdit(settings.config.last_album or "")
        self.album_edit.setPlaceholderText("Album")
        meta_row.addWidget(self.artist_edit, 1)
        meta_row.addWidget(self.album_edit, 1)
        self.lookup_button = QPushButton("Look up release...")
        self.lookup_button.clicked.connect(self._open_lookup)
        meta_row.addWidget(self.lookup_button)
        form.addRow("Metadata:", self._wrap(meta_row))

        # Always-visible summary of what is actually loaded. Its job is to make
        # a missing cover obvious *now* rather than after the album is encoded.
        self.release_preview = ReleasePreview()
        form.addRow("", self.release_preview)

        side_row = QHBoxLayout()
        self.side_combo = QComboBox()
        self.side_combo.addItem("Select a release to choose a side...", None)
        self.side_combo.setEnabled(False)
        self.side_combo.currentIndexChanged.connect(self._on_side_changed)
        side_row.addWidget(self.side_combo, 1)
        self.define_sides_button = QPushButton("Define sides...")
        self.define_sides_button.setEnabled(False)
        self.define_sides_button.clicked.connect(self._open_side_editor)
        side_row.addWidget(self.define_sides_button)
        side_row.addWidget(QLabel("or tracks:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 40)
        self.count_spin.setValue(1)
        self.count_spin.valueChanged.connect(self._on_count_changed)
        side_row.addWidget(self.count_spin)
        form.addRow("Side:", self._wrap(side_row))

        self.output_edit = QLineEdit(settings.config.output_dir
                                     or settings.config.default_output_dir)
        # See the Record tab's folder field: textEdited fires only for real
        # keystrokes, which is the line between a chosen path and an offered one.
        self.output_edit.textEdited.connect(
            lambda _t: setattr(self, "_output_hand_edited", True))
        form.addRow("Output folder:", self._path_row(self.output_edit, self._browse_output))

        # The destination the encode will *actually* use. An album job captures
        # the output folder at Start and every side encodes into that captured
        # path -- so while a job runs, this label, not the field above it, is the
        # truth. It is only shown during a run, when the two could disagree.
        self.destination_label = QLabel("")
        self.destination_label.setWordWrap(True)
        self.destination_label.setVisible(False)
        form.addRow("", self.destination_label)
        root.addLayout(form)

        # --- 4. Review: only meaningful once a side is ready --------------------
        # Everything below lives in one widget so it can be swapped for an
        # empty-state label instead of presenting a screenful of dead controls.
        self.empty_state = QLabel("Select a folder to begin.")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_muted(self.empty_state, extra="padding: 24px")
        root.addWidget(self.empty_state, 1)

        self.review_box = QWidget()
        review = QVBoxLayout(self.review_box)
        review.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.review_box, 1)
        self.review_box.setVisible(False)

        # A finished album's receipt, shown in this same idle space. It is a third
        # mutually-exclusive occupant alongside empty_state and review_box; the
        # visibility helpers below keep exactly one showing. It holds no job state,
        # so it never blocks a fresh Start.
        self.summary_card = AlbumSummaryCard()
        root.addWidget(self.summary_card, 1)
        self.summary_card.setVisible(False)

        self.waveform = WaveformView()
        self.waveform.markersChanged.connect(self._on_markers_changed)
        self.waveform.setMinimumHeight(190)
        self.waveform.seekRequested.connect(self._on_seek_requested)
        self.waveform.selectionChanged.connect(self._update_preview_enabled)
        review.addWidget(self.waveform, 3)

        # --- audition transport ------------------------------------------------
        # Splits on gapless material can only be judged by ear. Plays the RESTORED
        # staged WAV -- the audio the cuts actually apply to, never the raw source.
        self.player = AuditionPlayer(self)
        self.player.positionChanged.connect(self.waveform.set_playhead)
        self.player.playingChanged.connect(self._on_playing_changed)
        self.player.errorOccurred.connect(lambda m: self._log(f"Playback: {m}"))

        play_row = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        play_row.addWidget(self.play_btn)
        self.preview_cut_btn = QPushButton("Preview cut")
        self.preview_cut_btn.setEnabled(False)
        self.preview_cut_btn.clicked.connect(self._preview_selected_cut)
        play_row.addWidget(self.preview_cut_btn)
        self.playback_hint = QLabel(
            "Space play/pause · Ctrl+click seeks · click a marker to select it, "
            "arrows nudge it, then Preview cut."
        )
        apply_muted(self.playback_hint)
        play_row.addWidget(self.playback_hint, 1)
        review.addLayout(play_row)

        if not self.player.available:
            reason = self.player.unavailable_reason or "audio unavailable"
            for btn in (self.play_btn, self.preview_cut_btn):
                btn.setEnabled(False)
                btn.setToolTip(f"Playback unavailable: {reason}")
            self.playback_hint.setText(f"Playback unavailable: {reason}")

        # wrong-side diagnosis
        self.diagnosis_box = QGroupBox("Check the selection")
        diag = QHBoxLayout(self.diagnosis_box)
        self.diagnosis_label = QLabel("")
        self.diagnosis_label.setWordWrap(True)
        diag.addWidget(self.diagnosis_label, 1)
        # Single-side routes...
        self.reselect_btn = QPushButton("Re-select side")
        self.reselect_btn.clicked.connect(self._reselect_side)
        diag.addWidget(self.reselect_btn)
        self.resolve_anyway_btn = QPushButton("Resolve manually anyway")
        self.resolve_anyway_btn.clicked.connect(self._resolve_anyway)
        diag.addWidget(self.resolve_anyway_btn)
        # ...and the album equivalents. Same diagnosis, same resolve flow beneath;
        # only the way back out differs (a mapping table, not a side combo).
        self.recheck_mapping_btn = QPushButton("Re-check mapping")
        self.recheck_mapping_btn.clicked.connect(self._recheck_mapping)
        diag.addWidget(self.recheck_mapping_btn)
        self.review_manual_btn = QPushButton("Review and place splits manually")
        self.review_manual_btn.clicked.connect(self._resolve_anyway)
        diag.addWidget(self.review_manual_btn)
        self.diagnosis_box.setVisible(False)
        review.addWidget(self.diagnosis_box)

        # gap guidance
        self.gap_box = QGroupBox("Resolve gap (click on the highlighted region to place a split)")
        gap_layout = QHBoxLayout(self.gap_box)
        self.gap_prompt = QLabel("")
        self.gap_prompt.setWordWrap(True)
        gap_layout.addWidget(self.gap_prompt, 1)
        self.prev_gap_btn = QPushButton("< Prev")
        self.prev_gap_btn.clicked.connect(lambda: self._present_gap(self._gap_idx - 1))
        self.play_window_btn = QPushButton("Play window")
        self.play_window_btn.setToolTip("Hear the segue before placing the split.")
        self.play_window_btn.clicked.connect(self._play_current_window)
        gap_layout.addWidget(self.play_window_btn)
        self.next_gap_btn = QPushButton("Next >")
        self.next_gap_btn.clicked.connect(lambda: self._present_gap(self._gap_idx + 1))
        gap_layout.addWidget(self.prev_gap_btn)
        gap_layout.addWidget(self.next_gap_btn)
        self.gap_box.setVisible(False)
        review.addWidget(self.gap_box)

        ctl = QHBoxLayout()
        add_btn = QPushButton("Add split at centre")
        add_btn.clicked.connect(self.waveform.add_marker_at_center)
        ctl.addWidget(add_btn)
        self.override_check = QCheckBox("Split into fewer tracks anyway")
        self.override_check.toggled.connect(self._update_accept_enabled)
        ctl.addWidget(self.override_check)
        ctl.addStretch(1)
        self.marker_status = QLabel("")
        ctl.addWidget(self.marker_status)
        self.accept_button = QPushButton("Accept splits")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self._accept_splits)
        ctl.addWidget(self.accept_button)
        review.addLayout(ctl)

        self.model = TrackTableModel()
        self.table = TrackTableView()
        self.table.setModel(self.model)
        self.table.setMinimumHeight(110)
        review.addWidget(self.table, 2)

        self.meta_summary = QLabel("")
        self.meta_summary.setWordWrap(True)
        apply_muted(self.meta_summary)
        review.addWidget(self.meta_summary)

        self.progress = QProgressBar()
        root.addWidget(self.progress)

    # -- tiny helpers -------------------------------------------------------
    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _path_row(self, edit: QLineEdit, browse) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("Browse...")
        button.clicked.connect(browse)
        row.addWidget(button)
        return self._wrap(row)

    def _log(self, msg: str) -> None:
        self.logMessage.emit(msg)

    def _browse_output(self) -> None:
        start = self.output_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select output folder", start)
        if chosen:
            self.output_edit.setText(chosen)
            self.settings.set(output_dir=chosen)

    # -- embedded release lookup (scoped to Full Rip) -----------------------
    def _open_lookup(self) -> None:
        from gui.metadata_panel import MetadataPanel

        dialog = QDialog(self)
        dialog.setWindowTitle("Look up release")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        # Pass settings through: the panel reads the user's MusicBrainz contact
        # from it (and remembers this dialog's splitter). Without it, the lookup
        # opened from here would identify itself as having no contact even when
        # one is configured.
        panel = MetadataPanel(settings=self.settings)
        panel.artist_edit.setText(self.artist_edit.text())
        panel.album_edit.setText(self.album_edit.text())
        panel.statusMessage.connect(self._log)
        panel.releaseSelected.connect(lambda detail: (self._apply_release(detail), dialog.accept()))
        layout.addWidget(panel)
        # Clicking "Look up release..." with artist/album already filled in *is*
        # the search intent; making the user press Search again is friction. An
        # empty open still waits for input.
        panel.search_on_open()
        dialog.exec()

    def _apply_release(self, detail) -> None:
        """Apply a release to Full Rip only (never the batch panels)."""
        self._release = detail
        self._cover = detail.cover
        self.artist_edit.setText(detail.artist)
        self.album_edit.setText(detail.title)
        self._flat_titles = [t.title for t in detail.tracks]
        self._flat_durations_ms = [(t.length_ms or 0) for t in detail.tracks]
        self._flat_track_infos = list(detail.tracks)
        # Build sides from the release's own media structure.
        sides: list[Side] = []
        start = 0
        for i, medium in enumerate(detail.media):
            count = len(medium.tracks)
            indices = tuple(range(start, start + count))
            total = sum(self._flat_durations_ms[j] for j in indices)
            sides.append(Side(index=i, track_indices=indices, total_ms=total))
            start += count
        self._set_sides(sides)
        self.define_sides_button.setEnabled(bool(self._flat_titles))
        self.release_preview.set_release(detail)
        self._suggest_output_folder()
        self._log(f"Full Rip: release '{detail.title}' loaded "
                  f"({len(detail.media)} side(s), cover={'yes' if detail.cover else 'no'}).")
        if detail.cover is None:
            self._log(f"  ! {NO_COVER_TEXT} - tracks will be tagged without a picture.")

    def _suggest_output_folder(self) -> None:
        """Offer ``{output root}/{Artist}/{Album}`` -- editable, never forced.

        The configured output root is the trunk every derived path hangs off; the
        folder itself is created at encode time (see :func:`core.converter`), so
        offering one that does not exist yet costs nothing. A path the user typed
        is left strictly alone.
        """
        if self._release is None or self._output_hand_edited:
            return
        root = (self.settings.config.default_output_dir
                or self.settings.config.output_dir)
        if not root:
            return
        self.output_edit.setText(str(
            Path(root) / safe_part(self._release.artist)
            / safe_part(self._release.title)))

    def mapped_side_label(self, path) -> "str | None":
        """Which side a handed-off WAV mapped to, if it mapped at all.

        The Record tab asks this so its post-stop line can say where the capture
        went -- the mapping is decided here, but it has to be visible from there.
        """
        path = Path(path)
        wavs = [Path(w) for w in self._album_wavs]
        if path not in wavs:
            return None
        side = self._album_mapping[wavs.index(path)]
        return None if side is None else f"Side {side_letter(side)}"

    def focus_next_action(self) -> "QWidget | None":
        """Point the user at the natural next step, and press nothing.

        Arriving from the Record tab's bridge, the next action is Look up release
        when this album has no identity yet, and Start album when it does.
        """
        target = self.lookup_button if self._release is None else self.start_album_btn
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        target.setDefault(True)
        self._emphasised = target
        return target

    def _set_sides(self, sides: list[Side]) -> None:
        self._sides = sides
        self._total_sides = len(sides)
        self._side_track_infos = {
            s.index: [self._flat_track_infos[i] for i in s.track_indices
                      if i < len(self._flat_track_infos)]
            for s in sides
        }
        self.side_combo.blockSignals(True)
        self.side_combo.clear()
        for s in sides:
            self.side_combo.addItem(
                f"Side {side_letter(s.index)} - {s.track_count} tracks "
                f"({format_timestamp(s.total_ms / 1000)})", s.index)
        self.side_combo.setEnabled(bool(sides))
        self.side_combo.blockSignals(False)
        if sides:
            self.side_combo.setCurrentIndex(0)
            self._on_side_changed()
        # The side dropdowns in the mapping table only exist once we know the
        # sides, so re-derive the mapping whenever the side structure changes.
        if self._album_wavs:
            self._rebuild_mapping_table()

    def _open_side_editor(self) -> None:
        if not self._flat_titles:
            self._log("Full Rip: look up a release first, then define sides.")
            return
        dialog = SideEditorDialog(self._flat_titles, self._flat_durations_ms, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.sides:
            self._set_sides(dialog.sides)
            self._log(f"Full Rip: sides redefined -> {len(dialog.sides)} side(s).")

    def _on_side_changed(self, *_) -> None:
        if not self._sides:
            return
        idx = self.side_combo.currentIndex()
        if not (0 <= idx < len(self._sides)):
            return
        side = self._sides[idx]
        # Bounds-check: sides can outlive the flat tracklist they were built from
        # (a release replaced by a shorter one, sides defined by hand). _set_sides
        # already guards _side_track_infos this way; these two did not, and an
        # over-long side_index raised IndexError straight out of a signal handler.
        self._expected_titles = [self._flat_titles[i] for i in side.track_indices
                                 if i < len(self._flat_titles)]
        self._expected_n = side.track_count
        durations = [self._flat_durations_ms[i] for i in side.track_indices
                     if i < len(self._flat_durations_ms)]
        self._expected_durations_s = [d / 1000.0 for d in durations] if durations and all(durations) else []
        self._review_track_infos = self._side_track_infos.get(side.index, [])
        self._review_side_position = side.index + 1
        self._warn_single_track()
        self._update_meta_summary()

    def _on_count_changed(self, value: int) -> None:
        if self._release is None:
            self._expected_n = value
            self._expected_titles = []
            self._expected_durations_s = []
            self._warn_single_track()

    def _warn_single_track(self) -> None:
        if self._expected_n == 1:
            self._log("Single track - no splits will be proposed; is this intended?")

    # -- analyze ------------------------------------------------------------
    def _on_analyze_done(self, analysis: AnalyzeResult) -> None:
        self._analysis = analysis
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._set_busy(False)
        self._show_review()

        self.waveform.set_envelope(analysis.envelope)
        proposal = analysis.proposal
        self.waveform.set_markers([p.timestamp for p in proposal.split_points],
                                  [p.confidence for p in proposal.split_points])
        self._log(f"Full Rip: proposal mode='{proposal.mode}', "
                  f"{len(proposal.split_points)} cut(s), {len(proposal.unresolved)} unresolved.")

        res = analysis.restoration
        if res.source_clip_runs > 0:
            self._log(f"Source appears clipped at {res.source_clip_runs} point(s) - "
                      "consider re-ripping at lower gain.")
        if res.peak_gain_db < 0:
            self._log(f"Output attenuated {abs(res.peak_gain_db):.1f} dB to prevent clipping.")
        for w in res.warnings:
            self._log(f"  ! {w}")

        self._check_drift()

        # Wrong-side sanity guard before opening any resolve queue.
        self._unresolved = list(proposal.unresolved)
        if self._expected_n and wrong_side_suspected(
                self._expected_n, len(self._unresolved),
                frac=self.settings.config.wrong_side_frac):
            self._show_wrong_side_diagnosis()
        elif self._unresolved:
            self._begin_gap_resolution()
        else:
            self.gap_box.setVisible(False)
            self.waveform.clear_region()
            self.waveform.set_place_mode(False)
            self.waveform.zoom_full()
        self._update_accept_enabled()

    def _show_wrong_side_diagnosis(self) -> None:
        album = self._album is not None and self._album_review_index is not None
        n = self._expected_n
        confirmed = (n - 1) - len(self._unresolved) if n else 0

        if album:
            self.diagnosis_label.setText(
                f"Expected {n} tracks; only {confirmed} boundaries confirmed. "
                "This can mean the wrong side or release is mapped — or the record "
                "genuinely has gapless transitions."
            )
        else:
            self.diagnosis_label.setText(
                f"Expected {n} tracks but could only confirm {confirmed} boundaries. "
                "This usually means the wrong side or release is selected (the side has "
                "fewer tracks than expected)."
            )
        # Offer the routes that actually exist in the mode you are in.
        self.reselect_btn.setVisible(not album)
        self.resolve_anyway_btn.setVisible(not album)
        self.recheck_mapping_btn.setVisible(album)
        self.review_manual_btn.setVisible(album)

        self.diagnosis_box.setVisible(True)
        self.gap_box.setVisible(False)
        self.waveform.set_place_mode(False)
        self._log("Full Rip: too many gaps unresolved - suspect wrong side/release.")

    def _recheck_mapping(self) -> None:
        """Back to the mapping table, with this side unmapped and awaiting a choice."""
        self.diagnosis_box.setVisible(False)
        index = self._album_review_index
        side = self._album_side(index) if index is not None else None
        label = side.label if side else "the side"

        # Re-set the row that fed this side back to skip, so the user is choosing
        # again rather than staring at the mapping that just went wrong.
        for row, mapped in enumerate(self._album_mapping):
            if mapped == index:
                self._album_mapping[row] = None
                widget = self.mapping_table.cellWidget(row, 1)
                if widget is not None:
                    widget.blockSignals(True)
                    widget.setCurrentIndex(0)          # "— skip —"
                    widget.blockSignals(False)
                break

        self._album_review_index = None
        self._clear_review()
        self.mapping_table.setFocus()
        self._log(f"Album: {label} unmapped - pick the right WAV for it, then "
                  "press 'Re-do side'.")
        self._update_retry_enabled()

    def _reselect_side(self) -> None:
        self.diagnosis_box.setVisible(False)
        if self._sides:
            self.side_combo.setFocus()
        else:
            self._open_lookup()

    def _resolve_anyway(self) -> None:
        self.diagnosis_box.setVisible(False)
        if self._unresolved:
            self._begin_gap_resolution()

    # -- gap resolution -----------------------------------------------------
    def _begin_gap_resolution(self) -> None:
        self._gap_idx = 0
        self.gap_box.setVisible(True)
        self.waveform.set_place_mode(True)
        self._present_gap(0)

    def _present_gap(self, idx: int) -> None:
        if not self._unresolved:
            return
        idx = max(0, min(idx, len(self._unresolved) - 1))
        self._gap_idx = idx
        gap = self._unresolved[idx]
        self.waveform.highlight_region(gap.window_start, gap.window_end)
        self.waveform.zoom_to(gap.window_start, gap.window_end)
        self.prev_gap_btn.setEnabled(idx > 0)
        self.next_gap_btn.setEnabled(idx < len(self._unresolved) - 1)
        self._update_gap_prompt()

    def _update_gap_prompt(self) -> None:
        if not self._unresolved:
            return
        gap = self._unresolved[self._gap_idx]
        needed = (self._expected_n - 1) if self._expected_n else self.waveform.marker_count()
        placed = self.waveform.marker_count()
        self.gap_prompt.setText(
            f"Gap {self._gap_idx + 1}/{len(self._unresolved)}: after track "
            f"{gap.track_index + 1}, expected near {format_timestamp(gap.expected_ts)} - "
            f"place it. ({placed}/{needed} markers placed)")

    # -- markers / accept gating -------------------------------------------
    def _on_markers_changed(self) -> None:
        self._update_gap_prompt()
        self._update_accept_enabled()
        self._soft_hint_deviations()
        if self._album_review_index is not None:
            self._sync_review_table()

    def _sync_review_table(self) -> None:
        """Keep the editable track table sized to the current split count.

        The table is live from the moment a side is ready, so titles/artists can
        be corrected *before* Accept. Moving a marker changes how many tracks
        there are, so rows follow the markers -- edits already made are kept and
        any new row falls back to the release's title for that position.
        """
        n = len(self.waveform.marker_times()) + 1
        existing = self.model.rows()
        default_artist = self._release.artist if self._release else self.artist_edit.text().strip()

        rows = []
        for i in range(n):
            if i < len(existing):
                rows.append(existing[i])                  # keep what the user typed
                continue
            title = self._expected_titles[i] if i < len(self._expected_titles) else f"track_{i + 1:02d}"
            info = self._review_track_infos[i] if i < len(self._review_track_infos) else None
            artist = info.artist if (info and info.artist) else default_artist
            rows.append(Row(title=title, artist=artist, source_path=None))
        self.model.set_rows(rows)

    def _needed_markers(self) -> int | None:
        return (self._expected_n - 1) if self._expected_n else None

    def _update_accept_enabled(self) -> None:
        if self._busy or self._analysis is None or self.diagnosis_box.isVisible():
            self.accept_button.setEnabled(False)
            return
        count = self.waveform.marker_count()
        needed = self._needed_markers()
        ok = True if (self.override_check.isChecked() or needed is None) else count == needed
        self.accept_button.setEnabled(ok)
        self.marker_status.setText(f"{count}/{needed} splits" if needed is not None else f"{count} splits")

    def _segment_durations(self) -> list[float]:
        if self._analysis is None:
            return []
        total = self._analysis.proposal.duration
        cuts = sorted(self.waveform.marker_times())
        bounds = [0.0, *cuts, total]
        return [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]

    def _soft_hint_deviations(self) -> None:
        if not self._expected_durations_s or self._analysis is None:
            self.waveform.highlight_markers(set())
            return
        segs = self._segment_durations()
        flags = segment_deviations(segs, self._expected_durations_s)
        # Highlight the markers bounding any deviating segment.
        highlight: set[int] = set()
        off_segments: list[int] = []
        for i, bad in enumerate(flags):
            if bad:
                off_segments.append(i + 1)
                if i - 1 >= 0:
                    highlight.add(i - 1)
                highlight.add(min(i, self.waveform.marker_count() - 1))
        self.waveform.highlight_markers({h for h in highlight if h >= 0})
        self.marker_status.setToolTip(
            ("Segments far from expected length: " + ", ".join(map(str, off_segments)))
            if off_segments else "")

    def _check_drift(self) -> None:
        if not self._expected_durations_s:
            return
        if detect_progressive_drift(self._segment_durations(), self._expected_durations_s):
            self._log("Splits drift from the tracklist - wrong side selected?")

    # -- accept -------------------------------------------------------------
    def _accept_splits(self) -> None:
        """Accept the reviewed side.

        Asymmetry worth naming: everything in this tab is an album job now (a
        lone WAV is a one-row mapping table), so Accept both cuts *and* encodes,
        and there is no separate Encode button. The two-step Accept->Encode that
        the old standalone single-WAV entry point had went away with that entry
        point in the Full Rip consolidation; the Convert/Re-tag tabs still own
        the "I already have per-track files" workflow.
        """
        if self._analysis is None:
            return
        if self._album is not None and self._album_review_index is not None:
            self._accept_album_side()

    def _on_error(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self._set_busy(False)
        self._log(f"ERROR: {message}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.start_album_btn.setEnabled(not busy and self._album is None)
        if busy:
            self.accept_button.setEnabled(False)
        else:
            self._update_accept_enabled()

    # -- review area visibility ---------------------------------------------
    def _show_review(self) -> None:
        """Swap the empty state out for the review controls."""
        self.empty_state.setVisible(False)
        self.summary_card.setVisible(False)
        self.review_box.setVisible(True)

    def _set_empty_state(self, message: str) -> None:
        """Hide the review controls behind an explanatory line.

        Dead, greyed-out controls tell the user nothing; a sentence saying what
        the tab is waiting for tells them what to do next.
        """
        self.summary_card.setVisible(False)
        self.empty_state.setText(message)
        self.empty_state.setVisible(True)
        self.review_box.setVisible(False)

    def _clear_review(self) -> None:
        """Return the review area to its empty state after a side is handed off."""
        self._stop_playback()
        self._analysis = None
        self.model.set_rows([])
        self.waveform.clear_markers(emit=False)
        self.waveform.clear_region()
        self.waveform.set_place_mode(False)
        self.gap_box.setVisible(False)
        self.diagnosis_box.setVisible(False)
        self.accept_button.setText("Accept splits")
        self.accept_button.setEnabled(False)
        self._set_empty_state(self._pending_review_message())

    def _pending_review_message(self) -> str:
        if self._album is None:
            if not self._album_wavs:
                return "Select a folder to begin."
            return "Map each WAV to a side, then press Start album."
        states = [s.state for s in self._album.sides]
        if any(s == SideState.ANALYZING for s in states):
            return "Side analyzing..."
        if any(s == SideState.READY for s in states):
            return "A side is ready - pick it from the list to review its splits."
        if any(s == SideState.NEEDS_ATTENTION for s in states):
            flagged = ", ".join(x.label for x in self._album.sides
                                if x.state == SideState.NEEDS_ATTENTION)
            return (f"{flagged} needs attention - click it to see why and choose "
                    "how to resolve it.")
        done = all(s in (SideState.DONE, SideState.ERROR, SideState.CANCELLED) for s in states)
        if any(s == SideState.ERROR for s in states):
            failed = ", ".join(x.label for x in self._album.sides
                               if x.state == SideState.ERROR)
            head = "Album finished." if done else "Waiting for the next side..."
            return (f"{head}\n\n{failed} failed - select it in the list to see why, "
                    "then press 'Retry side'.")
        if done:
            return "Album finished."
        return "Waiting for the next side..."

    # -- album mode ---------------------------------------------------------
    def _album_select_folder(self) -> None:
        """The primary entry point: a folder is what the user actually has."""
        start = self._browse_start or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select the folder holding this record's WAVs", start)
        if folder:
            self._album_wavs = sorted(Path(folder).glob("*.wav"))
            self._pinned_map = {}          # a new source folder is a fresh slate
            self._rebuild_mapping_table()
            self._log(f"Source: {len(self._album_wavs)} WAV(s) found in {folder}")

    def _album_add_wavs(self) -> None:
        """Secondary affordance -- a single WAV is just a one-row mapping table."""
        start = self._browse_start or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Select a side WAV", start, "WAV files (*.wav)")
        if path:
            self._album_wavs = [Path(path)]
            self._pinned_map = {}
            self._rebuild_mapping_table()
            self._log(f"Source: {Path(path).name}")

    def add_recorded_wav(self, path, *, warnings=()) -> bool:
        """The record-to-rip handoff (see :meth:`_adopt_recorded_wav`), plus the
        clean-slate deferral resolution.

        If an album concluded while this recording was still in progress, its
        landing decides the deferred reset: a WAV that joins *this* album (same
        folder) cancels the reset -- the user is still building this record -- while
        one that lands elsewhere lets the reset run now (the album is truly done).
        """
        adopted = self._adopt_recorded_wav(path, warnings=warnings)
        if self._reset_deferred:
            self._reset_deferred = False
            if not adopted:
                self._reset_identity()
        return adopted

    def _adopt_recorded_wav(self, path, *, warnings=()) -> bool:
        """A recording just landed. Fold it into the mapping table if it belongs.

        This is the record-to-rip handoff. If the Record tab is writing into the
        same folder this tab is pointed at, a finished side appears here on its
        own -- record side A, flip, record side B, and the album job is mapped
        without the user touching the mapping table at all.

        And if an album is *already running* (side A analysing while B records),
        the newly-mapped side is admitted straight into that job's analysis queue
        rather than waiting for a restart -- the record-to-analyse pipeline. A job
        that has already concluded is left concluded (map only; re-run to include
        it), and with no job running this only maps, never auto-starting one.

        ``warnings`` are the completed recording's warnings (dropouts, clipping);
        they never block admission, but they are carried forward into the side's
        log line so a flagged capture stays visible after the handoff.

        Returns whether the file was adopted. A recording into some *other*
        folder is none of this tab's business and is left alone.
        """
        path = Path(path)
        if not path.exists():
            return False
        if any(Path(w) == path for w in self._album_wavs):
            return True                                # already listed

        # Only adopt it if it is in the folder this tab is already working from.
        # With nothing loaded there is no folder to disagree with, so the first
        # recording of a record-first session sets it -- otherwise that session
        # never lands anywhere and Stop is a dead end (9.10). An album already in
        # progress is unaffected: this only fires when the table is empty.
        folder = self._album_wavs[0].parent if self._album_wavs else path.parent
        if path.parent != folder:
            return False

        self._album_wavs = sorted({*self._album_wavs, path})
        self._rebuild_mapping_table()
        mapped = self._album_mapping[self._album_wavs.index(path)]
        warn_note = (f" Heads up: this recording carried {len(warnings)} "
                     f"warning(s) -- see the log." if warnings else "")

        # Live session: a job is running and this side mapped. Try to admit it.
        if self._album is not None and mapped is not None:
            if self._admit_recorded_side(mapped, path):
                self._log(f"Full Rip: '{path.name}' added as Side "
                          f"{side_letter(mapped)} and joined the album that's "
                          f"running — the app is working on it now.{warn_note}")
            else:
                # The job already concluded (8.4): finished is finished.
                self._log(f"Full Rip: '{path.name}' added as Side "
                          f"{side_letter(mapped)}. That album already finished — "
                          f"press Start album to run it again with this side "
                          f"included.{warn_note}")
            return True

        # No running job: map only, and never auto-start one the user didn't.
        where = (f"mapped to Side {side_letter(mapped)} -- press Start album when "
                 "ready" if mapped is not None
                 else "added, left on skip (name it SideX.wav to auto-map)")
        self._log(f"Full Rip: recording '{path.name}' {where}.{warn_note}")
        return True

    def _admit_recorded_side(self, side_index: int, wav_path) -> bool:
        """Build a SideJob for a just-recorded side and admit it to the live job.

        Mirrors the SideJob the initial :meth:`_start_album` builds (titles and
        durations pulled from the flat release lists by the side's track_indices),
        adds it to the side list so its queued->analysing transitions show, then
        admits it. If the controller refuses (the album has concluded), the list
        row is undone and this returns False.
        """
        spec = next((s for s in self._sides if s.index == side_index), None)
        if spec is None or self._album is None:
            return False
        titles = [self._flat_titles[i] for i in spec.track_indices] if self._flat_titles else []
        durations = ([self._flat_durations_ms[i] for i in spec.track_indices]
                     if self._flat_durations_ms else [])
        side = SideJob(index=side_index, label=f"Side {side_letter(side_index)}",
                       wav_path=Path(wav_path), titles=titles, durations_ms=durations)

        item = QListWidgetItem(f"{side.label} - {side.state.value}")
        item.setData(Qt.ItemDataRole.UserRole, side.index)
        self.side_list.addItem(item)
        if self._album.admit_side(side):
            return True
        self.side_list.takeItem(self.side_list.row(item))   # concluded: undo the row
        return False

    def _rebuild_mapping_table(self) -> None:
        """One row per scanned WAV; re-run the mapping proposal.

        The confidence ladder (:func:`core.album.propose_side_map`) pre-fills only
        what is knowable -- an explicit side name, an unambiguous count-and-order,
        or a clear duration match -- and leaves the rest on skip, which keeps a
        mixed folder safe. Hand-set rows (including a deliberate skip) are locked
        and never overwritten by a re-proposal. Runs on every change to the WAV
        list or the side structure, so a release looked up *after* scanning still
        gets matched.
        """
        self.mapping_table.setRowCount(len(self._album_wavs))
        if not self._album_wavs:
            self._album_mapping = []
            return

        num_sides = len(self._sides)
        durations = None
        auto: list[int] = []
        if not num_sides:
            self._album_mapping = [None] * len(self._album_wavs)
        else:
            current, locked = [], set()
            for i, wav in enumerate(self._album_wavs):
                if wav in self._pinned_map:
                    current.append(self._pinned_map[wav])
                    locked.add(i)
                else:
                    current.append(None)
            durations = [probe_duration_ms(w) for w in self._album_wavs]
            totals = [0] * num_sides
            for s in self._sides:
                if 0 <= s.index < num_sides:
                    totals[s.index] = s.total_ms
            self._album_mapping = propose_side_map(
                [w.name for w in self._album_wavs], num_sides,
                current=current, locked=locked,
                wav_durations_ms=durations, side_totals_ms=totals)
            auto = [i for i in range(len(self._album_wavs))
                    if i not in locked and current[i] is None
                    and self._album_mapping[i] is not None]

        for row, wav in enumerate(self._album_wavs):
            self.mapping_table.setItem(row, 0, QTableWidgetItem(wav.name))
            combo = QComboBox()
            combo.addItem(SKIP_LABEL, None)
            for s in self._sides:
                combo.addItem(f"Side {side_letter(s.index)} ({s.track_count} tr)", s.index)
            want = self._album_mapping[row]
            combo.setCurrentIndex(0 if want is None else max(0, combo.findData(want)))
            combo.setToolTip(self._mapping_tooltip(row, want, durations))
            combo.currentIndexChanged.connect(
                lambda _i, r=row, c=combo: self._mapping_changed(r, c))
            self.mapping_table.setCellWidget(row, 1, combo)

        if not num_sides:
            self._log("Source: look up a release (or Define sides) to choose sides.")
            return
        if auto:
            self._log(f"Mapped {len(auto)} WAV(s) to sides automatically.")
        if self._analysis is None:
            self._set_empty_state(self._pending_review_message())

    def _mapping_tooltip(self, row: int, side_index, durations) -> str:
        """Explain a duration-matched row ('Duration matches Side B: 22:41 ≈ 22:35').

        Only for rows the *filename* did not name -- a name match speaks for itself.
        """
        if side_index is None or not durations:
            return ""
        wav = self._album_wavs[row]
        if guess_side_index(wav.name) is not None:
            return ""
        dur = durations[row]
        side = next((s for s in self._sides if s.index == side_index), None)
        if side and dur and side.total_ms and \
                abs(dur - side.total_ms) <= DURATION_MATCH_TOLERANCE * side.total_ms:
            return (f"Duration matches Side {side_letter(side_index)}: "
                    f"{format_timestamp(dur / 1000)} ≈ {format_timestamp(side.total_ms / 1000)}")
        return ""

    def _mapping_changed(self, row: int, combo: QComboBox) -> None:
        side_index = combo.currentData()
        self._album_mapping[row] = side_index
        # Remember the user's explicit choice (a side, or a deliberate skip) so a
        # later re-proposal never overrides it.
        self._pinned_map[self._album_wavs[row]] = side_index
        if side_index is None:
            return
        # A side holds exactly one WAV: if another row already claimed this side,
        # release it rather than silently building an ambiguous job.
        for other in range(len(self._album_mapping)):
            if other != row and self._album_mapping[other] == side_index:
                self._album_mapping[other] = None
                # Displaced, not chosen: it goes back to being auto-mappable.
                self._pinned_map.pop(self._album_wavs[other], None)
                widget = self.mapping_table.cellWidget(other, 1)
                if widget is not None:
                    widget.blockSignals(True)
                    widget.setCurrentIndex(0)
                    widget.blockSignals(False)
                self._log(f"Source: Side {side_letter(side_index)} reassigned; "
                          f"'{self._album_wavs[other].name}' set back to skip.")

    def _album_side(self, index: int):
        return next((s for s in self._album.sides if s.index == index), None) if self._album else None

    def _start_album(self) -> None:
        """Start a *fresh* job. Pressing this again after one finishes re-runs it.

        Re-running is deliberately start-over: every mapped side is analysed
        again from its WAV, including sides that were done last time. The WAVs may
        have been re-recorded between runs and we assume nothing about them, so
        there is no partial re-run and no per-side re-encode -- one button, one
        meaning.
        """
        if self._album is not None:
            if self._redoing_side is not None:
                self._log(
                    f"Album: finish or cancel the Side "
                    f"{side_letter(self._redoing_side)} re-do first — there is "
                    "one review area, so one job at a time.")
            else:
                self._log("Album: already running.")
            return
        if not self._sides:
            self._log("Album: no sides. Look up a release or Define sides.")
            return
        output = self.output_edit.text().strip()
        if not output:
            self._log("Album: choose an output folder first.")
            return
        # Skipped rows are excluded outright -- they never become sides, so a
        # folder holding a second album's WAVs costs nothing.
        mapped = sides_from_proposal(self._album_wavs, self._album_mapping)
        if not mapped:
            self._log("Album: map at least one WAV to a side first "
                      "(everything is on skip).")
            return
        cfg = self.settings.config

        # Refuse to overwrite silently. Asked once per job, album-level: a
        # 12-track prompt-per-file is not a safety feature, it is an ordeal.
        if not self._confirm_overwrite(Path(output)):
            return

        # A fresh job starts un-cancelled, even if the previous one was cancelled.
        self._cancel.clear()
        self.summary_card.setVisible(False)   # the previous run's receipt steps aside
        self._album_output_root = output
        self._album_meta = {
            "artist": self._release.artist if self._release else cfg.last_artist,
            "album": self._release.title if self._release else cfg.last_album,
        }
        # A re-run gets its own staging. Drop the previous run's, or a long
        # session of re-runs quietly fills %TEMP% with whole restored sides.
        if self._album_work_dir is not None:
            shutil.rmtree(self._album_work_dir, ignore_errors=True)
        self._album_work_dir = Path(tempfile.mkdtemp(prefix="rrf_album_"))
        sides = []
        for s in self._sides:
            wav = mapped.get(s.index)
            if wav is None:
                continue                      # unmapped side -> not in this job
            titles = [self._flat_titles[i] for i in s.track_indices] if self._flat_titles else []
            durations = [self._flat_durations_ms[i] for i in s.track_indices] if self._flat_durations_ms else []
            sides.append(SideJob(index=s.index, label=f"Side {side_letter(s.index)}",
                                 wav_path=wav, titles=titles, durations_ms=durations))
        self._album = AlbumController(
            sides, self._album_analyze, self._album_encode,
            on_state_change=lambda side: self._relay.changed.emit(side),
            on_finished=lambda summary: self._relay.finished.emit(summary),
            max_analysis_workers=cfg.album_analysis_workers, max_encode_workers=1)
        self.side_list.clear()
        for side in sides:
            item = QListWidgetItem(f"{side.label} - {side.state.value}")
            item.setData(Qt.ItemDataRole.UserRole, side.index)
            self.side_list.addItem(item)
        self.cancel_album_btn.setEnabled(True)
        self.start_album_btn.setEnabled(False)
        self._lock_destination(True)
        self._album.start()
        self._log(f"Album: started ({len(sides)} sides, {cfg.album_analysis_workers} "
                  f"analysis worker(s)) -> {self._album_output_root}")

    # -- destination ---------------------------------------------------------
    def _lock_destination(self, running: bool) -> None:
        """Freeze the output folder for the life of a job, and say where it is.

        The encode has always used the folder captured at Start (see
        :meth:`_album_encode`), while the field above it stayed editable -- so
        editing it mid-album changed nothing, silently. Rather than make the field
        live (which would let one album's sides land in two different folders --
        an album is one artifact and belongs in one place), the field is disabled
        while a job runs and the captured destination is shown next to it. The
        rule becomes trivially reasonable: *the destination is whatever it was
        when you pressed Start, and there it is on screen.*
        """
        self.output_edit.setEnabled(not running)
        self.output_edit.setToolTip(
            "The destination is fixed while an album is running — every side "
            "encodes into the folder captured when you pressed Start album. "
            "Cancel or let it finish, then change it and start again."
            if running else "")
        self.destination_label.setVisible(running)
        if running:
            self.destination_label.setText(f"Encoding to: {self._album_output_root}")
            self.destination_label.setToolTip(
                "Where this album's FLACs are being written.")

    def _planned_filenames(self, only_side: int | None = None) -> list[str]:
        """The FLAC names this job is about to write, before it writes any.

        Derived from the same :func:`track_filename` the encoder uses, so the
        overwrite warning cannot describe different files from the ones that
        actually land. It is a best effort by nature: the reviewer may add or
        remove a split and change the track count. Under-reporting is acceptable
        here -- the prompt exists to catch the "I pointed it at the wrong folder"
        case, where the names are exactly the ones a previous run wrote.
        """
        cfg = self.settings.config
        names: list[str] = []
        for spec in self._sides:
            if not spec.track_indices:
                continue
            if only_side is not None and spec.index != only_side:
                continue          # a per-side re-do only threatens its own files
            file_start = spec.track_indices[0] + 1
            for n, flat in enumerate(spec.track_indices):
                title = (self._flat_titles[flat]
                         if self._flat_titles and flat < len(self._flat_titles) else "")
                if not title:
                    continue          # no title -> cannot predict; do not guess
                names.append(track_filename(
                    title, n + 1,
                    file_index=file_start + n,
                    side_letter=side_letter(spec.index),
                    use_side_letters=cfg.filename_side_letters,
                ))
        return names

    def _confirm_overwrite(self, out_dir: Path, *, only_side: int | None = None) -> bool:
        """Ask once, per job, before writing over files already in the destination.

        The Record tab refuses to overwrite a side outright; an album cannot, and
        should not -- a genuine re-run into the same folder is exactly what the
        user means to do. So: refuse *by default*, ask once, and name the number.
        """
        try:
            existing = [n for n in self._planned_filenames(only_side)
                        if (out_dir / n).exists()]
        except OSError:
            return True                     # unreadable destination: the encode will say so
        if not existing:
            return True
        answer = QMessageBox.question(
            self, "Overwrite existing files?",
            f"{len(existing)} file(s) already exist in {out_dir}.\n\n"
            "Starting this album will overwrite them. Overwrite?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,          # refuse by default
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log(f"Album: not started - {len(existing)} existing file(s) in "
                      f"{out_dir} were left alone. Choose a different output folder, "
                      "or start again and confirm the overwrite.")
            return False
        self._log(f"Album: overwriting {len(existing)} existing file(s) in {out_dir}.")
        return True

    def _cancel_album(self) -> None:
        if self._album is not None:
            self._album.cancel_all()
            self._cancel.set()
            self.cancel_album_btn.setEnabled(False)
            self._log("Album: cancelling all sides...")

    def _on_side_state(self, side) -> None:
        for i in range(self.side_list.count()):
            item = self.side_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == side.index:
                item.setText(f"{side.label} - {side.state.value}")
                if side.state == SideState.ERROR and side.error:
                    phase = side.failed_phase or "processing"
                    item.setToolTip(f"{side.label} failed during {phase}:\n{side.error}")
                elif side.state == SideState.NEEDS_ATTENTION and side.attention:
                    item.setToolTip(f"{side.label} needs review:\n{side.attention}")
                else:
                    item.setToolTip(f"{side.label} - {side.state.value}")
                break
        self._update_retry_enabled()
        if side.state == SideState.READY:
            self._log(f"Album: {side.label} ready for review.")
        elif side.state == SideState.NEEDS_ATTENTION:
            self._log(f"Album: {side.label} needs attention - {side.attention}. "
                      "Click it to review: re-check the mapping, or place the "
                      "splits by hand.")
        elif side.state == SideState.ERROR:
            # Say what actually went wrong, at the moment it goes wrong. The one
            # informative line used to be the only record, and the log pane shows
            # ~4 lines by default, so it scrolled away and clicking the side then
            # answered "not ready yet (error)" -- an error naming itself.
            phase = side.failed_phase or "processing"
            self._log(f"Album: {side.label} failed during {phase}: {side.error}")
            for line in (side.error_detail or "").rstrip().splitlines():
                self._log(f"    {line}")
            self._log(f"  -> select {side.label} and press 'Retry side' to run it again.")
        elif side.state == SideState.DONE:
            self._log(f"Album: {side.label} done.")
        if self._album_review_index is None:
            self._set_empty_state(self._pending_review_message())
        # Completion is the controller's to announce (on_finished), not something
        # re-derived here. This used to re-enable Start on the last terminal side
        # while leaving self._album set -- so the button came back enabled and
        # then answered "Album: already running." to anyone who pressed it.

    def _on_album_finished(self, summary) -> None:
        """The album concluded. Release it, and arm Start for a fresh run.

        Cancel already implied this arming; completion has to mean it too. The
        controller is dropped entirely -- pools shut down, state released -- so a
        second Start builds a genuinely new job rather than reviving a spent one.
        """
        album, self._album = self._album, None
        if album is not None:
            album.shutdown(wait=False)

        self._album_review_index = None
        self.cancel_album_btn.setEnabled(False)
        self._lock_destination(False)
        self._set_busy(False)               # re-arms Start (it checks _album is None)
        self.start_album_btn.setEnabled(not self._busy)
        self._update_retry_enabled()

        redone, self._redoing_side = self._redoing_side, None
        if redone is not None:
            # A scoped re-do is not an album, and saying "album finished" after
            # re-doing one side would be describing our machinery rather than
            # what the user did.
            letter = side_letter(redone)
            self._log(f"Album: Side {letter} re-done. {summary.describe()}")
            if summary.done:
                self._log(f"  -> {self._album_output_root}")
            # The receipt is rebuilt from the *previous* album's snapshot with
            # this side's new figures merged in, so the card keeps describing the
            # whole record rather than shrinking to the one side just re-done.
            self._merge_redo_into_snapshot(redone, summary)
            merged = self._rerun_snapshot.get("summary") if self._rerun_snapshot else None
            if merged is not None:
                self._show_summary_card(merged)
            self._maybe_reset_after_album()
            return

        self._log(summary.describe())
        if summary.done:
            self._log(f"  -> {self._album_output_root}")

        # Snapshot the finished album *before* the clean-slate reset so "Run again"
        # on the card can restore it in one click, then render the card (it copies
        # what it needs, so clearing identity afterwards cannot disturb it).
        self._rerun_snapshot = self._snapshot_album()
        self._rerun_snapshot["summary"] = summary
        if summary.total and self._album_review_index is None:
            self._show_summary_card(summary)
        elif self._album_review_index is None:
            self._set_empty_state(self._pending_review_message())

        # Clean slate for the next record -- unless a capture is under way, in
        # which case defer so a WAV about to land is not orphaned (9.2 decides
        # where it lands; the reset waits until it has -- see add_recorded_wav).
        self._maybe_reset_after_album()

    def _maybe_reset_after_album(self) -> None:
        """Clean slate, unless a capture is in flight and would be orphaned."""
        if self._recording_active:
            self._reset_deferred = True
            self._log("Album: a recording is in progress -- keeping this mapping "
                      "until it lands, then clearing for the next record.")
        else:
            self._reset_identity()

    def _merge_redo_into_snapshot(self, index: int, summary) -> None:
        """Fold a re-done side's new receipt into the album's stored summary.

        The card describes a record, not a job. After re-doing Side B, it should
        still list Side A -- with the figures from when A was made, which have
        not changed -- and Side B with its new track count, size and receipts.
        Replacing the stored summary wholesale would shrink the receipt to the
        one side and lose the other, which is the opposite of what a re-do is
        for.
        """
        if self._rerun_snapshot is None:
            return
        previous = self._rerun_snapshot.get("summary")
        fresh = next((s for s in summary.sides if s.index == index), None)
        if previous is None or fresh is None:
            self._rerun_snapshot["summary"] = summary
            return
        sides = tuple(fresh if s.index == index else s for s in previous.sides)
        self._rerun_snapshot["summary"] = dataclasses.replace(previous, sides=sides)

    def _show_summary_card(self, summary) -> None:
        """Populate and reveal the finished-album card (hiding the other two)."""
        self.summary_card.render(
            summary,
            cover=self._cover,
            artist=self._album_meta.get("artist", ""),
            album=self._album_meta.get("album", ""),
            destination=Path(self._album_output_root) if self._album_output_root else None,
            on_dismiss=self._dismiss_summary_card,
            on_rerun=self._run_album_again,
            on_redo_side=self._redo_side_from_card,
        )
        self.empty_state.setVisible(False)
        self.review_box.setVisible(False)
        self.summary_card.setVisible(True)

    def _dismiss_summary_card(self) -> None:
        """The × on the card: fall back to the normal idle message."""
        self.summary_card.setVisible(False)
        if self._album_review_index is None:
            self._set_empty_state(self._pending_review_message())

    # -- clean slate between albums -----------------------------------------
    def set_recording_active(self, active: bool) -> None:
        """MainWindow tells us when a capture is under way, so an album that
        concludes mid-recording can defer its clean-slate reset."""
        self._recording_active = active

    def _snapshot_album(self) -> dict:
        """Everything needed to restore the just-finished album for a re-run."""
        return {
            "release": self._release,
            "cover": self._cover,
            "artist": self.artist_edit.text(),
            "album": self.album_edit.text(),
            "flat_titles": list(self._flat_titles),
            "flat_durations_ms": list(self._flat_durations_ms),
            "flat_track_infos": list(self._flat_track_infos),
            "sides": list(self._sides),
            "wavs": list(self._album_wavs),
            "mapping": list(self._album_mapping),
            "pinned": dict(self._pinned_map),
            "output": self.output_edit.text(),
            "browse_start": self._browse_start,
        }

    def _reset_identity(self) -> None:
        """Clear all album *identity* between records -- unconditionally.

        Artist/Album, the selected release + preview, the side picker and the
        mapping table all go: no default is safe for identity, because inheriting
        the last album's Artist/release would mistag the next record. Source and
        output *folders* follow their configured policies (keep / reset / clear).
        The summary card is left alone -- it holds its own state.
        """
        cfg = self.settings.config
        self.artist_edit.clear()
        self.album_edit.clear()
        self._release = None
        self._cover = None
        self.release_preview.clear()
        self._flat_titles = []
        self._flat_durations_ms = []
        self._flat_track_infos = []
        self._album_wavs = []
        self._pinned_map = {}
        self._set_sides([])                  # empties the side picker (no WAVs -> no rebuild)
        self.define_sides_button.setEnabled(False)
        self.side_list.clear()
        self._rebuild_mapping_table()        # empties the mapping table widget
        self._apply_folder_policy(cfg.source_post_album_policy,
                                  cfg.default_source_dir, is_source=True)
        self._apply_folder_policy(cfg.output_post_album_policy,
                                  cfg.default_output_dir, is_source=False)
        # A cleared output field is no longer a path the user is defending, so the
        # next release may offer one again.
        self._output_hand_edited = bool(self.output_edit.text().strip())
        self._emphasised = None
        # The Record tab holds session state of its own (the declared album, the
        # armed bridge); the clean slate is album-wide, not tab-wide.
        self.identityReset.emit()

    def _apply_folder_policy(self, policy: str, default: str, *, is_source: bool) -> None:
        """Post-album folder behaviour: keep (leave it), reset (to the configured
        default), or clear (empty it). Identity has no such policy."""
        if policy == "keep":
            return
        target = default if policy == "reset" else ""
        if is_source:
            self._browse_start = target
            self.settings.set(source_dir=target)
        else:
            self.output_edit.setText(target)
            self.settings.set(output_dir=target)

    def _redo_side_from_card(self, index: int) -> None:
        """Re-open one finished side from the receipt, back into review.

        The stakeholder's case: Discovery ripped end to end, Side B accepted
        with four tracks when the record has five. A split was missed and Accept
        locked the door. Re-tag cannot split a FLAC, "Run this album again"
        re-does the side that was already right, and Retry was scoped to sides
        that had errored. There was no way to appeal a side that merely came out
        wrong.

        This is a *scoped* job: one AlbumController holding one side, built from
        the finished album's snapshot. Deliberately not a resurrection of the
        old job -- that album is finished, its other sides' files are untouched
        on disk, and nothing here should block starting a different album later.
        """
        snap = self._rerun_snapshot
        if snap is None:
            self._log("Album: nothing to re-do — this session's album is gone.")
            return
        if self._album is not None:
            self._log("Album: already running. Finish or cancel it first.")
            return

        side_meta = next((s for s in snap["sides"] if s.index == index), None)
        wav = self._snapshot_wav_for_side(snap, index)
        if side_meta is None or wav is None:
            self._log(f"Album: cannot re-do side {index} — its source WAV is no "
                      "longer mapped.")
            return
        if not Path(wav).exists():
            self._log(f"Album: cannot re-do {side_letter(index)} — its source WAV "
                      f"({Path(wav).name}) is not where it was. The raw WAVs are "
                      "the master; re-do needs the original file.")
            return
        output = snap.get("output", "") or self._album_output_root
        if not output:
            self._log("Album: cannot re-do — the output folder for that album is "
                      "not known any more.")
            return

        # Identity first: the encode reads self._release/_cover/_side_track_infos
        # directly, and the clean slate cleared all of them when the album ended.
        self._restore_identity_from(snap)
        if not self._warn_if_identity_is_missing(snap, side_letter(index)):
            return

        label = f"Side {side_letter(index)}"
        titles = ([snap["flat_titles"][i] for i in side_meta.track_indices]
                  if snap["flat_titles"] else [])
        durations = ([snap["flat_durations_ms"][i] for i in side_meta.track_indices]
                     if snap["flat_durations_ms"] else [])

        # Same one-ask overwrite doctrine as a full album, scoped to this side's
        # files. A re-do exists precisely to replace them, so the question is
        # "are you sure", asked once, not per file.
        if not self._confirm_overwrite(Path(output), only_side=index):
            return

        self._cancel.clear()
        self.summary_card.setVisible(False)
        self._album_output_root = output
        self._album_meta = {"artist": snap["artist"], "album": snap["album"]}
        if self._album_work_dir is not None:
            shutil.rmtree(self._album_work_dir, ignore_errors=True)
        self._album_work_dir = Path(tempfile.mkdtemp(prefix="rrf_redo_"))

        cfg = self.settings.config
        job = SideJob(index=index, label=label, wav_path=Path(wav),
                      titles=titles, durations_ms=durations)
        self._redoing_side = index
        self._album = AlbumController(
            [job], self._album_analyze, self._album_encode,
            on_state_change=lambda side: self._relay.changed.emit(side),
            on_finished=lambda summary: self._relay.finished.emit(summary),
            max_analysis_workers=1, max_encode_workers=1)
        self.side_list.clear()
        item = QListWidgetItem(f"{label} - {job.state.value}")
        item.setData(Qt.ItemDataRole.UserRole, index)
        self.side_list.addItem(item)
        self.cancel_album_btn.setEnabled(True)
        self.start_album_btn.setEnabled(False)
        self._lock_destination(True)
        self._album.start()
        self._log(f"Album: re-doing {label} from {Path(wav).name}. The other "
                  "sides' files are left alone.")
        self._set_empty_state(f"Re-analyzing {label}…")

    def _snapshot_wav_for_side(self, snap: dict, index: int):
        """The WAV the snapshot mapped to ``index``, or None."""
        for wav, mapped_index in zip(snap["wavs"], snap["mapping"]):
            if mapped_index == index:
                return wav
        return None

    def _restore_identity_from(self, snap: dict) -> None:
        """Put back everything the encode path reads off ``self``."""
        self._release = snap["release"]
        self._cover = snap["cover"]
        self._flat_titles = list(snap["flat_titles"])
        self._flat_durations_ms = list(snap["flat_durations_ms"])
        self._flat_track_infos = list(snap["flat_track_infos"])
        self.artist_edit.setText(snap["artist"])
        self.album_edit.setText(snap["album"])
        if snap["release"] is not None:
            self.release_preview.set_release(snap["release"])
        self._set_sides(list(snap["sides"]))

    def _warn_if_identity_is_missing(self, snap: dict, letter: str) -> bool:
        """Refuse to silently produce untagged output. Returns whether to go on.

        A re-do inherits the album's release from memory, which is fine while
        the app has been running. When it has not -- the release was never
        looked up, or the session has been restarted and this card was rebuilt
        without one -- the audio and the destination are recoverable but the
        tracklist and cover are not. Splitting correctly and then writing
        "Track 1" is not an acceptable appeal: it trades a visible problem for
        an invisible one.
        """
        if snap.get("release") is not None:
            return True
        self._log(f"Album: Side {letter} can be re-split, but this album's "
                  "tracklist and cover art are not in memory any more — the "
                  "re-done tracks would be saved without titles or cover.")
        return self._offer_lookup_before_redo(letter)

    def _offer_lookup_before_redo(self, letter: str) -> bool:
        """Ask, in plain words, with the fix attached rather than described."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Look up this release first?")
        box.setText(
            f"Side {letter} can be re-split, but this album's tracklist and "
            "cover art are no longer in memory.")
        box.setInformativeText(
            "Re-doing it now would save the tracks without titles, track "
            "numbers or cover art.\n\nLook the release up again first?")
        lookup = box.addButton("Look up release…", QMessageBox.ButtonRole.AcceptRole)
        anyway = box.addButton("Re-do without tags", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(lookup)
        box.exec()

        clicked = box.clickedButton()
        if clicked is lookup:
            self._open_lookup()
            self._log("Album: look up the release, then press Re-do this side again.")
            return False
        if clicked is anyway:
            self._log("Album: re-doing without tags, as asked — the tracks will "
                      "be split correctly but saved untitled.")
            return True
        return False

    def _run_album_again(self) -> None:
        """Restore the just-concluded album's identity + exact mapping and arm Start.

        The escape hatch that makes the clean slate safe: the 8.4 do-over stays a
        single click for the album you just finished, instead of full re-entry.
        """
        snap = self._rerun_snapshot
        if snap is None:
            return
        self._release = snap["release"]
        self._cover = snap["cover"]
        self.artist_edit.setText(snap["artist"])
        self.album_edit.setText(snap["album"])
        self._flat_titles = list(snap["flat_titles"])
        self._flat_durations_ms = list(snap["flat_durations_ms"])
        self._flat_track_infos = list(snap["flat_track_infos"])
        self._browse_start = snap["browse_start"]
        self.output_edit.setText(snap["output"])
        if snap["release"] is not None:
            self.release_preview.set_release(snap["release"])
        self.define_sides_button.setEnabled(bool(self._flat_titles))
        self._album_wavs = list(snap["wavs"])
        self._pinned_map = dict(snap["pinned"])
        self._set_sides(list(snap["sides"]))     # rebuilds side_combo, re-derives mapping
        # Force the *exact* saved mapping (not a fresh proposal) back onto the rows.
        self._album_mapping = list(snap["mapping"])
        for row in range(len(self._album_wavs)):
            combo = self.mapping_table.cellWidget(row, 1)
            if combo is not None:
                want = self._album_mapping[row]
                combo.blockSignals(True)
                combo.setCurrentIndex(0 if want is None else max(0, combo.findData(want)))
                combo.blockSignals(False)
        self.summary_card.setVisible(False)
        self.start_album_btn.setEnabled(not self._busy)
        self._log("Album: restored -- press Start album to run it again.")

    def _on_side_list_click(self, item) -> None:
        if self._album is None:
            return
        side = self._album_side(item.data(Qt.ItemDataRole.UserRole))
        if side is None:
            return
        if side.state == SideState.ERROR:
            self._show_side_error(side)
            return
        if side.analysis is None:
            self._log(f"Album: {side.label} not ready yet ({side.state.value}).")
            return
        if side.index == self._album_review_index:
            return                                  # already open
        if not self._confirm_discard_review():
            return
        # A guard-tripped side opens for review exactly like a ready one -- the
        # analysis is intact; it just arrives with a diagnosis banner. It keeps
        # its NEEDS_ATTENTION state while under review, rather than being flipped
        # to RESOLVING: the flag is what keeps Retry available (the user may go
        # fix the mapping) and what tells the list why this side is different.
        if side.state in (SideState.READY, SideState.RESOLVING):
            self._album.mark_resolving(side.index)
            self._load_side_for_review(side)
        elif side.state == SideState.NEEDS_ATTENTION:
            self._load_side_for_review(side)

    def _confirm_discard_review(self) -> bool:
        """Ask once before throwing away an in-progress, unaccepted review.

        Only unaccepted state is at risk: an accepted side is already cut and
        queued to encode, so switching away from it loses nothing.
        """
        if self._album_review_index is None or self._analysis is None:
            return True
        current = self._album_side(self._album_review_index)
        label = current.label if current else "this side"
        answer = QMessageBox.question(
            self, "Discard review?",
            f"Discard {label}'s review? Its splits and title edits have not been accepted.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self._log(f"Album: {label} review discarded.")
        self._album_review_index = None
        self._clear_review()
        return True

    # -- audition playback ---------------------------------------------------
    def _load_playback_source(self, restored_path) -> None:
        """Point the player at this side's restored WAV, transcoding if it won't play.

        ``restore`` quantises its final write back to the *source* subtype, so a
        normal 16-bit rip stages as PCM_16 and plays natively. A float-sourced rip
        would stage as float, which Windows' media backends handle unreliably --
        so those get a PCM_16 preview copy alongside the staging file.
        """
        self._stop_playback()
        if not self.player.available or restored_path is None:
            return
        path = Path(restored_path)
        if not path.exists():
            return
        try:
            import soundfile as sf

            if not sf.info(str(path)).subtype.startswith("PCM"):
                from gui.playback import transcode_for_preview

                preview = path.with_name(path.stem + "_preview.wav")
                path = transcode_for_preview(path, preview)
                self._log("Playback: staged audio is float; using a 16-bit preview copy.")
        except Exception as exc:                       # never break review over audio
            self._log(f"Playback: could not prepare audio ({exc}).")
            return
        self.player.set_source(path)
        self._update_preview_enabled()

    def _on_seek_requested(self, seconds: float) -> None:
        self.player.seek(seconds)
        self.waveform.set_playhead(seconds)

    def _on_playing_changed(self, playing: bool) -> None:
        self.play_btn.setText("Pause" if playing else "Play")

    def _toggle_play(self) -> None:
        self.player.toggle()

    def _update_preview_enabled(self) -> None:
        self.preview_cut_btn.setEnabled(
            self.player.available and self.waveform.selected_time() is not None)

    def _preview_selected_cut(self) -> None:
        """The core gesture: hear the approach and the cut, decide with your ear."""
        t = self.waveform.selected_time()
        if t is None:
            self._log("Playback: select a split marker first (click it on the waveform).")
            return
        lead = self.settings.config.preview_lead_in_s
        self.player.preview_cut(t, lead)
        self._log(f"Playback: previewing the cut at {format_timestamp(t)} "
                  f"(from {lead:.0f}s before).")

    def _play_current_window(self) -> None:
        """Hear an unresolved gap's window before placing a split in it."""
        if not self._unresolved or not 0 <= self._gap_idx < len(self._unresolved):
            return
        gap = self._unresolved[self._gap_idx]
        self.player.play_window(gap.window_start, gap.window_end)
        self._log(f"Playback: playing the window "
                  f"{format_timestamp(gap.window_start)}-{format_timestamp(gap.window_end)}.")

    def _stop_playback(self) -> None:
        """Stop AND release the file -- staging cleanup must never hit a locked handle."""
        self.player.stop()
        self.waveform.set_playhead(None)
        self.play_btn.setText("Play")

    def keyPressEvent(self, event) -> None:
        """Space plays/pauses; arrows nudge the selected marker.

        Nudge-then-preview is meant to be a two-key rhythm: Left/Right to move the
        cut, Space (or Preview cut) to hear it again.
        """
        if not self.review_box.isVisible():
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._toggle_play()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = self.settings.config.marker_nudge_ms / 1000.0
            delta = -step if key == Qt.Key.Key_Left else step
            if self.waveform.nudge_selected(delta):
                t = self.waveform.selected_time()
                self._log(f"Marker nudged to {format_timestamp(t)}." if t else "")
                event.accept()
                return
        super().keyPressEvent(event)

    def _show_side_error(self, side) -> None:
        """An errored side puts its cause in the review area, not an empty panel."""
        self._album_review_index = None
        phase = side.failed_phase or "processing"
        self._set_empty_state(
            f"{side.label} failed during {phase}.\n\n{side.error}\n\n"
            "Fix the cause, then press 'Retry side' to run this side again. "
            "The other sides are unaffected."
        )
        self._log(f"Album: {side.label} failed during {phase}: {side.error}")
        self._update_retry_enabled()

    def _selected_side(self):
        item = self.side_list.currentItem()
        if item is None or self._album is None:
            return None
        return self._album_side(item.data(Qt.ItemDataRole.UserRole))

    def _update_retry_enabled(self) -> None:
        """Live whenever re-doing the selected side is meaningful.

        Which is any settled state: finished, failed, or flagged. A control that
        stays disabled for an entire session reads as broken rather than as
        inapplicable, and "the side is finished" was never a reason you cannot
        do it again.
        """
        side = self._selected_side()
        redoable = side is not None and side.wav_path is not None and side.state in (
            SideState.ERROR, SideState.NEEDS_ATTENTION, SideState.DONE)
        self.retry_side_btn.setEnabled(redoable)
        if side is not None and side.state == SideState.NEEDS_ATTENTION:
            self.retry_side_btn.setToolTip(
                "Re-analyzes with the current mapping — if nothing changed, "
                "the result won't either.")
        elif side is not None and side.state == SideState.DONE:
            self.retry_side_btn.setToolTip(
                "Re-run this finished side from its source WAV and review the "
                "splits again. Its current files are replaced when you accept.")
        else:
            self.retry_side_btn.setToolTip(
                "Re-run the selected side from its source WAV, back through review.")

    def _retry_selected_side(self) -> None:
        side = self._selected_side()
        if side is None or self._album is None:
            return
        label = side.label
        was_done = side.state == SideState.DONE
        if self._album.retry_side(side.index):
            self._log(f"Album: re-doing {label} from its source WAV."
                      + (" Its current files stay until you accept the new ones."
                         if was_done else ""))
            if self._album_review_index is None:
                self._set_empty_state(self._pending_review_message())
        else:
            self._log(f"Album: {label} cannot be re-done ({side.state.value}).")
        self._update_retry_enabled()

    def _load_side_for_review(self, side) -> None:
        self._album_review_index = side.index
        analysis = side.analysis
        self._analysis = analysis
        self._show_review()
        self._expected_titles = list(side.titles)
        self._expected_n = len(side.titles) or None
        self._expected_durations_s = (
            [d / 1000.0 for d in side.durations_ms]
            if side.durations_ms and all(side.durations_ms) else [])
        self.waveform.set_envelope(analysis.envelope)
        self.waveform.set_markers([p.timestamp for p in analysis.proposal.split_points],
                                  [p.confidence for p in analysis.proposal.split_points])
        self._unresolved = list(analysis.proposal.unresolved)
        # Audition the RESTORED staged WAV -- the audio the cuts actually apply to.
        self._load_playback_source(analysis.restored_path)
        self.accept_button.setText("Accept side")
        if getattr(side, "attention", ""):
            # The guard tripped. Say so, and let the user choose between fixing
            # the mapping and placing the splits by hand -- the same resolve flow
            # the single-side path has always had, just reachable from here.
            self._show_wrong_side_diagnosis()
        elif self._unresolved:
            self._begin_gap_resolution()
        else:
            self.gap_box.setVisible(False)
            self.waveform.clear_region()
            self.waveform.set_place_mode(False)
            self.waveform.zoom_full()
        self.model.set_rows([])
        self._sync_review_table()
        self._update_accept_enabled()
        self._log(f"Album: reviewing {side.label} "
                  f"({len(analysis.proposal.split_points)} cut(s), {len(self._unresolved)} unresolved). "
                  "Edit titles/artists, then Accept side.")

    def _enrich_tracks(self, titles, segments, track_infos, side_position, total_sides, artist, album,
                       *, artists=None, file_start=None, side_letter_="", use_side_letters=False):
        """Build Tracks carrying every field we actually have from the release.

        With no release selected, only the base fields are set -- the old minimal
        tag set. Track numbering is per-side (Picard vinyl convention): TRACKNUMBER
        resets each side, TRACKTOTAL is the side's track count, DISCNUMBER is the
        side position, DISCTOTAL the number of sides.

        ``file_start`` is the album-wide 1-based number of this side's first track
        and drives the *filename* only (album jobs write every side into one flat
        folder, so filenames must not collide even though TRACKNUMBER repeats).
        Leave it ``None`` for a single-side job to keep the plain ``[NN]`` naming.
        """
        from core.tracks import Tracks

        rich = self._release is not None
        year = self._release.year if rich else ""
        album_mb = self._release.release_id if rich else ""
        release_artist_id = self._release.artist_id if rich else ""
        total = len(segments)
        result = []
        for i, seg in enumerate(segments):
            info = track_infos[i] if i < len(track_infos) else None
            if i < len(titles) and titles[i].strip():
                title = titles[i].strip()
            elif info is not None:
                title = info.title
            else:
                title = f"track_{i + 1:02d}"
            # An artist the reviewer actually typed wins over anything derived.
            if artists and i < len(artists) and str(artists[i]).strip():
                row_artist = str(artists[i]).strip()
            else:
                row_artist = info.artist if (info and info.artist) else artist
            result.append(Tracks(
                i + 1, title, album, row_artist, seg,
                album_artist=(artist if rich else ""),
                date=(year if rich else ""),
                track_total=(total if rich else None),
                disc_number=(side_position if rich else None),
                disc_total=(total_sides if rich else None),
                mb_album_id=(album_mb if rich else ""),
                mb_artist_id=((info.artist_id if info and info.artist_id else release_artist_id) if rich else ""),
                mb_recording_id=(info.recording_id if (rich and info) else ""),
                mb_track_id=(info.track_mbid if (rich and info) else ""),
                # Filename-only: never reaches the tags.
                file_index=(file_start + i if file_start is not None else None),
                side_letter=side_letter_,
                use_side_letters=use_side_letters,
            ))
        return result

    def _update_meta_summary(self) -> None:
        if self._release is None:
            self.meta_summary.setText("")
            return
        r = self._release
        bits = [f"album artist: {r.artist}"]
        if r.year:
            bits.append(f"date: {r.year}")
        if r.release_id:
            bits.append("MB album id")
        if r.artist_id:
            bits.append("MB artist id")
        n_rec = sum(1 for ti in self._review_track_infos if getattr(ti, "recording_id", ""))
        if n_rec:
            bits.append(f"{n_rec} track MBID(s)")
        self.meta_summary.setText("Will also write: " + " | ".join(bits))

    def _accept_album_side(self) -> None:
        """Accept *is* the commit: snapshot, enqueue the encode, free the review.

        There used to be an accepted-but-not-yet-encoded limbo -- Accept cut the
        side, then a separate Encode button handed it to the controller -- and
        switching sides in between silently dropped it. Now Accept snapshots the
        table onto the SideJob and the controller enqueues the encode straight
        onto its encode pool, so there is nothing left in the UI to lose. The
        controller does the cutting inside encode_fn.
        """
        if self._busy or self._album is None:
            return
        index = self._album_review_index
        side = self._album_side(index)
        if side is None or side.analysis is None:
            return
        # Check the destination the encode will *actually* use -- the one captured
        # at Start -- not the live field. Validating one value and encoding into
        # another is how "I fixed the output folder" came to mean nothing.
        if not self._album_output_root:
            self._log("Album: choose an output folder first.")
            return

        # Release the staged file before the controller cuts and cleans it up --
        # a live handle makes the staging delete fail on Windows.
        self._stop_playback()

        timestamps = self.waveform.marker_times()
        rows = self.model.rows()
        titles = [r.title for r in rows]
        artists = [r.artist for r in rows]

        # Snapshot + enqueue in one step; the side list now drives the feedback.
        self._album.accept_side(index, timestamps, titles, artists)
        self._log(f"Album: {side.label} accepted ({len(titles)} tracks); "
                  "cutting and encoding in the background.")

        self._album_review_index = None
        self._clear_review()

    def _album_analyze(self, side, should_cancel):
        """Runs on an AlbumController thread -- no widget access."""
        from core.restoration import restore
        from core.splitting import propose_splits, propose_splits_anchored
        from core.waveform import load_peak_envelope

        cfg = self.settings.config
        stages = job_settings.build_stages(cfg)
        policy = job_settings.build_policy(cfg)
        params = job_settings.build_silence_params(cfg)
        side_dir = self._album_work_dir / f"side_{side.index}"
        # Fresh staging every run, so a retry never inherits a half-written
        # restored.wav from the attempt that failed.
        shutil.rmtree(side_dir, ignore_errors=True)
        side_dir.mkdir(parents=True, exist_ok=True)
        restored = side_dir / "restored.wav"
        result = restore(side.wav_path, restored, stages, policy=policy, should_cancel=should_cancel)

        if side.durations_ms and all(side.durations_ms):
            proposal = propose_splits_anchored(restored, side.durations_ms, params=params,
                                               window_s=cfg.window_s, speed_tolerance=cfg.speed_tolerance)
        elif side.titles:
            proposal = propose_splits(restored, track_count=len(side.titles), params=params)
        else:
            proposal = propose_splits(restored, params=params)

        envelope = load_peak_envelope(restored)
        analysis = AnalyzeResult(result, proposal, envelope, restored, stages)

        # The sanity guard used to raise here, which the controller turned into
        # ERROR -- throwing away a perfectly good proposal and leaving Retry as
        # the only exit, on input that deterministically re-fails. A guard trip
        # is a request for review, so hand the analysis over with it.
        n = len(side.titles) or None
        if n and wrong_side_suspected(n, len(proposal.unresolved),
                                      frac=cfg.wrong_side_frac):
            confirmed = (n - 1) - len(proposal.unresolved)
            raise NeedsAttention(
                f"expected {n} tracks; only {confirmed} of {n - 1} boundaries confirmed",
                analysis,
            )
        return analysis

    def _album_encode(self, side, should_cancel):
        """Runs on an AlbumController thread -- no widget access."""
        from core.converter import convert_wavs_to_flacs
        from core.ffmpeg_locator import configure_pydub
        from core.splitting import execute_split
        from core.tracks import Tracks

        side_dir = self._album_work_dir / f"side_{side.index}"
        segments = execute_split(side.analysis.restored_path, side.timestamps,
                                 side_dir / "segments")
        artist = self._album_meta.get("artist", "")
        album = self._album_meta.get("album", "")
        track_infos = self._side_track_infos.get(side.index, [])
        # Every side lands in the SAME flat folder, so filenames carry an
        # album-wide number (or a side letter) while the tags stay per-side.
        # file_start is this side's first track in flat album order, which keeps
        # numbering stable even if the sides are encoded out of order or a side
        # is re-run on its own later.
        cfg = self.settings.config
        spec = next((s for s in self._sides if s.index == side.index), None)
        file_start = (spec.track_indices[0] + 1) if spec and spec.track_indices else 1
        tracks = self._enrich_tracks(
            list(side.titles), segments, track_infos,
            side.index + 1, self._total_sides or 1, artist, album,
            # Per-track artists as the reviewer left them, snapshotted at accept.
            artists=list(side.artists),
            file_start=file_start,
            side_letter_=side_letter(side.index),
            use_side_letters=cfg.filename_side_letters,
        )
        out_dir = Path(self._album_output_root)
        configure_pydub()

        # The single-side path surfaces batch.warnings via _on_encode_done; the
        # album path used to throw the BatchResult away, so a failed tag write or
        # cover embed ("Could not write tags: ...", "Could not embed cover art:
        # ...") vanished silently and looked like the album path simply not
        # tagging. Warnings are per-track and never fail the batch, so they have
        # to be reported or they are lost. _log emits a signal, which is safe to
        # call from this worker thread.
        if self._release is None:
            self._log(f"Album: {side.label} - no release loaded, writing minimal tags "
                      "and no cover art. Use 'Look up release...' for full metadata.")
        batch = convert_wavs_to_flacs(tracks, out_dir, configure=False, cover=self._cover,
                                      max_workers=self.settings.config.encode_workers,
                                      should_cancel=should_cancel,
                                      restoration_stages=side.analysis.stages,
                                      output_sample_rate=cfg.output_sample_rate)
        for warning in batch.warnings:
            self._log(f"  ! {side.label}: {warning}")

        # Capture the side's receipt now, while the files are fresh -- the summary
        # card reads this, it never re-walks the output folder. The controller
        # re-stamps the final state (this side may still be cancelled). state here
        # is a placeholder; measure_outputs sizes the just-written FLACs.
        output_paths = tuple(o.output_path for o in batch.outcomes)
        total_bytes, duration_s = measure_outputs(output_paths)
        restoration = getattr(side.analysis, "restoration", None)
        side.result = SideSummary(
            index=side.index, label=side.label, state=side.state,
            track_count=len(output_paths), output_paths=output_paths,
            total_bytes=total_bytes, duration_s=duration_s,
            warnings=tuple(batch.warnings),
            warned_tracks=sum(1 for o in batch.outcomes if o.warnings),
            declick_repaired_samples=getattr(restoration, "declick_repaired_samples", None),
            declick_total_samples=getattr(restoration, "declick_total_samples", None),
        )
        if side.result.declick_repaired_samples:
            pct = 100.0 * side.result.declick_repaired_samples / side.result.declick_total_samples
            self._log(f"  {side.label}: declick repaired "
                      f"{side.result.declick_repaired_samples:,} of "
                      f"{side.result.declick_total_samples:,} samples ({pct:.2f}%).")

    def _cleanup_work_dir(self) -> None:
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

    def cleanup(self) -> None:
        self._cancel.set()
        if self._album is not None:
            self._album.cancel_all()
            self._album.shutdown(wait=False)
        if self._album_work_dir is not None:
            shutil.rmtree(self._album_work_dir, ignore_errors=True)
        self._cleanup_work_dir()
