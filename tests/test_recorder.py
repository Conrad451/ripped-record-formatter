"""Recorder: disk streaming, clip detection, bounded queue, lock discipline.

No hardware. A fake stream lets the tests push frames through the *real* callback
path, so what is exercised is the actual realtime->queue->writer->disk chain.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.recorder import (
    CLIP_LEVEL,
    STATS_GRACE_S,
    LevelMonitor,
    Recorder,
    RecordingResult,
)

SR = 44100
GRACE_FRAMES = int(SR * STATS_GRACE_S)


class FakeStream:
    """Stands in for sd.InputStream. The test drives the callback by hand."""

    def __init__(self, *, device, channels, samplerate, dtype, blocksize, callback):
        self.channels = channels
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.started = False
        self.closed = False
        self.stop_raises: Exception | None = None

    def start(self):
        self.started = True

    def stop(self):
        if self.stop_raises is not None:
            raise self.stop_raises
        self.started = False

    def close(self):
        self.closed = True

    # -- test helpers --------------------------------------------------------
    def push(self, block: np.ndarray, status=None):
        """Feed one block through the real audio callback."""
        self.callback(block, block.shape[0], None, status or _NoStatus())


class _NoStatus:
    input_overflow = False

    def __bool__(self):
        return False

    def __str__(self):
        return ""


class _Overflow:
    input_overflow = True

    def __bool__(self):
        return True

    def __str__(self):
        return "input overflow"


def _tone(frames, channels=2, amp=0.25):
    t = np.arange(frames) / SR
    mono = amp * np.sin(2 * np.pi * 440 * t)
    return np.repeat(mono[:, None], channels, axis=1).astype(np.float32)


def _silence(frames, channels=2):
    return np.zeros((frames, channels), dtype=np.float32)


def _prime(stream, channels=2):
    """Push the opening grace window through as silence.

    Every stats assertion below is about audio the user actually played, so the
    tests have to get past the open-transient guard the same way production does
    -- by feeding it frames. Silence, so it contributes nothing of its own.
    """
    stream.push(_silence(GRACE_FRAMES + 1, channels))


def _recorder(tmp_path, **kw):
    """A recorder wired to a FakeStream, plus a way to reach the stream."""
    made = {}

    def factory(**kwargs):
        made["stream"] = FakeStream(**kwargs)
        return made["stream"]

    rec = Recorder(stream_factory=factory, **kw)
    return rec, made


# --------------------------------------------------------------------------- #
# Streaming to disk
# --------------------------------------------------------------------------- #
def test_streams_to_a_valid_wav_of_the_expected_length_and_subtype(tmp_path):
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "out" / "SideA.wav"

    rec.start(device=0, path=dest, samplerate=SR, channels=2, subtype="PCM_16")
    stream = made["stream"]

    for _ in range(10):                      # 10 blocks of 1024 frames
        stream.push(_tone(1024))
    result = rec.stop()

    assert isinstance(result, RecordingResult)
    assert dest.exists()                     # delivered to the destination
    info = sf.info(str(dest))
    assert info.samplerate == SR
    assert info.channels == 2
    assert info.subtype == "PCM_16"
    assert info.frames == 10 * 1024
    assert result.duration == pytest.approx(10 * 1024 / SR, abs=1e-6)
    assert result.warnings == []             # a clean capture says nothing


def test_24_bit_subtype_is_honoured(tmp_path):
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "SideA.wav"
    rec.start(device=0, path=dest, samplerate=SR, subtype="PCM_24")
    made["stream"].push(_tone(1024))
    rec.stop()
    assert sf.info(str(dest)).subtype == "PCM_24"


def test_rejects_an_unsupported_subtype(tmp_path):
    rec, _ = _recorder(tmp_path)
    with pytest.raises(ValueError, match="subtype"):
        rec.start(device=0, path=tmp_path / "x.wav", samplerate=SR, subtype="FLOAT")


# --------------------------------------------------------------------------- #
# Clip detection -- same semantics as source_clip_runs
# --------------------------------------------------------------------------- #
def test_counts_injected_full_scale_runs(tmp_path):
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "SideA.wav"
    rec.start(device=0, path=dest, samplerate=SR, channels=2)
    stream = made["stream"]
    _prime(stream)

    block = _tone(1024)
    block[100:110] = 1.0                     # run 1: 10 frames at full scale
    block[500:504] = -1.0                    # run 2: 4 frames
    block[900] = 1.0                         # a LONE full-scale sample: not a clip
    stream.push(block)

    result = rec.stop()
    assert result.clip_runs == 2             # two runs, not three, not 15
    assert result.clipped is True
    assert result.max_peak_dbfs == pytest.approx(0.0, abs=0.01)


def test_clip_runs_are_also_counted_per_channel(tmp_path):
    """The aggregate says *that* it clipped; the per-channel counts say *which*
    channel did, which is what puts a tick in the right lane of the strip."""
    from core.recorder import Telemetry

    seen: list[Telemetry] = []
    rec, made = _recorder(tmp_path, on_telemetry=seen.append)
    rec.start(device=0, path=tmp_path / "SideA.wav", samplerate=SR, channels=2)
    stream = made["stream"]
    _prime(stream)

    block = _tone(1024)
    block[100:110, 1] = 1.0                  # R clips...
    block[500:510, 1] = -1.0                 # ...twice
    block[800:810, 0] = 1.0                  # L clips once
    stream.push(block)
    rec.stop()

    counter = rec._stats.clips
    assert counter.channel_runs == [1, 2]    # L once, R twice
    assert counter.runs == 3                 # aggregate: three non-overlapping runs


def test_a_clip_run_spanning_two_blocks_counts_once(tmp_path):
    """The run must be carried across the block boundary, not counted twice."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]
    _prime(stream)

    a = _tone(1024)
    a[-5:] = 1.0                             # run starts at the end of block 1...
    b = _tone(1024)
    b[:5] = 1.0                              # ...and continues into block 2
    stream.push(a)
    stream.push(b)

    assert rec.stop().clip_runs == 1


