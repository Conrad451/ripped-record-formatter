"""A dismissible bar offering to pick up an interrupted album.

Turntable time is unrepeatable, so losing a session to a crash costs something
that cannot simply be redone. But an offer to recover is still an offer: it is a
bar the user can ignore, not a modal that ambushes them on launch before they
have seen the app.

The copy is split by audience, deliberately. The bar carries the *consequence*
in the user's vocabulary -- the side has to be prepared again before it can be
reviewed -- because "re-analyse" is our word for our operation. The log carries
the precise version, staging and filenames and all, because whoever reads the
log wants exactly that.

Nothing here pretends. Staging is a ``mkdtemp`` and never survives a restart, so
resuming genuinely does re-prepare from the WAVs, and the bar says so rather
than implying the work was kept.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from gui.text_styles import apply_body


class ResumeBar(QFrame):
    """One line, two choices, and a way to make it go away."""

    resumeRequested = Signal()
    discardRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("resumeBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setVisible(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)

        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        apply_body(self.message_label)          # an offer about your work is content
        row.addWidget(self.message_label, 1)

        self.resume_button = QPushButton("Resume")
        self.resume_button.setToolTip(
            "Pick this album up again. The sides that were finished are left "
            "as they are; anything unfinished is prepared again from its WAV.")
        self.resume_button.clicked.connect(self._on_resume)
        row.addWidget(self.resume_button)

        self.discard_button = QPushButton("Discard")
        self.discard_button.setToolTip(
            "Forget this session. Nothing on disk is deleted.")
        self.discard_button.clicked.connect(self._on_discard)
        row.addWidget(self.discard_button)

    def offer(self, message: str) -> None:
        self.message_label.setText(message)
        self.setVisible(True)

    def dismiss(self) -> None:
        self.setVisible(False)

    def _on_resume(self) -> None:
        self.dismiss()
        self.resumeRequested.emit()

    def _on_discard(self) -> None:
        self.dismiss()
        self.discardRequested.emit()
