"""Dialog to (re)partition a flat tracklist into vinyl sides.

A thin view over :mod:`core.side_partition`: the user picks a number of sides and
adjusts each side boundary; track order is immutable (only boundary *positions*
move) because the model is a list of divider indices, never a reordering of
tracks. Live preview shows each side's track count and total time.

Movement is by per-boundary position spinboxes rather than literal row-dragging
-- same result, and it makes "order stays fixed" true by construction.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.side_partition import Side, default_dividers, partition
from core.timefmt import format_timestamp


def side_letter(index: int) -> str:
    """0 -> 'A', 1 -> 'B', ... (falls back to a number past 'Z')."""
    return chr(ord("A") + index) if index < 26 else str(index + 1)


class SideEditorDialog(QDialog):
    def __init__(self, titles: list[str], durations_ms: list[int], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Define sides")
        self._titles = list(titles)
        self._durations = list(durations_ms)
        n = len(self._durations)
        self.sides: list[Side] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"{n} tracks. Choose how many sides and where each begins."))

        form = QFormLayout()
        self.sides_spin = QSpinBox()
        self.sides_spin.setRange(1, max(1, n))
        self.sides_spin.setValue(min(2, max(1, n)))
        self.sides_spin.valueChanged.connect(self._rebuild_boundaries)
        form.addRow("Number of sides:", self.sides_spin)
        root.addLayout(form)

        self._boundary_form = QFormLayout()
        root.addLayout(self._boundary_form)
        self._boundary_spins: list[QSpinBox] = []

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._rebuild_boundaries()

    def _clear_boundary_form(self) -> None:
        while self._boundary_form.rowCount():
            self._boundary_form.removeRow(0)
        self._boundary_spins = []

    def _rebuild_boundaries(self) -> None:
        self._clear_boundary_form()
        num_sides = self.sides_spin.value()
        n = len(self._durations)
        dividers = default_dividers(self._durations, num_sides)
        for k, pos in enumerate(dividers, start=1):
            spin = QSpinBox()
            # bounds keep boundaries strictly increasing with >=1 track per side
            spin.setRange(k, n - (len(dividers) - k + 1) + 1)
            spin.setValue(pos)
            spin.valueChanged.connect(self._on_boundary_changed)
            self._boundary_form.addRow(
                f"Side {side_letter(k)} begins at track:", spin
            )
            self._boundary_spins.append(spin)
        self._update_preview()

    def _on_boundary_changed(self, *_) -> None:
        # Enforce strict ordering: each boundary at least one past the previous.
        prev = 0
        for spin in self._boundary_spins:
            if spin.value() <= prev:
                spin.blockSignals(True)
                spin.setValue(prev + 1)
                spin.blockSignals(False)
            prev = spin.value()
        self._update_preview()

    def _current_dividers(self) -> list[int]:
        return [spin.value() for spin in self._boundary_spins]

    def _update_preview(self) -> None:
        sides = partition(self._durations, self.sides_spin.value(), self._current_dividers())
        parts = []
        for s in sides:
            parts.append(
                f"Side {side_letter(s.index)} - {s.track_count} tracks "
                f"({format_timestamp(s.total_ms / 1000)})"
            )
        self.preview.setText("   |   ".join(parts))

    def _accept(self) -> None:
        self.sides = partition(self._durations, self.sides_spin.value(), self._current_dividers())
        self.accept()

    def side_labels(self) -> list[str]:
        return [
            f"Side {side_letter(s.index)} - {s.track_count} tracks "
            f"({format_timestamp(s.total_ms / 1000)})"
            for s in self.sides
        ]
