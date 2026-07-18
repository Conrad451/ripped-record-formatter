"""Record-tab setup checks -- plain language, no project vocabulary.

GUI-free logic. Each check inspects only what the device info and the meters'
telemetry already provide, and returns a :class:`CheckResult` whose message is
written for someone who has never heard this project's words. Checks *advise*;
nothing here gates recording -- a warning is a nudge, never a wall.

Two kinds:

* :func:`check_sample_rate` -- instant, from device info.
* :func:`check_signal` -- from a short listen window of telemetry taken *after*
  the meters are reset, so the device-open transient (already excluded from the
  recorder's stats by its grace guard) does not count as clipping here.
"""

from __future__ import annotations

from dataclasses import dataclass

OK = "ok"
WARN = "warn"
INFO = "info"

#: Vinyl wants 44.1 kHz. A stereo (line-in-looking) device set to anything else
#: gets a one-click nudge.
TARGET_RATE = 44100

#: A listen window whose loudest point stays below this (dBFS) means "no signal".
SILENCE_DBFS = -70.0

#: This many clip runs in the window (past the open transient) is "much too hot"
#: -- the double-preamp signature. Two, not one, so a single stray peak is not a
#: verdict.
HOT_CLIP_RUNS = 2


@dataclass(frozen=True)
class CheckResult:
    """One line of the setup-check list."""

    status: str                       # OK | WARN | INFO
    message: str
    fix_label: str | None = None      # button text, when a one-click fix exists
    fix_key: str | None = None        # what that fix does


def check_sample_rate(*, device_rate: int, capture_rate: int,
                      max_channels: int) -> CheckResult | None:
    """A stereo device configured to something other than 44.1 kHz gets a nudge.

    ``capture_rate`` is our pinned rate (0 = follow the device's own rate).
    Returns ``None`` when the effective rate is already 44100, or the device does
    not look like a stereo line input (mono devices are microphones, not turntables).
    """
    effective = capture_rate or device_rate
    if max_channels >= 2 and effective != TARGET_RATE:
        return CheckResult(
            WARN,
            f"This device is set to {device_rate} in Windows. For vinyl you almost "
            f"certainly want 44,100 Hz.",
            fix_label="Use 44100", fix_key="set_rate_44100")
    return None


def check_signal(*, clip_runs: int, peak_dbfs: float) -> CheckResult:
    """From a short listen window: too hot, no signal, or all-clear.

    ``clip_runs`` and ``peak_dbfs`` are accumulated over the window after the
    meters were reset, so the opening transient does not reach here.
    """
    if clip_runs >= HOT_CLIP_RUNS:
        return CheckResult(
            WARN,
            "The signal is much too hot. If your turntable has a PHONO/LINE switch, "
            "make sure it and your USB box aren't both set to amplify — only one "
            "should.")
    if peak_dbfs < SILENCE_DBFS:
        return CheckResult(
            WARN,
            "No signal detected. Is the record playing, and are the red/white "
            "cables in the USB box's inputs?")
    return CheckResult(
        OK,
        "Setup looks good. Play the loudest song on the record and adjust the "
        "volume until the moving line stays below the dashed one, then you're "
        "ready to record.")
