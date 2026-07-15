"""Software monitoring (Passthrough): frame ordering, feedback refusal, and --
above all -- structural independence from the capture path.

No hardware: fake input/output streams drive the real callbacks by hand.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from core.recorder import STATS_GRACE_S, Passthrough, Recorder

SR = 44100
GRACE_FRAMES = int(SR * STATS_GRACE_S)


class _NoStatus:
    input_overflow = False

    def __bool__(self):
        return False

    def __str__(self):
        return ""


class FakeStream:
    """Stands in for sd.InputStream / sd.OutputStream; the test drives the callback."""

    latency = 0.01

    def __init__(self, *, device, channels, samplerate, dtype, blocksize, callback):
        self.device = device
        self.channels = channels
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def drive(self, buf):
        """Feed `buf` through the real callback (input: a block to read; output: a
        buffer to fill in place)."""
        self.callback(buf, buf.shape[0], None, _NoStatus())


def _passthrough(blocksize=4, ring_blocks=8):
    made = {}

    def in_factory(**kw):
        made["in"] = FakeStream(**kw)
        return made["in"]

    def out_factory(**kw):
        made["out"] = FakeStream(**kw)
        return made["out"]

    p = Passthrough(input_factory=in_factory, output_factory=out_factory,
                    blocksize=blocksize, ring_blocks=ring_blocks)
    return p, made


# --------------------------------------------------------------------------- #
# Passthrough itself
# --------------------------------------------------------------------------- #
def test_passthrough_moves_frames_input_to_output_in_order():
    p, made = _passthrough(blocksize=4)
    p.start(input_device=1, output_device=2, samplerate=SR, channels=2)
    assert p.error == "" and p.running

    b1 = np.full((4, 2), 0.1, dtype=np.float32)
    b2 = np.full((4, 2), 0.2, dtype=np.float32)
    made["in"].drive(b1)
    made["in"].drive(b2)

    out = np.zeros((4, 2), dtype=np.float32)
    made["out"].drive(out)
    assert np.array_equal(out, b1)                 # first in, first out
    made["out"].drive(out)
    assert np.array_equal(out, b2)

    # Nothing left: underrun fills silence, never blocks.
    made["out"].drive(out)
    assert np.array_equal(out, np.zeros((4, 2), dtype=np.float32))
    assert p.underruns == 1
    p.stop()
    assert not p.running


def test_passthrough_ring_drops_oldest_when_full_bounding_latency():
    p, made = _passthrough(blocksize=4, ring_blocks=2)
    p.start(1, 2, SR, 2)
    for i in range(5):                             # 5 blocks into a 2-deep ring
        made["in"].drive(np.full((4, 2), float(i), dtype=np.float32))
    assert p.dropped == 3                          # 0,1,2 dropped; 3,4 survive

    out = np.zeros((4, 2), dtype=np.float32)
    made["out"].drive(out)
    assert out[0, 0] == 3.0                        # oldest survivor, in order
    p.stop()


def test_passthrough_refuses_the_same_endpoint():
    p, made = _passthrough()
    p.start(input_device=5, output_device=5, samplerate=SR, channels=2)
    assert "same device" in p.error
    assert not p.running                           # nothing opened -> no feedback loop
    assert "in" not in made and "out" not in made


def test_passthrough_reports_a_latency_estimate(monkeypatch):
    p, made = _passthrough(blocksize=1024, ring_blocks=8)
    p.start(1, 2, SR, 2)
    # 0.01 s in + 0.01 s out + 8 * 1024 / 44100 (~0.186 s) ring.
    assert p.latency_s > 0.0
    assert abs(p.latency_s - (0.02 + 8 * 1024 / SR)) < 1e-6
    p.stop()


# --------------------------------------------------------------------------- #
# Structural independence: a monitor can NEVER touch a capture
# --------------------------------------------------------------------------- #
def _silence(frames, channels=2):
    return np.zeros((frames, channels), dtype=np.float32)


def _blocks():
    """A deterministic take: silence grace, a tone, a clipping burst, a tone."""
    t = np.arange(1024) / SR
    tone = (0.3 * np.sin(2 * np.pi * 440 * t))[:, None].repeat(2, axis=1).astype(np.float32)
    hot = np.ones((1024, 2), dtype=np.float32)     # full scale -> clip runs
    return [tone, hot, tone]


def _record(dest, *, monitor=False, kill_monitor=False):
    """Record a fixed block sequence, optionally toggling a Passthrough alongside."""
    made = {}

    def factory(**kw):
        made["s"] = FakeStream(**kw)
        return made["s"]

    rec = Recorder(stream_factory=factory)
    rec.start(device=0, path=dest, samplerate=SR, channels=2, subtype="PCM_16")
    stream = made["s"]
    stream.push = stream.drive  # Recorder's FakeStream drives the same way
    stream.drive(_silence(GRACE_FRAMES + 1))       # step past the stats grace window

    pt, pt_made = _passthrough(blocksize=1024)
    for i, block in enumerate(_blocks()):
        stream.drive(block)                        # the capture
        if monitor:
            # Toggle the monitor on and off around the capture, feed it the same
            # audio, and -- if asked -- kill it mid-capture. None of this may reach
            # the recording.
            if i == 0:
                pt.start(1, 2, SR, 2)
            if pt.running:
                pt_made["in"].drive(block)
            if kill_monitor and i == 1:
                pt.error = "OutputError: device vanished"
                pt.stop()                          # the monitor dies here
            elif i == len(_blocks()) - 1:
                pt.stop()
    return rec.stop()


def test_monitor_death_leaves_capture_stats_untouched(tmp_path):
    control = _record(tmp_path / "control.wav")
    monitored = _record(tmp_path / "monitored.wav", monitor=True, kill_monitor=True)

    assert monitored.clip_runs == control.clip_runs
    assert monitored.max_peak_dbfs == control.max_peak_dbfs
    assert monitored.warnings == control.warnings
    assert control.clip_runs >= 1                  # the take really did clip


def test_toggling_monitor_mid_capture_leaves_the_recording_identical(tmp_path):
    control = _record(tmp_path / "control.wav")
    monitored = _record(tmp_path / "monitored.wav", monitor=True)

    # Byte-identical output WAVs: the monitor shares nothing with the capture.
    assert (tmp_path / "monitored.wav").read_bytes() == (tmp_path / "control.wav").read_bytes()
    assert monitored.duration == control.duration
    # sanity: a real capture -- the grace silence plus the three blocks (grace is
    # excluded from stats, but still recorded to the file).
    assert sf.info(str(tmp_path / "control.wav")).frames == (GRACE_FRAMES + 1) + 3 * 1024