def test_a_clean_capture_reports_no_clipping(tmp_path):
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    _prime(made["stream"])
    made["stream"].push(_tone(1024, amp=0.5))
    result = rec.stop()
    assert result.clip_runs == 0
    assert result.clipped is False
    assert result.max_peak_dbfs == pytest.approx(-6.0, abs=0.2)   # 0.5 -> -6 dBFS


# --------------------------------------------------------------------------- #
# The open transient: a device's first ~100 ms is not a level reading
#
# Measured on the headset mic from the bug report: every stream open emits a run
# of genuinely full-scale samples about 68 ms in -- not in the first block, which
# is clean -- which latched max peak at +0.0 dBFS and counted clip runs while the
# input was idle. These tests inject that artifact and demand it be ignored.
# --------------------------------------------------------------------------- #
def _open_transient(channels=2, at_frame=3000, length=64):
    """The artifact as measured: a full-scale burst ~68 ms into the stream."""
    block = _silence(GRACE_FRAMES, channels)
    block[at_frame:at_frame + length] = -1.0
    return block


def test_the_open_transient_does_not_latch_the_max_peak(tmp_path):
    """The bug, exactly: idle input, full-scale burst at open, max reads +0.0."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]

    stream.push(_open_transient())            # the device priming itself
    stream.push(_tone(1024, amp=0.01))        # ...and then a quiet room

    result = rec.stop()
    assert result.max_peak_dbfs == pytest.approx(-40.0, abs=0.5)   # the room
    assert result.max_peak_dbfs < -3.0                             # NOT +0.0 dBFS


def test_the_open_transient_does_not_count_as_clipping(tmp_path):
    """Same artifact, the other statistic: the screenshot's phantom '3 run(s)'."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]

    stream.push(_open_transient())
    stream.push(_tone(1024, amp=0.01))

    result = rec.stop()
    assert result.clip_runs == 0
    assert result.clipped is False


def test_the_transient_is_still_written_to_the_file(tmp_path):
    """Discarded from the *statistics*, never from the *audio*."""
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "a.wav"
    rec.start(device=0, path=dest, samplerate=SR, channels=2)
    made["stream"].push(_open_transient())

    result = rec.stop()
    assert sf.info(str(dest)).frames == GRACE_FRAMES      # every frame kept
    data, _ = sf.read(str(dest))
    assert np.abs(data).max() >= CLIP_LEVEL               # including the burst
    assert result.max_peak_dbfs < -3.0                    # but not counted


def test_real_clipping_after_the_grace_window_is_still_caught(tmp_path):
    """The guard must not become a blindfold: clipping the user *caused* counts."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]

    stream.push(_open_transient())            # ignored...
    hot = _tone(1024)
    hot[200:260] = 1.0                        # ...but this is the record, too loud
    stream.push(hot)

    result = rec.stop()
    assert result.clip_runs == 1
    assert result.max_peak_dbfs == pytest.approx(0.0, abs=0.01)


def test_the_grace_window_can_straddle_a_block_boundary(tmp_path):
    """The window is a frame count, not a block count: blocks may land anywhere."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]

    # One block that begins inside the grace window and ends outside it, with a
    # full-scale burst on each side of the boundary. The window is a count of
    # frames, so the split has to happen *within* the block.
    stream.push(_silence(GRACE_FRAMES - 1024))    # 1024 frames of grace remain
    straddle = _silence(2048)
    straddle[:10] = 1.0                           # inside grace  -> ignored
    straddle[1030:1090] = 0.5                     # outside grace -> counted
    stream.push(straddle)

    result = rec.stop()
    assert result.clip_runs == 0                  # the pre-boundary burst: ignored
    # ...and the post-boundary audio is genuinely being watched.
    assert result.max_peak_dbfs == pytest.approx(-6.0, abs=0.2)   # 0.5, not 1.0


