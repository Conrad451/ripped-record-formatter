"""Background worker that runs a core batch operation off the GUI thread.

A :class:`QRunnable` is scheduled on the global :class:`QThreadPool`. It first
calls ``configure_pydub`` (which may download ffmpeg on the very first run -- so
it must never run on the GUI thread) and then the conversion/re-tag operation,
reporting everything back to the GUI through queued Qt signals.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Signal


class WorkerSignals(QObject):
    progress = Signal(int, int, str)   # current, total, track_name
    log = Signal(str)
    finished = Signal(object)          # core.converter.BatchResult
    error = Signal(str)


class ConversionWorker(QRunnable):
    """Runs ``operation(tracks, output_dir, ...)`` in a pool thread."""

    def __init__(self, operation: Callable, tracks: list, output_dir: Path, **kwargs):
        super().__init__()
        self._operation = operation
        self._tracks = tracks
        self._output_dir = output_dir
        self._kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.log.emit("Preparing ffmpeg (first run may download it)...")
            from core.ffmpeg_locator import configure_pydub

            ffmpeg_path = configure_pydub()
            self.signals.log.emit(f"ffmpeg ready: {ffmpeg_path}")

            def on_progress(current: int, total: int, name: str) -> None:
                self.signals.progress.emit(current, total, name)
                self.signals.log.emit(f"[{current}/{total}] {name}")

            result = self._operation(
                self._tracks,
                self._output_dir,
                on_progress=on_progress,
                configure=False,
                **self._kwargs,
            )
            self.signals.finished.emit(result)
        except Exception as exc:  # never let a worker crash take down the app
            self.signals.error.emit(
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
