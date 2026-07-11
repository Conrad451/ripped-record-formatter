"""Full Rip tab: one side-long WAV -> restored -> split -> tagged FLAC tracks.

The centrepiece workflow. A single restoration pass produces the audio that both
the split proposal and the final tracks come from -- tracks are never
re-processed. Everything heavy (restore, propose, split, convert) runs on the
QThreadPool; the waveform stays interactive throughout.
"""

from __future__ import annotations

import shutil
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import job_settings
from core.split_review import detect_progressive_drift, segment_deviations
from gui.track_model import Row, TrackTableModel, TrackTableView
from gui.waveform import WaveformView


def _mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
@dataclass
class AnalyzeResult:
    restoration: object          # core.restoration.RestorationResult
    proposal: object             # core.splitting.SplitProposal
    envelope: object             # core.waveform.WaveformEnvelope
    restored_path: Path


class _Signals(QObject):
    progress = Signal(str)
    finished = Signal(object)
    error = Signal(str)


class AnalyzeWorker(QRunnable):
    """restore() -> proposal (best mode) -> waveform envelope, off-thread."""

    def __init__(self, source, work_dir, stages, policy, params,
                 expected_durations_ms, track_count, window_s, speed_tolerance):
        super().__init__()
        self.source = Path(source)
        self.work_dir = Path(work_dir)
        self.stages = stages
        self.policy = policy
        self.params = params
        self.expected_durations_ms = expected_durations_ms
        self.track_count = track_count
        self.window_s = window_s
        self.speed_tolerance = speed_tolerance
        self.signals = _Signals()

    def run(self) -> None:
        try:
            from core.restoration import restore
            from core.splitting import propose_splits, propose_splits_anchored
            from core.waveform import load_peak_envelope

            restored = self.work_dir / "restored.wav"
            self.signals.progress.emit("Restoring side (this can take a while)...")

            def on_stage(name, idx, total):
                self.signals.progress.emit(f"  restore [{idx}/{total}] {name}")

            result = restore(self.source, restored, self.stages,
                             on_progress=on_stage, policy=self.policy)

            self.signals.progress.emit("Proposing splits on the restored audio...")
            if self.expected_durations_ms:
                proposal = propose_splits_anchored(
                    restored, self.expected_durations_ms, params=self.params,
                    window_s=self.window_s, speed_tolerance=self.speed_tolerance,
                )
            elif self.track_count:
                proposal = propose_splits(restored, track_count=self.track_count, params=self.params)
            else:
                proposal = propose_splits(restored, params=self.params)

            self.signals.progress.emit("Computing waveform...")
            envelope = load_peak_envelope(restored)
            self.signals.finished.emit(AnalyzeResult(result, proposal, envelope, restored))
        except Exception as exc:
            self.signals.error.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class AcceptWorker(QRunnable):
    """execute_split() on the restored WAV, then convert+tag+cover the segments."""

    def __init__(self, restored, timestamps, split_dir, output_dir,
                 titles, album, artist, cover):
        super().__init__()
        self.restored = Path(restored)
        self.timestamps = timestamps
        self.split_dir = Path(split_dir)
        self.output_dir = Path(output_dir)
        self.titles = titles
        self.album = album
        self.artist = artist
        self.cover = cover
        self.signals = _Signals()

    def run(self) -> None:
        try:
            from core.converter import convert_wavs_to_flacs
            from core.ffmpeg_locator import configure_pydub
            from core.splitting import execute_split
            from core.tracks import Tracks

            self.signals.progress.emit("Cutting tracks...")
            segments = execute_split(
                self.restored, self.timestamps, self.split_dir,
                on_progress=lambda i, t, n: self.signals.progress.emit(f"  cut [{i}/{t}] {n}"),
            )

            tracks = []
            for i, seg in enumerate(segments):
                title = self.titles[i] if i < len(self.titles) else f"track_{i + 1:02d}"
                tracks.append(Tracks(i + 1, title, self.album, self.artist, seg))

            self.signals.progress.emit("Encoding + tagging FLACs...")
            configure_pydub()
            batch = convert_wavs_to_flacs(
                tracks, self.output_dir,
                on_progress=lambda i, t, n: self.signals.progress.emit(f"  encode [{i}/{t}] {n}"),
                configure=False, cover=self.cover,
            )
            self.signals.finished.emit(batch)
        except Exception as exc:
            self.signals.error.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


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
        self._expected_n: int | None = None
        self._expected_titles: list[str] = []
        self._expected_durations_s: list[float] = []
        self._unresolved: list = []
        self._gap_idx = 0
        self._busy = False

        root = QVBoxLayout(self)

        form = QFormLayout()
        self.source_edit = QLineEdit(settings.config.source_dir)
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_source)
        src_row.addWidget(browse)
        form.addRow("Side-long WAV:", self._wrap(src_row))

        self.output_edit = QLineEdit(settings.config.output_dir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_edit, 1)
        out_browse = QPushButton("Browse...")
        out_browse.clicked.connect(self._browse_output)
        out_row.addWidget(out_browse)
        form.addRow("Output folder:", self._wrap(out_row))

        # side picker (release) OR manual count
        side_row = QHBoxLayout()
        self.side_combo = QComboBox()
        self.side_combo.setEnabled(False)
        self.side_combo.currentIndexChanged.connect(self._on_side_changed)
        side_row.addWidget(self.side_combo, 1)
        side_row.addWidget(QLabel("or tracks:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 40)
        self.count_spin.setValue(1)
        self.count_spin.valueChanged.connect(self._on_count_changed)
        side_row.addWidget(self.count_spin)
        form.addRow("Side:", self._wrap(side_row))
        root.addLayout(form)

        self.analyze_button = QPushButton("Analyze")
        self.analyze_button.clicked.connect(self._analyze)
        root.addWidget(self.analyze_button)

        # waveform
        self.waveform = WaveformView()
        self.waveform.markersChanged.connect(self._on_markers_changed)
        root.addWidget(self.waveform, 1)

        # gap guidance
        self.gap_box = QGroupBox("Resolve gap")
        gap_layout = QHBoxLayout(self.gap_box)
        self.gap_prompt = QLabel("")
        gap_layout.addWidget(self.gap_prompt, 1)
        self.prev_gap_btn = QPushButton("◂ Prev")
        self.prev_gap_btn.clicked.connect(lambda: self._present_gap(self._gap_idx - 1))
        self.next_gap_btn = QPushButton("Next ▸")
        self.next_gap_btn.clicked.connect(lambda: self._present_gap(self._gap_idx + 1))
        gap_layout.addWidget(self.prev_gap_btn)
        gap_layout.addWidget(self.next_gap_btn)
        self.gap_box.setVisible(False)
        root.addWidget(self.gap_box)

        # marker controls + accept
        ctl = QHBoxLayout()
        add_btn = QPushButton("Add split at view center")
        add_btn.clicked.connect(self.waveform.add_marker_at_center)
        ctl.addWidget(add_btn)
        self.override_check = QCheckBox("Split into fewer tracks anyway")
        self.override_check.toggled.connect(self._update_accept_enabled)
        ctl.addWidget(self.override_check)
        ctl.addStretch(1)
        self.marker_status = QLabel("")
        ctl.addWidget(self.marker_status)
        self.accept_button = QPushButton("Accept && Export")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self._accept)
        ctl.addWidget(self.accept_button)
        root.addLayout(ctl)

        # resulting track table
        self.model = TrackTableModel()
        self.table = TrackTableView()
        self.table.setModel(self.model)
        root.addWidget(self.table, 1)

        self.progress = QProgressBar()
        root.addWidget(self.progress)

    # -- small helpers ------------------------------------------------------
    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _log(self, msg: str) -> None:
        self.logMessage.emit(msg)

    def _browse_source(self) -> None:
        start = self.source_edit.text().strip() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(self, "Select side-long WAV", start, "WAV files (*.wav)")
        if chosen:
            self.source_edit.setText(chosen)

    def _browse_output(self) -> None:
        start = self.output_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select output folder", start)
        if chosen:
            self.output_edit.setText(chosen)
            self.settings.set(output_dir=chosen)

    # -- release / side -----------------------------------------------------
    def set_release(self, detail) -> None:
        """Called when a MusicBrainz release is selected: fill the side picker."""
        self._release = detail
        self._cover = detail.cover
        self.side_combo.blockSignals(True)
        self.side_combo.clear()
        for medium in detail.media:
            fmt = f" ({medium.format})" if medium.format else ""
            self.side_combo.addItem(f"Side {medium.position}{fmt} - {len(medium.tracks)} tracks", medium.position)
        self.side_combo.setEnabled(detail.media != ())
        self.side_combo.blockSignals(False)
        self.count_spin.setEnabled(not detail.media)
        if detail.media:
            self.side_combo.setCurrentIndex(0)
            self._on_side_changed()
        self._log(f"Full Rip: release '{detail.title}' loaded ({len(detail.media)} side(s)).")

    def _current_medium(self):
        if not self._release or not self._release.media:
            return None
        idx = self.side_combo.currentIndex()
        if 0 <= idx < len(self._release.media):
            return self._release.media[idx]
        return None

    def _on_side_changed(self, *_) -> None:
        medium = self._current_medium()
        if medium is None:
            return
        self._expected_titles = [t.title for t in medium.tracks]
        self._expected_n = len(medium.tracks)
        lengths = [t.length_ms for t in medium.tracks]
        if all(v is not None for v in lengths) and lengths:
            self._expected_durations_s = [v / 1000.0 for v in lengths]
        else:
            self._expected_durations_s = []

    def _on_count_changed(self, value: int) -> None:
        if self._release is None:
            self._expected_n = value
            self._expected_titles = []
            self._expected_durations_s = []

    # -- analyze ------------------------------------------------------------
    def _analyze(self) -> None:
        if self._busy:
            return
        source = self.source_edit.text().strip()
        if not source or not Path(source).is_file():
            self._log(f"Full Rip: source WAV not found: {source!r}")
            return
        if self._release is None:
            self._expected_n = self.count_spin.value()

        cfg = self.settings.config
        stages = job_settings.build_stages(cfg)
        policy = job_settings.build_policy(cfg)
        params = job_settings.build_silence_params(cfg)

        expected_ms = None
        track_count = None
        if self._expected_durations_s:
            expected_ms = [int(round(s * 1000)) for s in self._expected_durations_s]
            self._log("Full Rip: anchored mode (per-track durations available).")
        elif self._expected_n:
            track_count = self._expected_n
            self._log(f"Full Rip: count mode (N={track_count}).")
        else:
            self._log("Full Rip: threshold mode (no tracklist).")

        # Fresh staging dir for this analysis.
        self._cleanup_work_dir()
        self._work_dir = Path(tempfile.mkdtemp(prefix="rrf_fullrip_"))

        self._set_busy(True)
        self.progress.setRange(0, 0)  # indeterminate during analyze
        worker = AnalyzeWorker(source, self._work_dir, stages, policy, params,
                               expected_ms, track_count, cfg.window_s, cfg.speed_tolerance)
        worker.signals.progress.connect(self._log)
        worker.signals.finished.connect(self._on_analyze_done)
        worker.signals.error.connect(self._on_error)
        self.pool.start(worker)

    def _on_analyze_done(self, analysis: AnalyzeResult) -> None:
        self._analysis = analysis
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._set_busy(False)

        self.waveform.set_envelope(analysis.envelope)
        proposal = analysis.proposal
        self.waveform.set_markers(
            [p.timestamp for p in proposal.split_points],
            [p.confidence for p in proposal.split_points],
        )
        self._log(f"Full Rip: proposal mode='{proposal.mode}', "
                  f"{len(proposal.split_points)} cut(s), {len(proposal.unresolved)} unresolved.")

        # Diagnostics from restoration.
        res = analysis.restoration
        if res.source_clip_runs > 0:
            self._log(f"Source appears clipped at {res.source_clip_runs} point(s) - consider re-ripping at lower gain.")
        if res.peak_gain_db < 0:
            self._log(f"Output attenuated {abs(res.peak_gain_db):.1f} dB to prevent clipping.")
        for w in res.warnings:
            self._log(f"  ! {w}")

        # Off-by-one / wrong-side drift on the proposed cuts.
        self._check_drift(auto=True)

        # Unresolved gaps -> guided resolution.
        self._unresolved = list(proposal.unresolved)
        if self._unresolved:
            self._gap_idx = 0
            self.gap_box.setVisible(True)
            self._present_gap(0)
        else:
            self.gap_box.setVisible(False)
            self.waveform.clear_region()
            self.waveform.zoom_full()
        self._update_accept_enabled()

    # -- gap resolution -----------------------------------------------------
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
            f"{gap.track_index + 1}, expected near {_mmss(gap.expected_ts)} - place it. "
            f"({placed}/{needed} markers placed)"
        )

    # -- markers / accept gating -------------------------------------------
    def _on_markers_changed(self) -> None:
        self._update_gap_prompt()
        self._update_accept_enabled()
        self._soft_hint_deviations()

    def _needed_markers(self) -> int | None:
        return (self._expected_n - 1) if self._expected_n else None

    def _update_accept_enabled(self) -> None:
        if self._busy or self._analysis is None:
            self.accept_button.setEnabled(False)
            return
        count = self.waveform.marker_count()
        needed = self._needed_markers()
        if self.override_check.isChecked() or needed is None:
            ok = True
        else:
            ok = count == needed
        self.accept_button.setEnabled(ok)
        if needed is not None:
            self.marker_status.setText(f"{count}/{needed} splits")
        else:
            self.marker_status.setText(f"{count} splits")

    def _soft_hint_deviations(self) -> None:
        """Soft per-segment hint when a user-placed marker gives an off length."""
        if not self._expected_durations_s or self._analysis is None:
            return
        segs = self._segment_durations()
        flags = segment_deviations(segs, self._expected_durations_s)
        off = [i + 1 for i, f in enumerate(flags) if f]
        if off:
            self.marker_status.setToolTip(
                "Segments far from expected length: " + ", ".join(map(str, off))
            )
        else:
            self.marker_status.setToolTip("")

    def _segment_durations(self) -> list[float]:
        if self._analysis is None:
            return []
        total = self._analysis.proposal.duration
        cuts = sorted(self.waveform.marker_times())
        bounds = [0.0, *cuts, total]
        return [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]

    def _check_drift(self, auto: bool) -> None:
        if not self._expected_durations_s:
            return
        segs = self._segment_durations()
        if detect_progressive_drift(segs, self._expected_durations_s):
            self._log("Splits drift from the tracklist - wrong side selected?")

    # -- accept -------------------------------------------------------------
    def _accept(self) -> None:
        if self._busy or self._analysis is None:
            return
        output = self.output_edit.text().strip()
        if not output:
            self._log("Full Rip: choose an output folder first.")
            return
        timestamps = self.waveform.marker_times()
        titles = list(self._expected_titles)
        artist = self._release.artist if self._release else self.settings.config.last_artist
        album = self._release.title if self._release else self.settings.config.last_album

        # Populate the resulting-track table (titles known up front).
        n_segments = len(timestamps) + 1
        rows = [Row(title=(titles[i] if i < len(titles) else f"track_{i + 1:02d}"))
                for i in range(n_segments)]
        self.model.set_rows(rows)

        split_dir = self._work_dir / "segments"
        self._set_busy(True)
        self.progress.setRange(0, 0)
        worker = AcceptWorker(self._analysis.restored_path, timestamps, split_dir,
                              output, titles, album, artist, self._cover)
        worker.signals.progress.connect(self._log)
        worker.signals.finished.connect(self._on_accept_done)
        worker.signals.error.connect(self._on_error)
        self.pool.start(worker)

    def _on_accept_done(self, batch) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._set_busy(False)
        self._log(batch.summary())
        for w in batch.warnings:
            self._log(f"  ! {w}")

    # -- misc ---------------------------------------------------------------
    def _on_error(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self._set_busy(False)
        self._log(f"ERROR: {message}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.analyze_button.setEnabled(not busy)
        if busy:
            self.accept_button.setEnabled(False)
        else:
            self._update_accept_enabled()

    def _cleanup_work_dir(self) -> None:
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

    def cleanup(self) -> None:
        self._cleanup_work_dir()
