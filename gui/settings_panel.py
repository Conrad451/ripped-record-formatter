"""Settings panel: every restoration/splitting tunable, persisted via config.

Defaults-first: the handful of controls people actually reach for sit at the top;
everything else lives behind a collapsible *Advanced* disclosure. Each control is
bound to one :class:`core.config.Config` field through the shared ``Settings``
wrapper, so a change is saved immediately. The restoration chain order is fixed
(rumble -> hum -> noise -> declick); only per-stage enable toggles are offered.

Intermediate-format internals (staging subtype/codec) are design invariants and
are deliberately *not* exposed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.text_styles import apply_muted
from core.metadata_lookup import MAX_CONTACT_LENGTH, sanitize_contact

#: Post-album folder policy choices, mirrored from core.config.
_POLICY_CHOICES = (("Keep last used", "keep"),
                   ("Reset to default", "reset"),
                   ("Clear", "clear"))


class CollapsibleBox(QWidget):
    """A titled disclosure: click the header to show/hide its content."""

    def __init__(self, title: str, expanded: bool = False):
        super().__init__()
        self._button = QToolButton()
        self._button.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(expanded)
        self._button.clicked.connect(self._toggle)

        self._content = QWidget()
        self._content.setVisible(expanded)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._button)
        root.addWidget(self._content)

    def _toggle(self, checked: bool) -> None:
        self._button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._content.setVisible(checked)

    def content_layout(self, layout) -> None:
        self._content.setLayout(layout)


class SettingsPanel(QWidget):
    """Bind every tunable to config; save on change."""

    changed = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        cfg = settings.config

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        root = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # ---------------- Basic ----------------
        stages_box = QGroupBox("Restoration stages (applied in this order)")
        stages_form = QFormLayout(stages_box)
        stages_form.addRow(self._check("Rumble filter (subsonic high-pass)", "rumble_enabled", cfg.rumble_enabled))
        stages_form.addRow(self._check("Hum removal (mains notch)", "hum_enabled", cfg.hum_enabled))
        stages_form.addRow(self._check("Noise reduction (spectral gate)", "noise_enabled", cfg.noise_enabled))
        stages_form.addRow(self._check("Declick (ffmpeg adeclick)", "declick_enabled", cfg.declick_enabled))
        root.addWidget(stages_box)

        basic_box = QGroupBox("Common controls")
        basic = QFormLayout(basic_box)
        # Hum region 60/50 selector
        self.region_combo = QComboBox()
        self.region_combo.addItem("60 Hz (Americas)", 60.0)
        self.region_combo.addItem("50 Hz (Europe/Asia)", 50.0)
        self.region_combo.setCurrentIndex(0 if cfg.hum_base_freq >= 55 else 1)
        self.region_combo.currentIndexChanged.connect(
            lambda _i: self._save(hum_base_freq=self.region_combo.currentData())
        )
        basic.addRow("Mains frequency:", self.region_combo)
        # Noise strength slider
        basic.addRow("Noise strength:", self._strength_slider(cfg.noise_strength))
        # Headroom
        basic.addRow("Output headroom (dBFS):",
                     self._dspin("headroom_target_dbfs", cfg.headroom_target_dbfs, -6.0, 0.0, 0.1, 2))
        basic.addRow("Parallel encode jobs:",
                     self._ispin("encode_workers", cfg.encode_workers, 1, 16))
        basic.addRow("Album analysis workers:",
                     self._ispin("album_analysis_workers", cfg.album_analysis_workers, 1, 4))
        # Album output is one flat folder, so filenames must not collide across
        # sides. Off: continuous [01]..[NN]. On: [A01]/[B01]. Tags are unaffected.
        basic.addRow(self._check(
            "Name files [A01]/[B01] by side (off: continuous [01]-[NN])",
            "filename_side_letters", cfg.filename_side_letters))
        root.addWidget(basic_box)

        # ---------------- Default folders ----------------
        folders_box = QGroupBox("Default folders")
        folders = QFormLayout(folders_box)
        # Named for what lives in them, not for which field they populate. The
        # two roots have genuinely different jobs -- one holds masters you keep
        # forever, the other holds the library you play -- and calling them
        # "source" and "output" made that a thing you had to already know.
        folders.addRow("WAV root (recordings & masters):",
                       self._folder_row("default_source_dir", cfg.default_source_dir))
        folders.addRow("When an album finishes, the WAV root:",
                       self._policy_combo("source_post_album_policy", cfg.source_post_album_policy))
        folders.addRow("FLAC root (finished library):",
                       self._folder_row("default_output_dir", cfg.default_output_dir))
        folders.addRow("When an album finishes, the FLAC root:",
                       self._policy_combo("output_post_album_policy", cfg.output_post_album_policy))
        folders_hint = QLabel(
            "Recordings are saved under the WAV root; finished FLACs go under the "
            "FLAC root. The Artist/Album and the looked-up release are always "
            "cleared when an album finishes -- no default is safe for identity. "
            "Only these folders follow a policy.")
        folders_hint.setWordWrap(True)
        apply_muted(folders_hint)
        folders.addRow(folders_hint)
        root.addWidget(folders_box)

        # ---------------- Metadata lookup ----------------
        lookup_box = QGroupBox("Metadata lookup")
        lookup = QFormLayout(lookup_box)
        self.contact_edit = QLineEdit(cfg.metadata_contact)
        self.contact_edit.setPlaceholderText("you@example.com")
        self.contact_edit.setMaxLength(MAX_CONTACT_LENGTH)
        # Saved as you leave the field, not per keystroke -- a half-typed address
        # is not worth a config write, and it would be the one on the wire if you
        # searched mid-edit.
        self.contact_edit.editingFinished.connect(self._save_contact)
        lookup.addRow("MusicBrainz contact (email or URL):", self.contact_edit)
        hint = QLabel(
            "MusicBrainz asks API users to provide a contact so they can reach "
            "you about traffic issues. Optional, but courteous."
        )
        hint.setWordWrap(True)
        apply_muted(hint)
        lookup.addRow(hint)
        root.addWidget(lookup_box)

        # ---------------- Advanced ----------------
        advanced = CollapsibleBox("Advanced", expanded=False)
        adv = QFormLayout()
        adv.addRow(QLabel("<b>Rumble filter</b>"))
        adv.addRow("Cutoff (Hz):", self._dspin("rumble_cutoff_hz", cfg.rumble_cutoff_hz, 1.0, 200.0, 1.0, 1))
        adv.addRow("Order:", self._ispin("rumble_order", cfg.rumble_order, 1, 12))
        adv.addRow(QLabel("<b>Hum removal</b>"))
        adv.addRow("Harmonics:", self._ispin("hum_harmonics", cfg.hum_harmonics, 1, 12))
        adv.addRow("Quality (Q):", self._dspin("hum_quality", cfg.hum_quality, 1.0, 100.0, 1.0, 1))
        adv.addRow(QLabel("<b>Noise reduction</b>"))
        adv.addRow("Profile start (s):", self._dspin("noise_profile_start", cfg.noise_profile_start, 0.0, 600.0, 0.5, 2))
        adv.addRow("Profile duration (s):", self._dspin("noise_profile_duration", cfg.noise_profile_duration, 0.1, 60.0, 0.5, 2))
        adv.addRow(QLabel("<b>Split detection</b>"))
        adv.addRow("Silence threshold (dBFS):", self._dspin("silence_threshold_db", cfg.silence_threshold_db, -90.0, -10.0, 1.0, 1))
        adv.addRow("Min silence (s):", self._dspin("min_silence", cfg.min_silence, 0.1, 10.0, 0.1, 2))
        adv.addRow("Min track length (s):", self._dspin("min_track_length", cfg.min_track_length, 1.0, 300.0, 1.0, 1))
        adv.addRow("Frame (ms):", self._dspin("frame_ms", cfg.frame_ms, 1.0, 200.0, 1.0, 1))
        adv.addRow("Hop (ms):", self._dspin("hop_ms", cfg.hop_ms, 1.0, 200.0, 1.0, 1))
        adv.addRow(QLabel("<b>Confidence scoring</b>"))
        adv.addRow("Depth ref (dB):", self._dspin("depth_ref_db", cfg.depth_ref_db, 1.0, 60.0, 1.0, 1))
        adv.addRow("Duration ref (s):", self._dspin("duration_ref_s", cfg.duration_ref_s, 0.1, 20.0, 0.1, 2))
        adv.addRow("Depth weight:", self._dspin("quality_depth_weight", cfg.quality_depth_weight, 0.0, 1.0, 0.05, 2))
        adv.addRow("Proximity weight:", self._dspin("proximity_weight", cfg.proximity_weight, 0.0, 1.0, 0.05, 2))
        adv.addRow("Post-miss penalty:", self._dspin("post_miss_penalty", cfg.post_miss_penalty, 0.0, 1.0, 0.05, 2))
        adv.addRow("Confidence digits:", self._ispin("confidence_round_digits", cfg.confidence_round_digits, 0, 8))
        adv.addRow("dB floor eps:", self._dspin("db_floor_eps", cfg.db_floor_eps, 1e-12, 1e-3, 1e-10, 12))
        adv.addRow(QLabel("<b>Audition playback</b>"))
        adv.addRow("Preview lead-in (s):",
                   self._dspin("preview_lead_in_s", cfg.preview_lead_in_s, 0.5, 30.0, 0.5, 1))
        adv.addRow("Marker nudge (ms):",
                   self._ispin("marker_nudge_ms", cfg.marker_nudge_ms, 1, 2000))
        adv.addRow(QLabel("<b>Review sanity guard</b>"))
        adv.addRow("Max unconfirmed boundaries (fraction):",
                   self._dspin("wrong_side_frac", cfg.wrong_side_frac, 0.0, 1.0, 0.05, 2))
        adv.addRow(QLabel("<b>Anchored search</b>"))
        adv.addRow("Window (s):", self._dspin("window_s", cfg.window_s, 1.0, 120.0, 1.0, 1))
        adv.addRow("Speed tolerance:", self._dspin("speed_tolerance", cfg.speed_tolerance, 0.0, 0.2, 0.005, 3))
        advanced.content_layout(adv)
        root.addWidget(advanced)
        root.addStretch(1)

    # -- default-folder controls --------------------------------------------
    def _folder_row(self, field: str, value: str) -> QWidget:
        """A path line-edit + Browse button, both saving to ``field``."""
        edit = QLineEdit(value)
        edit.setPlaceholderText("(none)")
        edit.editingFinished.connect(
            lambda e=edit, f=field: self._save(**{f: e.text().strip()}))
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda _=False, e=edit, f=field: self._browse_folder(e, f))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        wrapper = QWidget()
        wrapper.setLayout(row)
        return wrapper

    def _browse_folder(self, edit: QLineEdit, field: str) -> None:
        start = edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select a default folder", start)
        if chosen:
            edit.setText(chosen)
            self._save(**{field: chosen})

    def _policy_combo(self, field: str, value: str) -> QComboBox:
        """keep / reset / clear, mirroring :data:`_POLICY_CHOICES`."""
        combo = QComboBox()
        for label, data in _POLICY_CHOICES:
            combo.addItem(label, data)
        combo.setCurrentIndex(max(0, combo.findData(value or "keep")))
        combo.currentIndexChanged.connect(
            lambda _i, f=field, c=combo: self._save(**{f: c.currentData()}))
        return combo

    # -- persistence helpers -------------------------------------------------
    def _save(self, **fields) -> None:
        self.settings.set(**fields)
        self.changed.emit()

    def _save_contact(self) -> None:
        """Persist the contact as it will actually be sent -- sanitised.

        Storing the raw text and cleaning it only on the way out would leave the
        field showing something the app never sends. What you see here is the
        string that goes in the header.
        """
        clean = sanitize_contact(self.contact_edit.text())
        if clean != self.contact_edit.text():
            self.contact_edit.setText(clean)
        if clean != self.settings.config.metadata_contact:
            self._save(metadata_contact=clean)

    def _check(self, label: str, field: str, value: bool) -> QCheckBox:
        box = QCheckBox(label)
        box.setChecked(bool(value))
        box.toggled.connect(lambda v, f=field: self._save(**{f: bool(v)}))
        return box

    def _dspin(self, field: str, value: float, lo: float, hi: float, step: float, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.setValue(float(value))
        spin.valueChanged.connect(lambda v, f=field: self._save(**{f: float(v)}))
        return spin

    def _ispin(self, field: str, value: int, lo: int, hi: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setValue(int(value))
        spin.valueChanged.connect(lambda v, f=field: self._save(**{f: int(v)}))
        return spin

    def _strength_slider(self, value: float) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(int(round(value * 100)))
        label = QLabel(f"{value:.2f}")
        slider.valueChanged.connect(lambda v: (label.setText(f"{v / 100:.2f}"),
                                               self._save(noise_strength=v / 100.0)))
        row.addWidget(slider, 1)
        row.addWidget(label)
        return container
