"""Audition playback for the side under review.

Splits on gapless material cannot be judged by eye. The gesture this exists to
serve is: **place, preview, nudge, preview, accept** -- jump to a few seconds
before a cut, listen through it, and hear whether it lands in silence or halfway
through a note.

What it plays is the **restored staged WAV** -- the exact audio the cuts will be
applied to -- never the raw source. Hearing a cut against material that is not
what gets cut would be worse than not hearing it at all.

Two things this module is careful about:

* **It must never crash the review flow.** A machine with no audio backend, or a
  Qt build without the multimedia plugin, gets :attr:`AuditionPlayer.available`
  ``False`` and disabled controls -- not an exception. Import of QtMultimedia is
  itself guarded, because it is the part most likely to be absent.
* **It must not hold the file open.** Staging is deleted after a side is accepted,
  and on Windows an open handle makes that delete fail. :meth:`stop` clears the
  media source, which is what actually releases the handle -- pausing does not.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

_BACKEND_ERROR = ""
try:  # QtMultimedia is the piece most likely to be missing on a given machine.
    from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

    _HAVE_QTMULTIMEDIA = True
except Exception as exc:  # pragma: no cover - depends on the Qt build
    _HAVE_QTMULTIMEDIA = False
    _BACKEND_ERROR = f"QtMultimedia is not available ({exc})"


class AuditionPlayer(QObject):
    """Play/pause/seek over one staged WAV, with a position feed for the waveform."""

    #: Playback position, in seconds. Throttled -- see ``notify_interval_ms``.
    positionChanged = Signal(float)
    #: True when playing, False when paused/stopped.
    playingChanged = Signal(bool)
    #: Human-readable problem (bad codec, missing backend). Never raised.
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None, *, notify_interval_ms: int = 50) -> None:
        super().__init__(parent)
        self._player = None
        self._output = None
        self._source: Path | None = None
        self._stop_at: float | None = None
        # A seek issued before the backend has finished loading is silently
        # dropped by QMediaPlayer -- which is exactly what happens when the user
        # hits "Preview cut" the moment a side opens. Hold it and apply on load.
        self._pending_seek: float | None = None
        self.unavailable_reason = _BACKEND_ERROR

        if not _HAVE_QTMULTIMEDIA:
            return
        if not QMediaDevices.audioOutputs():
            self.unavailable_reason = "no audio output device"
            return

        try:
            self._output = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._output)
            # Coarse enough not to flood the GUI thread, fine enough that the
            # cursor looks like it is moving rather than stepping.
            self._player.setNotifyInterval(notify_interval_ms) if hasattr(
                self._player, "setNotifyInterval") else None
            self._player.positionChanged.connect(self._on_position)
            self._player.playbackStateChanged.connect(self._on_state)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(
                lambda _e, msg: self.errorOccurred.emit(msg or "playback error"))
        except Exception as exc:  # pragma: no cover - defensive
            self._player = None
            self._output = None
            self.unavailable_reason = f"could not start audio ({exc})"

    # -- availability --------------------------------------------------------
    @property
    def available(self) -> bool:
        """Whether playback can happen at all. False => controls stay disabled."""
        return self._player is not None

    # -- source --------------------------------------------------------------
    def set_source(self, path: Path | None) -> None:
        """Point the player at a staged WAV (or ``None`` to release everything)."""
        if not self.available:
            return
        self.stop()
        self._source = Path(path) if path else None
        self._player.setSource(
            QUrl.fromLocalFile(str(self._source)) if self._source else QUrl())

    # -- transport -----------------------------------------------------------
    def play(self) -> None:
        if self.available and self._source is not None:
            self._player.play()

    def pause(self) -> None:
        if self.available:
            self._player.pause()

    def toggle(self) -> None:
        if not self.available:
            return
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """Stop and **release the file**.

        Clearing the source is the part that matters: a merely-stopped
        QMediaPlayer can still hold the handle open, and staging cleanup then
        fails on Windows with a sharing violation.
        """
        if not self.available:
            return
        self._stop_at = None
        self._pending_seek = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._source = None
        self.positionChanged.emit(0.0)

    def is_playing(self) -> bool:
        if not self.available:
            return False
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def seek(self, seconds: float) -> None:
        if not self.available:
            return
        target = max(0, int(seconds * 1000))
        if self._player.duration() <= 0:
            self._pending_seek = target / 1000.0     # not loaded yet; apply on load
            return
        self._player.setPosition(target)

    def position(self) -> float:
        return self._player.position() / 1000.0 if self.available else 0.0

    # -- the audition gestures ----------------------------------------------
    def preview_cut(self, timestamp: float, lead_in: float = 5.0) -> None:
        """Play from ``lead_in`` seconds before a cut, straight through it.

        The whole point: you hear the approach *and* the cut, so the ear decides
        whether it lands in silence or mid-note.
        """
        if not self.available:
            return
        self._stop_at = None
        self.seek(max(0.0, timestamp - lead_in))
        self.play()

    def play_window(self, start: float, end: float) -> None:
        """Play just an unresolved gap's window, so the segue is heard before placing."""
        if not self.available:
            return
        self.seek(max(0.0, start))
        self._stop_at = end
        self.play()

    # -- internals -----------------------------------------------------------
    def _on_position(self, ms: int) -> None:
        seconds = ms / 1000.0
        if self._stop_at is not None and seconds >= self._stop_at:
            self._stop_at = None
            self.pause()
        self.positionChanged.emit(seconds)

    def _on_state(self, state) -> None:
        self.playingChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_media_status(self, status) -> None:
        loaded = status in (QMediaPlayer.MediaStatus.LoadedMedia,
                            QMediaPlayer.MediaStatus.BufferedMedia)
        if loaded and self._pending_seek is not None:
            self._player.setPosition(max(0, int(self._pending_seek * 1000)))
            self._pending_seek = None


def transcode_for_preview(src: Path, dest: Path) -> Path:
    """Rewrite ``src`` as 16-bit PCM so any audio backend will take it.

    The staged WAV is normally already PCM_16 (``restore`` quantises its final
    write back to the source subtype), so this is a fallback, not the path. It
    exists because Windows' media backends are unreliable with float WAVs, and a
    rip whose source was float would otherwise be un-auditionable.
    """
    import soundfile as sf

    data, samplerate = sf.read(str(src), dtype="float32", always_2d=True)
    sf.write(str(dest), data, samplerate, subtype="PCM_16")
    return dest