def test_the_monitor_ignores_the_open_transient_too(tmp_path):
    """The meters and the recorder must agree about what the input is doing."""
    seen = []
    made = {}

    def factory(**kwargs):
        made["stream"] = FakeStream(**kwargs)
        return made["stream"]

    mon = LevelMonitor(on_telemetry=seen.append, stream_factory=factory,
                       telemetry_interval_s=0.0)
    mon.start(device=0, samplerate=SR, channels=2)
    made["stream"].push(_open_transient())
    made["stream"].push(_tone(1024, amp=0.01))
    mon.stop()

    assert seen, "no telemetry emitted"
    assert seen[-1].clip_runs == 0
    assert seen[-1].max_peak_dbfs == pytest.approx(-40.0, abs=0.5)


def test_restarting_the_monitor_re_arms_the_grace_window(tmp_path):
    """Every open has its own transient -- switching device must step over it."""
    seen = []
    made = {}

    def factory(**kwargs):
        made["stream"] = FakeStream(**kwargs)
        return made["stream"]

    mon = LevelMonitor(on_telemetry=seen.append, stream_factory=factory,
                       telemetry_interval_s=0.0)
    mon.start(device=0, samplerate=SR, channels=2)
    _prime(made["stream"])
    made["stream"].push(_tone(1024, amp=0.5))       # a real -6 dBFS on device 0
    assert seen[-1].max_peak_dbfs == pytest.approx(-6.0, abs=0.2)
    mon.stop()

    mon.start(device=1, samplerate=SR, channels=2)  # now a different device
    made["stream"].push(_open_transient())          # with its own opening burst
    made["stream"].push(_tone(1024, amp=0.01))
    mon.stop()

    # Device 1's transient is ignored, and device 0's max did not follow us here.
    assert seen[-1].clip_runs == 0
    assert seen[-1].max_peak_dbfs == pytest.approx(-40.0, abs=0.5)


def test_monitor_reset_clears_the_max_without_re_arming_grace(tmp_path):
    """Reset must actually reset -- and must not blind the meters for 150 ms."""
    seen = []
    made = {}

    def factory(**kwargs):
        made["stream"] = FakeStream(**kwargs)
        return made["stream"]

    mon = LevelMonitor(on_telemetry=seen.append, stream_factory=factory,
                       telemetry_interval_s=0.0)
    mon.start(device=0, samplerate=SR, channels=2)
    stream = made["stream"]
    _prime(stream)
    stream.push(_tone(1024, amp=0.5))               # a loud passage: -6 dBFS
    assert seen[-1].max_peak_dbfs == pytest.approx(-6.0, abs=0.2)

    mon.reset_peaks()
    stream.push(_tone(1024, amp=0.01))              # now quiet
    mon.stop()

    # The old max is gone, and the new quiet reading is visible IMMEDIATELY --
    # a Reset that re-armed the grace window would report -inf here instead.
    assert seen[-1].max_peak_dbfs == pytest.approx(-40.0, abs=0.5)


# --------------------------------------------------------------------------- #
# The queue is bounded, and the callback never blocks
# --------------------------------------------------------------------------- #
def test_queue_stays_bounded_under_a_slow_writer(tmp_path):
    """A stalled disk must not grow memory without limit, and must not block
    the realtime callback -- it drops, and warns."""
    rec, made = _recorder(tmp_path, queue_blocks=8)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    stream = made["stream"]

    # Stall the writer by taking the file away from it.
    rec._stop_flag.set()
    rec._writer.join(timeout=2.0)

    block = _tone(256)
    started = time.monotonic()
    for _ in range(200):                     # far more than the queue can hold
        stream.push(block)
    elapsed = time.monotonic() - started

    assert rec._queue.qsize() <= 8           # hard ceiling honoured
    assert elapsed < 2.0                     # ...and the callback never blocked
    assert rec._dropped_blocks > 0

    result = rec.stop()
    assert any("dropped" in w for w in result.warnings)   # and it admits it


