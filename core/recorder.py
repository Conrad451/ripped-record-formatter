"""Capture from an input device straight to disk. GUI-free.

The appliance frame: the only decisions a user makes are *which input* (once) and
*press Record*. Capture shows no live waveform and does no editing. Optional
software monitoring -- hearing the input on an output device -- lives in
:class:`Passthrough`, deliberately a separate object from :class:`Recorder` with
its own streams and buffer, so a monitor glitch can never touch a capture. What
this module owes the caller is a WAV that is honest -- correct length, correct
format, and loud about any way in which it might be damaged.

Three things it is careful about:

* **The audio callback never blocks.** PortAudio calls it on a realtime thread;
  one blocking disk write there is a dropout. The callback copies its frames into
  a *bounded* queue and returns. A writer thread drains the queue to disk via
  soundfile. A side-long capture therefore streams -- it never accumulates in RAM.
  If the queue ever fills (disk stalled), we drop the block and *say so* rather
  than block the callback and corrupt the stream.
* **Staging is local.** The destination is typically a network share; a live
  capture must not be writing across one. We record to a local temp file and move
  it on stop -- and the file handle is closed *before* the move, because Windows
  will not rename a file that is still open.
* **Dropouts are never silent.** PortAudio's overflow flags, dropped queue blocks
  and a vanished device all land in :attr:`RecordingResult.warnings`. A damaged
  capture that looks fine is worse than one that admits it.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

# Clip detection, deliberately the same semantics as
# core.restoration._count_clip_runs: a frame counts as clipped when any channel
# reaches full scale, and it takes a *run* of them to count as clipping -- one
# stray sample at full scale is not a clip.
CLIP_LEVEL = 0.999
CLIP_RUN_LEN = 3

#: Digital full scale, and the ceiling every reported level is measured against.
#:
#: WASAPI **shared mode hands us float32 that is not bounded to ±1.0**. Windows
#: mixes, applies the endpoint volume and runs any APO processing in the float
#: domain, so a device whose hardware is 16-bit still delivers samples above
#: unity -- and ``_to_dbfs`` faithfully turns 1.2303 into +1.8 dBFS. That is the
#: "max 1.8 dBFS (0.0 dB headroom)" from the field: not an arithmetic error, but
#: a level measured in the wrong domain.
#:
#: The meter is a statement about *the recording*, and the recording is an
#: integer file that saturates here: write that same 1.2303 sample as PCM_16 and
#: it reads back 0.999969. So the peak is clamped to full scale at the moment it
#: is measured. Nothing is lost by doing so -- a sample over the ceiling is
#: clipping, ``CLIP_LEVEL`` catches it, and the clip counter is what reports it.
FULL_SCALE = 1.0

#: How often telemetry is pushed to the GUI. See the module docstring in the tab
#: and the report: peak is accumulated over *every* frame in the callback, so a
#: coarse emit rate loses no transient -- it only limits how often the meters
#: repaint.
TELEMETRY_INTERVAL_S = 0.05

#: Peak and clip statistics ignore this much audio after a stream opens.
#:
#: Measured, not guessed. Opening an input stream on the headset mic from the bug
#: report produces a burst of genuinely full-scale samples about **68 ms** in --
#: on every host API (WASAPI, MME, DirectSound, WDM-KS), on every open, in a
#: silent room. It is the device's capture pipeline priming itself, and it is a
#: *run* of consecutive full-scale frames, so it latched ``max_peak`` at
#: +0.0 dBFS and counted clip runs while the meters sat at idle. Note it is not
#: in the first block -- block 0 is clean; the burst lands around block 3 -- so
#: discarding one block would not have caught it.
#:
#: 150 ms because the measured burst ends by ~72 ms and a window that only just
#: covers it is not a window. The audio itself is still recorded in full; only
#: the *statistics* skip this opening slice.
STATS_GRACE_S = 0.15

_SUBTYPES = ("PCM_16", "PCM_24")


@dataclass(frozen=True)
class DeviceInfo:
    """One input device, as the picker needs to show it."""

    index: int
    name: str
    hostapi: str
    samplerate: int          # the device's native/shared-mode rate
    max_channels: int

    @property
    def is_wasapi(self) -> bool:
        return "wasapi" in self.hostapi.lower()

    def label(self) -> str:
        return f"{self.name} ({self.hostapi}, {self.max_channels}ch, {self.samplerate} Hz)"


@dataclass
class Telemetry:
    """A throttled snapshot for the meters. Cheap to build, safe to drop."""

    peaks_dbfs: list[float] = field(default_factory=list)   # per channel, this window
    max_peak_dbfs: float = -np.inf                          # running, whole session
    clip_runs: int = 0
    clip_runs_by_channel: list[int] = field(default_factory=list)
    """Latching clip-run count per channel, so the history strip can put a tick in
    the lane that clipped. Empty when the producer doesn't track it; ``clip_runs``
    stays the aggregate either way."""
    elapsed_s: float = 0.0
    bytes_written: int = 0


@dataclass
class RecordingResult:
    path: Path
    duration: float
    samplerate: int
    subtype: str
    max_peak_dbfs: float
    clip_runs: int
    warnings: list[str] = field(default_factory=list)

    @property
    def clipped(self) -> bool:
        return self.clip_runs > 0


TelemetryCallback = Callable[[Telemetry], None]


def _to_dbfs(amplitude: float) -> float:
    """Amplitude -> dBFS, never above 0.

    The clamp is belt-and-braces: :meth:`_LevelStats.feed` already holds the
    stored peaks at :data:`FULL_SCALE`, but every dBFS number the telemetry
    reports goes through this function, so the invariant is enforced where it
    cannot be routed around.
    """
    if amplitude <= 0:
        return -np.inf
    return 20.0 * float(np.log10(min(float(amplitude), FULL_SCALE)))


def list_input_devices() -> list[DeviceInfo]:
    """Every input device, WASAPI first on Windows.

    WASAPI is preferred because it is the modern, low-overhead path and reports
    the rate the device is *actually* configured at in Windows Sound settings --
    MME and DirectSound both report a legacy 44100 regardless. Ordering only sets
    the default; the user can pick any of them.
    """
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    devices: list[DeviceInfo] = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        devices.append(DeviceInfo(
            index=index,
            name=str(dev["name"]),
            hostapi=str(hostapis[dev["hostapi"]]["name"]),
            samplerate=int(dev["default_samplerate"]),
            max_channels=int(dev["max_input_channels"]),
        ))
    devices.sort(key=lambda d: (not d.is_wasapi, d.name.lower()))
    return devices


def list_output_devices() -> list[DeviceInfo]:
    """Every output device, WASAPI first on Windows -- for software monitoring.

    Mirror of :func:`list_input_devices`, filtered to devices with an output
    channel. ``max_channels`` here is the output channel count.
    """
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    devices: list[DeviceInfo] = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] < 1:
            continue
        devices.append(DeviceInfo(
            index=index,
            name=str(dev["name"]),
            hostapi=str(hostapis[dev["hostapi"]]["name"]),
            samplerate=int(dev["default_samplerate"]),
            max_channels=int(dev["max_output_channels"]),
        ))
    devices.sort(key=lambda d: (not d.is_wasapi, d.name.lower()))
    return devices


def supported_input_rates(device: int, channels: int, candidates,
                          *, probe=None) -> list[int]:
    """Which of ``candidates`` this input device will actually open at.

    WASAPI shared mode opens a stream only at the device's *configured* rate, so a
    fixed rate menu lies: picking 44100 on a device Windows has at 48000 fails with
    ``PortAudioError -9997``. ``sounddevice.check_input_settings`` raises for an
    unopenable ``(device, rate, channels)``; we probe each candidate and keep the
    ones that don't raise. ``probe(rate) -> bool`` is injectable for tests. Never
    raises -- an enumeration failure just drops that rate.
    """
    if probe is None:
        import sounddevice as sd

        def probe(rate: int) -> bool:
            try:
                sd.check_input_settings(device=device, samplerate=int(rate),
                                        channels=channels, dtype="float32")
                return True
            except Exception:
                return False

    return [int(r) for r in candidates if probe(r)]


def _scan_runs(hot, run: int, counted: bool, run_len: int) -> tuple[int, bool, int]:
    """Count newly-completed clip runs in a 1-D boolean sequence.

    ``run``/``counted`` carry the in-progress state across block boundaries, so a
    run straddling two callbacks is one run and not two. Returns the new state
    plus how many runs completed in this block.
    """
    added = 0
    for is_hot in hot:
        if is_hot:
            run += 1
            if run >= run_len and not counted:
                added += 1
                counted = True
        else:
            run = 0
            counted = False
    return run, counted, added


class _ClipCounter:
    """Counts runs of consecutive full-scale frames, across block boundaries.

    Two views of the same events. ``runs`` is the aggregate -- a frame counts as
    hot when *any* channel is at full scale -- which is what the latch, the
    warning and :class:`RecordingResult` have always reported. ``channel_runs``
    counts each channel separately, so the history strip can put a clip tick in
    the lane of the channel that actually clipped rather than in both.
    """

    def __init__(self, level: float = CLIP_LEVEL, run_len: int = CLIP_RUN_LEN,
                 channels: int = 1) -> None:
        self._level = level
        self._run_len = run_len
        self.channels = max(1, int(channels))
        self._run = 0            # frames at full scale so far, carried between blocks
        self._counted = False    # whether the current run has already been counted
        self.runs = 0
        self._ch_run = [0] * self.channels
        self._ch_counted = [False] * self.channels
        self.channel_runs = [0] * self.channels

    def feed(self, block: np.ndarray) -> None:
        """``block`` is (frames, channels) float."""
        hot = np.abs(block) >= self._level                  # (frames, channels)
        self._run, self._counted, added = _scan_runs(
            hot.any(axis=1).tolist(), self._run, self._counted, self._run_len)
        self.runs += added

        for ch in range(min(self.channels, hot.shape[1])):
            self._ch_run[ch], self._ch_counted[ch], ch_added = _scan_runs(
                hot[:, ch].tolist(), self._ch_run[ch], self._ch_counted[ch],
                self._run_len)
            self.channel_runs[ch] += ch_added


class _LevelStats:
    """Peak and clip statistics for one open stream, with an open-transient guard.

    Shared by :class:`Recorder` and :class:`LevelMonitor` so the guard cannot be
    fixed in one and forgotten in the other -- the meters and the recording have
    to agree about what the input is doing.

    The guard is the whole point: for the first :data:`STATS_GRACE_S` of a stream
    the frames are passed straight through to disk but contribute *nothing* to
    the peak or the clip count. See :data:`STATS_GRACE_S` for the measurement.
    """

    def __init__(self, channels: int, samplerate: int,
                 grace_s: float = STATS_GRACE_S) -> None:
        self.channels = int(channels)
        self._grace_frames = max(0, int(samplerate * grace_s))
        self._frames_seen = 0
        self.window_peak = np.zeros(self.channels, dtype=np.float64)
        self.max_peak = 0.0
        self.clips = _ClipCounter(channels=self.channels)

    @property
    def in_grace(self) -> bool:
        """True while the opening slice is still being ignored."""
        return self._frames_seen < self._grace_frames

    def feed(self, block: np.ndarray) -> None:
        """``block`` is (frames, channels) float. Grace frames are dropped here."""
        frames = block.shape[0]
        skip = min(frames, max(0, self._grace_frames - self._frames_seen))
        self._frames_seen += frames
        if skip:
            block = block[skip:]                 # may straddle the boundary
            if block.shape[0] == 0:
                return

        # Clamped to the format ceiling: shared-mode float32 can exceed unity
        # (see FULL_SCALE), and a level meter that reads above full scale is
        # describing a file that cannot exist.
        peaks = np.minimum(np.abs(block).max(axis=0), FULL_SCALE)
        if peaks.shape == self.window_peak.shape:
            np.maximum(self.window_peak, peaks, out=self.window_peak)
        block_peak = float(peaks.max()) if peaks.size else 0.0
        if block_peak > self.max_peak:
            self.max_peak = block_peak
        self.clips.feed(block)

    def take_window_peaks(self) -> list[float]:
        """The per-channel peak since the last call, in dBFS. Resets the window,
        so the bars fall back instead of only ever climbing."""
        peaks = [_to_dbfs(float(p)) for p in self.window_peak]
        self.window_peak.fill(0.0)
        return peaks

    def reset(self) -> None:
        """Clear the running max and the clip latch -- what Reset means.

        The grace window is deliberately *not* re-armed: this stream is long past
        its opening transient, and re-arming would blind the meters for another
        150 ms every time the user pressed a button.
        """
        self.max_peak = 0.0
        self.clips = _ClipCounter(channels=self.channels)
        self.window_peak.fill(0.0)


class Recorder:
    """Streams one input device to a WAV on disk.

    ``stream_factory`` exists so tests can drive the callback path with a fake
    stream instead of real hardware; production leaves it alone.
    """

    def __init__(
        self,
        on_telemetry: TelemetryCallback | None = None,
        *,
        stream_factory: Callable | None = None,
        queue_blocks: int = 256,
        telemetry_interval_s: float = TELEMETRY_INTERVAL_S,
        stats_grace_s: float = STATS_GRACE_S,
    ) -> None:
        self._on_telemetry = on_telemetry
        self._stream_factory = stream_factory
        self._telemetry_interval = telemetry_interval_s
        self._stats_grace_s = stats_grace_s

        # Bounded: this is the whole point. ~256 blocks of 1024 frames is a few
        # seconds of slack for a disk hiccup, and a hard ceiling on memory.
        self._queue: queue.Queue = queue.Queue(maxsize=queue_blocks)

        self._stream = None
        self._writer: threading.Thread | None = None
        self._file: sf.SoundFile | None = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

        self._staging_dir: Path | None = None
        self._staging_path: Path | None = None
        self._dest: Path | None = None

        self._samplerate = 0
        self._channels = 0
        self._subtype = "PCM_16"

        self._frames_written = 0
        self._bytes_written = 0
        self._stats = _LevelStats(0, 0, self._stats_grace_s)
        self._started_at = 0.0
        self._last_emit = 0.0

        self._warnings: list[str] = []
        self._dropped_blocks = 0
        self._device_error = ""

    # -- state ---------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def device_error(self) -> str:
        """Non-empty when the device vanished mid-capture."""
        return self._device_error

    # No reset_peaks() here, deliberately: the recorder's max and clip count are
    # what :meth:`stop` reports *about the file*, and a file's peak is not
    # something the user gets to clear halfway through. Reset is a monitor
    # affordance (see :meth:`LevelMonitor.reset_peaks`); mid-capture the meters
    # simply re-read the true running stats of the take.

    def _warn(self, message: str) -> None:
        with self._lock:
            if message not in self._warnings:
                self._warnings.append(message)

    # -- lifecycle -----------------------------------------------------------
    def start(
        self,
        device: int,
        path: str | Path,
        samplerate: int,
        channels: int = 2,
        subtype: str = "PCM_16",
        *,
        blocksize: int = 1024,
    ) -> None:
        """Open the device and begin streaming to local staging."""
        if self.recording:
            raise RuntimeError("already recording")
        if subtype not in _SUBTYPES:
            raise ValueError(f"subtype must be one of {_SUBTYPES}; got {subtype!r}")

        self._dest = Path(path)
        self._samplerate = int(samplerate)
        self._channels = int(channels)
        self._subtype = subtype

        self._frames_written = 0
        self._bytes_written = 0
        self._stats = _LevelStats(self._channels, self._samplerate, self._stats_grace_s)
        self._warnings = []
        self._dropped_blocks = 0
        self._device_error = ""
        self._stop_flag.clear()
        while not self._queue.empty():           # a previous run's tail
            self._queue.get_nowait()

        # Local staging: the destination is usually a network share, and a live
        # capture must never be writing across one.
        self._staging_dir = Path(tempfile.mkdtemp(prefix="rrf_record_"))
        self._staging_path = self._staging_dir / self._dest.name

        self._file = sf.SoundFile(
            str(self._staging_path), mode="w",
            samplerate=self._samplerate, channels=self._channels, subtype=self._subtype)

        self._writer = threading.Thread(target=self._writer_loop, daemon=True,
                                        name="rrf-recorder-writer")
        self._writer.start()

        factory = self._stream_factory
        if factory is None:
            import sounddevice as sd

            factory = sd.InputStream
        self._started_at = time.monotonic()
        self._last_emit = 0.0
        self._stream = factory(
            device=device,
            channels=self._channels,
            samplerate=self._samplerate,
            dtype="float32",
            blocksize=blocksize,
            callback=self.audio_callback,
        )
        self._stream.start()

    def audio_callback(self, indata, frames, time_info, status) -> None:
        """PortAudio's realtime thread. **Must not block, must not raise.**

        Everything expensive -- disk, GUI -- happens elsewhere. This does a copy,
        a peak, a clip count, and a non-blocking enqueue.
        """
        try:
            if status:
                # Overflow means PortAudio had to throw input away: the capture
                # has a hole in it, and the user has to be told.
                if getattr(status, "input_overflow", False):
                    self._warn("input overflow: the capture dropped samples "
                               "(a dropout is present in the audio)")
                else:
                    self._warn(f"audio device reported: {status}")

            block = np.array(indata, dtype=np.float32, copy=True)
            if block.ndim == 1:
                block = block.reshape(-1, 1)

            # Statistics only -- the block itself is queued for disk regardless,
            # so the opening transient is *recorded*, just not *counted*.
            self._stats.feed(block)

            try:
                self._queue.put_nowait(block)
            except queue.Full:
                # Never block the realtime thread. Drop, and admit it.
                self._dropped_blocks += 1
                self._warn("disk could not keep up: audio blocks were dropped "
                           "(a dropout is present in the audio)")

            self._maybe_emit_telemetry()
        except Exception as exc:                 # a raise here would kill the stream
            self._device_error = f"{type(exc).__name__}: {exc}"

    def _maybe_emit_telemetry(self) -> None:
        if self._on_telemetry is None:
            return
        now = time.monotonic()
        if now - self._last_emit < self._telemetry_interval:
            return
        self._last_emit = now

        try:
            self._on_telemetry(Telemetry(
                peaks_dbfs=self._stats.take_window_peaks(),
                max_peak_dbfs=_to_dbfs(self._stats.max_peak),
                clip_runs=self._stats.clips.runs,
                clip_runs_by_channel=list(self._stats.clips.channel_runs),
                elapsed_s=now - self._started_at,
                bytes_written=self._bytes_written,
            ))
        except Exception:
            pass                                 # telemetry must never break capture

    def _writer_loop(self) -> None:
        """Drains the queue to disk. The only place that touches the file."""
        while True:
            try:
                block = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_flag.is_set():
                    return
                continue
            if block is None:
                return
            try:
                if self._file is not None:
                    self._file.write(block)
                    self._frames_written += block.shape[0]
                    self._bytes_written = self._frames_written * self._channels * (
                        2 if self._subtype == "PCM_16" else 3)
            except Exception as exc:
                self._warn(f"write failed: {type(exc).__name__}: {exc}")
                return

    def stop(self) -> RecordingResult:
        """Stop, flush, **close the file**, then move staging to the destination.

        The close-before-move order is not cosmetic: Windows will not rename an
        open file, and a capture that cannot be delivered is a capture lost.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                self._device_error = self._device_error or f"{type(exc).__name__}: {exc}"

        # Let the writer finish what the callback already handed it.
        self._stop_flag.set()
        if self._writer is not None:
            self._writer.join(timeout=5.0)
            self._writer = None
        while not self._queue.empty():            # drain anything left
            block = self._queue.get_nowait()
            try:
                if self._file is not None and block is not None:
                    self._file.write(block)
                    self._frames_written += block.shape[0]
            except Exception:
                break

        # Release the handle BEFORE the move.
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

        if self._device_error:
            self._warn(f"recording device failed: {self._device_error}. "
                       "The audio captured before the failure has been kept.")

        final = self._deliver()

        duration = self._frames_written / self._samplerate if self._samplerate else 0.0
        return RecordingResult(
            path=final,
            duration=duration,
            samplerate=self._samplerate,
            subtype=self._subtype,
            max_peak_dbfs=_to_dbfs(self._stats.max_peak),
            clip_runs=self._stats.clips.runs,
            warnings=list(self._warnings),
        )

    def _deliver(self) -> Path:
        """Move the staged capture to its destination; keep it either way."""
        staged = self._staging_path
        dest = self._dest
        try:
            if staged is None or dest is None or not staged.exists():
                return dest if dest is not None else Path()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(dest))
            return dest
        except Exception as exc:
            # Delivery failed (share gone?). The capture still exists -- say where.
            self._warn(f"could not move the recording to {dest}: "
                       f"{type(exc).__name__}: {exc}. It is still at {staged}.")
            return staged if staged is not None else Path()
        finally:
            if self._staging_dir is not None and self._staging_path is not None \
                    and not self._staging_path.exists():
                shutil.rmtree(self._staging_dir, ignore_errors=True)
                self._staging_dir = None
            self._staging_path = None


