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

import shutil
import tempfile
import threading
import traceback
from dataclasses import dataclass
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
    AlbumController,
    SideJob,
    SideState,
    propose_wav_side_map,
    sides_from_proposal,
)
from core.side_partition import Side
from core.split_review import detect_progressive_drift, segment_deviations, wrong_side_suspected
from core.timefmt import format_timestamp
from gui.release_preview import NO_COVER_TEXT, ReleasePreview
from gui.side_editor import SideEditorDialog, side_letter
from gui.track_model import Row, TrackTableModel, TrackTableView
from gui.waveform import WaveformView

# Shown in the side dropdown for a WAV that is not part of this album. It is the
# default for anything the heuristics are not confident about.
SKIP_LABEL = "— skip —"

# Mapping-table geometry: 5 rows visible without scrolling (header + 5 * row) --
# enough for a double album plus a stray, while leaving the review area the
# dominant share of the tab.
_MAP_ROW_H = 22
_MAP_TABLE_ROWS = 5
_MAP_TABLE_H = _MAP_ROW_H * _MAP_TABLE_ROWS + 26


class _StateRelay(QObject):
    """Marshals AlbumController state callbacks (worker threads) to the GUI."""

    changed = Signal(object)


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
@dataclass
class AnalyzeResult:
    restoration: object
    proposal: object
    envelope: object
    restored_path: Path


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

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.pool = QThreadPool.globalInstance()

        # state
        self._release = None
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
        self._browse_start: str = settings.config.source_dir or ""

        # album mode
        self._album: AlbumController | None = None
        self._album_wavs: list[Path] = []
        self._album_mapping: list = []
        self._album_work_dir: Path | None = None
        self._album_output_root = ""
        self._album_meta: dict = {}
        self._album_review_index: int | None = None
        self._relay = _StateRelay()
        self._relay.changed.connect(self._on_side_state)

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
        self.album_box = QGroupBox("Source - pick the folder holding this record's side WAVs")
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
        pick_row.addWidget(self.start_album_btn)
        pick_row.addWidget(self.cancel_album_btn)
        album_layout.addLayout(pick_row)

        self.mapping_hint = QLabel(
            "One row per WAV found. Set the side for the files belonging to this "
            "record; leave anything else on — skip —. Only mapped rows are "
            "processed — run again with a different mapping for the next album."
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

        self.output_edit = QLineEdit(settings.config.output_dir)
        form.addRow("Output folder:", self._path_row(self.output_edit, self._browse_output))
        root.addLayout(form)

        # --- 4. Review: only meaningful once a side is ready --------------------
        # Everything below lives in one widget so it can be swapped for an
        # empty-state label instead of presenting a screenful of dead controls.
        self.empty_state = QLabel("Select a folder to begin.")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setStyleSheet("QLabel { color: palette(mid); padding: 24px; }")
        root.addWidget(self.empty_state, 1)

        self.review_box = QWidget()
        review = QVBoxLayout(self.review_box)
        review.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.review_box, 1)
        self.review_box.setVisible(False)

        self.waveform = WaveformView()
        self.waveform.markersChanged.connect(self._on_markers_changed)
        self.waveform.setMinimumHeight(190)
        review.addWidget(self.waveform, 3)

        # wrong-side diagnosis
        self.diagnosis_box = QGroupBox("Check the selection")
        diag = QHBoxLayout(self.diagnosis_box)
        self.diagnosis_label = QLabel("")
        self.diagnosis_label.setWordWrap(True)
        diag.addWidget(self.diagnosis_label, 1)
        reselect = QPushButton("Re-select side")
        reselect.clicked.connect(self._reselect_side)
        diag.addWidget(reselect)
        resolve_anyway = QPushButton("Resolve manually anyway")
        resolve_anyway.clicked.connect(self._resolve_anyway)
        diag.addWidget(resolve_anyway)
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
        self.next_gap_btn = QPushButton("Next >")
        self.next_gap_btn.clicked.connect(lambda: self._present_gap(self._gap_idx + 1))
        gap_layout.addWidget(self.prev_gap_btn)
        gap_layout.addWidget(self.next_gap_btn)
        self.gap_box.setVisible(False)
        review.addWidget(self.gap_box)

        ctl = QHBoxLayout()
        add_btn = QPushButton("Add split at center")
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
        self.meta_summary.setStyleSheet("QLabel { color: palette(mid); }")
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
        panel = MetadataPanel()
        panel.artist_edit.setText(self.artist_edit.text())
        panel.album_edit.setText(self.album_edit.text())
        panel.statusMessage.connect(self._log)
        panel.releaseSelected.connect(lambda detail: (self._apply_release(detail), dialog.accept()))
        layout.addWidget(panel)
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
        self._log(f"Full Rip: release '{detail.title}' loaded "
                  f"({len(detail.media)} side(s), cover={'yes' if detail.cover else 'no'}).")
        if detail.cover is None:
            self._log(f"  ! {NO_COVER_TEXT} - tracks will be tagged without a picture.")

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
        if self._expected_n and wrong_side_suspected(self._expected_n, len(self._unresolved)):
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
        n = self._expected_n
        confirmed = (n - 1) - len(self._unresolved) if n else 0
        self.diagnosis_label.setText(
            f"Expected {n} tracks but could only confirm {confirmed} boundaries. "
            "This usually means the wrong side or release is selected (the side has "
            "fewer tracks than expected)."
        )
        self.diagnosis_box.setVisible(True)
        self.gap_box.setVisible(False)
        self.waveform.set_place_mode(False)
        self._log("Full Rip: too many gaps unresolved - suspect wrong side/release.")

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
        self.review_box.setVisible(True)

    def _set_empty_state(self, message: str) -> None:
        """Hide the review controls behind an explanatory line.

        Dead, greyed-out controls tell the user nothing; a sentence saying what
        the tab is waiting for tells them what to do next.
        """
        self.empty_state.setText(message)
        self.empty_state.setVisible(True)
        self.review_box.setVisible(False)

    def _clear_review(self) -> None:
        """Return the review area to its empty state after a side is handed off."""
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
        if all(s in (SideState.DONE, SideState.ERROR, SideState.CANCELLED) for s in states):
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
            self._rebuild_mapping_table()
            self._log(f"Source: {len(self._album_wavs)} WAV(s) found in {folder}")

    def _album_add_wavs(self) -> None:
        """Secondary affordance -- a single WAV is just a one-row mapping table."""
        start = self._browse_start or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Select a side WAV", start, "WAV files (*.wav)")
        if path:
            self._album_wavs = [Path(path)]
            self._rebuild_mapping_table()
            self._log(f"Source: {Path(path).name}")

    def _rebuild_mapping_table(self) -> None:
        """One row per scanned WAV; the side dropdown defaults to skip.

        The heuristics only pre-fill files that actually name a side. Anything
        else stays on skip, which is what makes a mixed folder safe: WAVs from
        another album are simply not part of this job.
        """
        self.mapping_table.setRowCount(len(self._album_wavs))
        if not self._album_wavs:
            self._album_mapping = []
            return

        num_sides = len(self._sides)
        self._album_mapping = (
            propose_wav_side_map(self._album_wavs, num_sides) if num_sides
            else [None] * len(self._album_wavs)
        )

        for row, wav in enumerate(self._album_wavs):
            self.mapping_table.setItem(row, 0, QTableWidgetItem(wav.name))
            combo = QComboBox()
            combo.addItem(SKIP_LABEL, None)
            for s in self._sides:
                combo.addItem(f"Side {side_letter(s.index)} ({s.track_count} tr)", s.index)
            want = self._album_mapping[row]
            combo.setCurrentIndex(0 if want is None else max(0, combo.findData(want)))
            combo.currentIndexChanged.connect(
                lambda _i, r=row, c=combo: self._mapping_changed(r, c))
            self.mapping_table.setCellWidget(row, 1, combo)

        if not num_sides:
            self._log("Source: look up a release (or Define sides) to choose sides.")
            return
        mapped = sum(1 for m in self._album_mapping if m is not None)
        self._log(f"Source: {len(self._album_wavs)} WAV(s) - {mapped} mapped by name, "
                  f"{len(self._album_wavs) - mapped} left on skip.")
        if self._analysis is None:
            self._set_empty_state(self._pending_review_message())

    def _mapping_changed(self, row: int, combo: QComboBox) -> None:
        side_index = combo.currentData()
        self._album_mapping[row] = side_index
        if side_index is None:
            return
        # A side holds exactly one WAV: if another row already claimed this side,
        # release it rather than silently building an ambiguous job.
        for other in range(len(self._album_mapping)):
            if other != row and self._album_mapping[other] == side_index:
                self._album_mapping[other] = None
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
        if self._album is not None:
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
        self._album_output_root = output
        self._album_meta = {
            "artist": self._release.artist if self._release else cfg.last_artist,
            "album": self._release.title if self._release else cfg.last_album,
        }
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
            max_analysis_workers=cfg.album_analysis_workers, max_encode_workers=1)
        self.side_list.clear()
        for side in sides:
            item = QListWidgetItem(f"{side.label} - {side.state.value}")
            item.setData(Qt.ItemDataRole.UserRole, side.index)
            self.side_list.addItem(item)
        self.cancel_album_btn.setEnabled(True)
        self.start_album_btn.setEnabled(False)
        self._album.start()
        self._log(f"Album: started ({len(sides)} sides, {cfg.album_analysis_workers} analysis worker(s)).")

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
                break
        if side.state == SideState.READY:
            self._log(f"Album: {side.label} ready for review.")
        elif side.state == SideState.ERROR:
            self._log(f"Album: {side.label} ERROR - {side.error}")
        elif side.state == SideState.DONE:
            self._log(f"Album: {side.label} done.")
        if self._album_review_index is None:
            self._set_empty_state(self._pending_review_message())
        if self._album and all(s.state in (SideState.DONE, SideState.ERROR, SideState.CANCELLED)
                               for s in self._album.sides):
            self.cancel_album_btn.setEnabled(False)
            self.start_album_btn.setEnabled(True)

    def _on_side_list_click(self, item) -> None:
        if self._album is None:
            return
        side = self._album_side(item.data(Qt.ItemDataRole.UserRole))
        if side is None or side.analysis is None:
            if side is not None:
                self._log(f"Album: {side.label} not ready yet ({side.state.value}).")
            return
        if side.index == self._album_review_index:
            return                                  # already open
        if not self._confirm_discard_review():
            return
        if side.state in (SideState.READY, SideState.RESOLVING):
            self._album.mark_resolving(side.index)
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
        self.accept_button.setText("Accept side")
        if self._unresolved:
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
        if not self.output_edit.text().strip():
            self._log("Album: choose an output folder first.")
            return

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

        n = len(side.titles) or None
        if n and wrong_side_suspected(n, len(proposal.unresolved)):
            raise RuntimeError(
                f"wrong side/release suspected: {len(proposal.unresolved)} of {n - 1} "
                "boundaries unresolved")
        envelope = load_peak_envelope(restored)
        return AnalyzeResult(result, proposal, envelope, restored)

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
                                      should_cancel=should_cancel)
        for warning in batch.warnings:
            self._log(f"  ! {side.label}: {warning}")

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