def test_portaudio_overflow_becomes_a_warning(tmp_path):
    """A capture with dropouts must say so -- never ship a damaged WAV silently."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    made["stream"].push(_tone(1024), status=_Overflow())

    result = rec.stop()
    assert any("overflow" in w for w in result.warnings)
    assert any("dropout" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Windows lock discipline: handle released BEFORE the move
# --------------------------------------------------------------------------- #
def test_stop_releases_the_handle_and_delivers_to_the_destination(tmp_path):
    dest_dir = tmp_path / "share"            # stands in for the network share
    dest = dest_dir / "SideA.wav"
    rec, made = _recorder(tmp_path)

    rec.start(device=0, path=dest, samplerate=SR, channels=2)
    staging = rec._staging_path
    assert staging is not None and staging.exists()
    assert staging.parent != dest_dir        # capture is LOCAL, not on the share

    made["stream"].push(_tone(2048))
    result = rec.stop()

    # The move succeeded -- which on Windows is only possible with the handle shut.
    assert dest.exists()
    assert result.path == dest
    assert not staging.exists()
    assert not staging.parent.exists()       # staging dir cleaned up

    # And the delivered file is fully flushed and readable.
    assert sf.info(str(dest)).frames == 2048
    # Nothing holds it: it can be deleted immediately.
    dest.unlink()
    assert not dest.exists()


def test_staging_is_local_even_when_the_destination_is_not(tmp_path):
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "nested" / "deep" / "SideB.wav"
    rec.start(device=0, path=dest, samplerate=SR, channels=2)
    staged = rec._staging_path
    assert "rrf_record_" in str(staged.parent)      # a local temp dir
    made["stream"].push(_tone(512))
    rec.stop()
    assert dest.exists()                            # destination dirs created
    shutil.rmtree(dest.parent)


# --------------------------------------------------------------------------- #
# Device vanishes mid-capture (USB unplug)
# --------------------------------------------------------------------------- #
def test_device_vanishing_keeps_the_partial_file_and_surfaces_the_error(tmp_path):
    """Never a crash, never a zero-byte mystery file."""
    rec, made = _recorder(tmp_path)
    dest = tmp_path / "SideA.wav"
    rec.start(device=0, path=dest, samplerate=SR, channels=2)
    stream = made["stream"]

    stream.push(_tone(4096))                 # 4096 frames captured fine...
    # ...then the USB device is yanked: PortAudio blows up on stop().
    stream.stop_raises = OSError("PortAudioError: Device unavailable [-9985]")

    result = rec.stop()                      # must not raise

    assert dest.exists()
    assert dest.stat().st_size > 0           # NOT a zero-byte mystery file
    assert sf.info(str(dest)).frames == 4096  # everything before the failure kept
    assert result.duration > 0
    assert any("Device unavailable" in w for w in result.warnings)
    assert any("kept" in w for w in result.warnings)


def test_an_exception_inside_the_callback_does_not_escape(tmp_path):
    """A raise on PortAudio's realtime thread would kill the stream."""
    rec, made = _recorder(tmp_path)
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)

    # A malformed block (wrong dtype/shape) must be swallowed, not propagated.
    made["stream"].callback(object(), 0, None, _NoStatus())   # nonsense input

    assert rec.device_error                  # recorded...
    result = rec.stop()                      # ...and surfaced, without a crash
    assert any("device failed" in w or "kept" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
def test_telemetry_reports_peaks_clips_and_elapsed(tmp_path):
    seen = []
    rec, made = _recorder(tmp_path, telemetry_interval_s=0.0)   # emit every block
    rec._on_telemetry = seen.append

    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    _prime(made["stream"])
    block = _tone(1024, amp=0.5)
    block[10:20] = 1.0
    made["stream"].push(block)
    rec.stop()

    assert seen, "no telemetry emitted"
    t = seen[-1]
    assert len(t.peaks_dbfs) == 2                       # per channel
    assert t.max_peak_dbfs == pytest.approx(0.0, abs=0.01)
    assert t.clip_runs == 1
    assert t.elapsed_s >= 0.0


def test_telemetry_is_throttled(tmp_path):
    seen = []
    rec, made = _recorder(tmp_path, telemetry_interval_s=10.0)  # effectively never
    rec._on_telemetry = seen.append

    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    for _ in range(50):
        made["stream"].push(_tone(256))
    rec.stop()

    # 50 callbacks, but the GUI is not asked to repaint 50 times.
    assert len(seen) <= 1


def test_telemetry_failure_never_breaks_the_capture(tmp_path):
    def boom(_t):
        raise RuntimeError("GUI exploded")

    rec, made = _recorder(tmp_path, telemetry_interval_s=0.0)
    rec._on_telemetry = boom
    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    made["stream"].push(_tone(1024))

    result = rec.stop()
    assert sf.info(str(result.path)).frames == 1024     # audio survived regardless


# --------------------------------------------------------------------------- #
# The invariant: an integer-format capture can never report above full scale.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("amp,expected_dbfs", [
    (0.9, -0.92),        # the reference reading
    (0.5, -6.02),
    (1.0, 0.0),          # full scale is exactly zero, not "about" zero
])
def test_known_amplitudes_read_their_true_dbfs(tmp_path, amp, expected_dbfs):
    """Injected known amplitudes, through the real callback path."""
    seen = []
    rec, made = _recorder(tmp_path, telemetry_interval_s=0.0)
    rec._on_telemetry = seen.append

    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    _prime(made["stream"])
    made["stream"].push(np.full((1024, 2), amp, dtype=np.float32))
    rec.stop()

    assert seen[-1].max_peak_dbfs == pytest.approx(expected_dbfs, abs=0.01)