class LevelMonitor:
    """Live input levels *without* recording -- for setting gain before you press
    Record.

    Deliberately separate from :class:`Recorder`: it opens a stream, computes the
    same peaks and clip runs, and writes nothing at all. No file, no queue, no
    writer thread. Metering while idle must not be able to produce a stray WAV.
    """

    def __init__(
        self,
        on_telemetry: TelemetryCallback | None = None,
        *,
        stream_factory: Callable | None = None,
        telemetry_interval_s: float = TELEMETRY_INTERVAL_S,
        stats_grace_s: float = STATS_GRACE_S,
    ) -> None:
        self._on_telemetry = on_telemetry
        self._stream_factory = stream_factory
        self._telemetry_interval = telemetry_interval_s
        self._stats_grace_s = stats_grace_s
        self._stream = None
        self._channels = 0
        self._stats = _LevelStats(0, 0, stats_grace_s)
        self._started_at = 0.0
        self._last_emit = 0.0
        self.error = ""

    @property
    def running(self) -> bool:
        return self._stream is not None

    def reset_peaks(self) -> None:
        """Clear the running max and the latched clip count."""
        self._stats.reset()

    def start(self, device: int, samplerate: int, channels: int = 2,
              *, blocksize: int = 1024) -> None:
        if self.running:
            self.stop()
        self._channels = int(channels)
        # A new stream is a new measurement -- and a new opening transient to
        # step over, which is why the stats are rebuilt rather than reused.
        self._stats = _LevelStats(self._channels, int(samplerate), self._stats_grace_s)
        self.error = ""
        self._started_at = time.monotonic()
        self._last_emit = 0.0

        factory = self._stream_factory
        if factory is None:
            import sounddevice as sd

            factory = sd.InputStream
        try:
            self._stream = factory(
                device=device, channels=self._channels, samplerate=int(samplerate),
                dtype="float32", blocksize=blocksize, callback=self.audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self.error = f"{type(exc).__name__}: {exc}"

    def audio_callback(self, indata, frames, time_info, status) -> None:
        try:
            block = np.array(indata, dtype=np.float32, copy=True)
            if block.ndim == 1:
                block = block.reshape(-1, 1)
            self._stats.feed(block)

            if self._on_telemetry is None:
                return
            now = time.monotonic()
            if now - self._last_emit < self._telemetry_interval:
                return
            self._last_emit = now
            self._on_telemetry(Telemetry(
                peaks_dbfs=self._stats.take_window_peaks(),
                max_peak_dbfs=_to_dbfs(self._stats.max_peak),
                clip_runs=self._stats.clips.runs,
                clip_runs_by_channel=list(self._stats.clips.channel_runs),
                elapsed_s=now - self._started_at,
                bytes_written=0,
            ))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"


class Passthrough:
    """Software monitoring: pass an input device through to an output device, so
    you can hear what you are capturing without Windows' "Listen to this device".

    Wholly independent of :class:`Recorder` -- its own input stream, its own output
    stream, its own buffer. It never touches a recording's stats, queue, or file,
    so a monitor glitch (an output underrun, a vanished speaker) is *structurally*
    unable to affect a capture: the worst it can do is fall silent.

    The bridge is a small bounded ring of blocks. The input callback appends a copy
    and returns -- it never blocks; a full ring drops its oldest block, which bounds
    latency. The output callback pops a block, or fills silence on underrun -- it
    never waits. Input and output run at the same samplerate, channel count and
    blocksize, so the ring is a plain block FIFO. Streams are injectable
    (``input_factory``/``output_factory``) so tests need no real audio device.
    """

    def __init__(
        self,
        *,
        input_factory: Callable | None = None,
        output_factory: Callable | None = None,
        blocksize: int = 1024,
        ring_blocks: int = 3,
    ) -> None:
        self._input_factory = input_factory
        self._output_factory = output_factory
        self._blocksize = int(blocksize)
        self._ring: deque = deque(maxlen=max(1, int(ring_blocks)))
        self._channels = 0
        self._in_stream = None
        self._out_stream = None
        self.error = ""
        self.underruns = 0      # output callbacks that found the ring empty
        self.dropped = 0        # input blocks dropped because the ring was full
        self.latency_s = 0.0    # PortAudio-reported round-trip + ring depth, at start()

    @property
    def running(self) -> bool:
        return self._in_stream is not None or self._out_stream is not None

    def start(self, input_device: int, output_device: int, samplerate: int,
              channels: int = 2) -> None:
        """Open the streams and begin passing audio through.

        Refuses (sets :attr:`error`, opens nothing) when input and output are the
        same endpoint -- monitoring a device back into itself is a feedback trap.
        A failure to open either stream is captured in :attr:`error`, never raised,
        and leaves nothing half-open.
        """
        if self.running:
            self.stop()
        self.error = ""
        self.underruns = 0
        self.dropped = 0
        self._ring.clear()
        self._channels = max(1, int(channels))
        if input_device == output_device:
            self.error = "input and output are the same device (feedback risk)"
            return

        in_factory = self._input_factory
        out_factory = self._output_factory
        if in_factory is None or out_factory is None:
            import sounddevice as sd

            in_factory = in_factory or sd.InputStream
            out_factory = out_factory or sd.OutputStream
        try:
            self._in_stream = in_factory(
                device=input_device, channels=self._channels,
                samplerate=int(samplerate), dtype="float32",
                blocksize=self._blocksize, callback=self._input_callback,
            )
            self._out_stream = out_factory(
                device=output_device, channels=self._channels,
                samplerate=int(samplerate), dtype="float32",
                blocksize=self._blocksize, callback=self._output_callback,
            )
            self._out_stream.start()
            self._in_stream.start()
            rate = float(samplerate) or 1.0
            self.latency_s = (
                float(getattr(self._in_stream, "latency", 0.0) or 0.0)
                + float(getattr(self._out_stream, "latency", 0.0) or 0.0)
                + self._ring.maxlen * self._blocksize / rate)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.stop()

    def _input_callback(self, indata, frames, time_info, status) -> None:
        """Realtime input thread: copy the block into the ring and return.

        Never blocks; a full ring drops its oldest block (deque maxlen), bounding
        latency at the cost of a click when the output cannot keep up.
        """
        try:
            if len(self._ring) == self._ring.maxlen:
                self.dropped += 1
            block = np.array(indata, dtype=np.float32, copy=True)
            if block.ndim == 1:
                block = block.reshape(-1, 1)
            self._ring.append(block)
        except Exception as exc:               # a raise here would kill the stream
            self.error = f"{type(exc).__name__}: {exc}"

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        """Realtime output thread: fill from the ring, or silence on underrun."""
        try:
            block = self._ring.popleft()
        except IndexError:
            outdata.fill(0)
            self.underruns += 1
            return
        except Exception as exc:
            outdata.fill(0)
            self.error = f"{type(exc).__name__}: {exc}"
            return
        if block.shape == outdata.shape:
            outdata[:] = block
        else:
            # Defensive: a size mismatch fills what fits and zeros the rest rather
            # than raise on the realtime thread.
            outdata.fill(0)
            rows = min(block.shape[0], outdata.shape[0])
            cols = min(block.shape[1], outdata.shape[1])
            outdata[:rows, :cols] = block[:rows, :cols]

    def stop(self) -> None:
        for attr in ("_in_stream", "_out_stream"):
            stream = getattr(self, attr)
            setattr(self, attr, None)
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
        self._ring.clear()