def test_no_telemetry_path_reports_above_full_scale(tmp_path):
    """The v2.4.0 invariant, from a real field report: a stakeholder screenshot
    showed "max 1.8 dBFS (0.0 dB headroom)" on a 16-bit capture -- a level the
    sample format cannot contain.

    Nothing was miscomputing it. WASAPI shared mode hands us float32 that is not
    bounded to +/-1.0 (Windows applies the endpoint volume and any APO gain in
    the float domain), and the peak was reported straight out of that domain
    instead of the integer one the file is written in. 1.2303 really is +1.8
    dBFS -- it just cannot survive the trip to PCM_16, where it saturates to
    0.999969. So the meter now measures the ceiling the recording actually has.
    """
    seen = []
    rec, made = _recorder(tmp_path, telemetry_interval_s=0.0)
    rec._on_telemetry = seen.append

    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    _prime(made["stream"])
    # 1.2303 is exactly the +1.8 dBFS from the screenshot.
    made["stream"].push(np.full((1024, 2), 1.2303, dtype=np.float32))
    rec.stop()

    for t in seen:
        assert t.max_peak_dbfs <= 0.0, f"telemetry reported {t.max_peak_dbfs} dBFS"
        for db in t.peaks_dbfs:
            assert db <= 0.0, f"per-channel telemetry reported {db} dBFS"
    # It reads as pinned to the ceiling, and it is still reported as clipping --
    # clamping the *level* must not hide the overshoot.
    assert seen[-1].max_peak_dbfs == pytest.approx(0.0, abs=1e-9)
    assert seen[-1].clip_runs >= 1


def test_the_overshoot_the_meter_clamps_is_what_the_file_saturates_to(tmp_path):
    """The clamp is not a cosmetic fix -- it is what the WAV holds.

    Written as PCM_16, a float sample of 1.2303 comes back as 0.999969. The
    meter reading and the file's own peak now agree, which is the whole claim.
    """
    path = tmp_path / "over.wav"
    with sf.SoundFile(path, "w", samplerate=SR, channels=2, subtype="PCM_16") as f:
        f.write(np.full((256, 2), 1.2303, dtype=np.float32))

    back, _ = sf.read(path, dtype="float32")
    assert float(np.abs(back).max()) <= 1.0
    assert float(np.abs(back).max()) == pytest.approx(1.0, abs=1e-4)


def test_clamping_the_level_does_not_reduce_the_clip_count(tmp_path):
    """The clip counter was field-verified, not inferred.

    A stakeholder wired into the digitizer's direct output and heard heavy
    clipping on the runs the counter had flagged: the 8 counted runs were real
    ceiling contact, and the counter needed no fix. That makes it a thing to
    *protect* while the level reading changes around it -- clamping the
    reported peak to full scale must not quietly make clipping harder to count,
    which would turn a verified-correct signal into a silent regression.
    """
    seen = []
    rec, made = _recorder(tmp_path, telemetry_interval_s=0.0)
    rec._on_telemetry = seen.append

    rec.start(device=0, path=tmp_path / "a.wav", samplerate=SR, channels=2)
    _prime(made["stream"])
    # Well past the ceiling: exactly the case the clamp now flattens to 0.0 dBFS.
    made["stream"].push(np.full((512, 2), 1.9, dtype=np.float32))
    result = rec.stop()

    assert result.clip_runs >= 1, "an overshoot stopped counting as clipping"
    assert result.max_peak_dbfs == pytest.approx(0.0, abs=1e-9)
    assert result.clipped
