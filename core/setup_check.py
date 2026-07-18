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


def _output_target(output_rate) -> int | None:
    """The configured output sample rate as Hz, or None for "keep source"."""
    if output_rate in (None, "", "source"):
        return None
    try:
        return int(output_rate)
    except (TypeError, ValueError):
        return None


def check_sample_rate(*, device_rate: int, output_rate) -> CheckResult | None:
    """Reassure, don't nag: a device on a non-44.1k rate is *fine* -- the FLACs are
    resampled to the library rate at encode.

    ``output_rate`` is the ``output_sample_rate`` setting ("source"/"44100"/
    "48000"). Returns ``None`` when nothing will be resampled (the setting is
    "keep source", or the device already runs at the output rate) -- there is
    then nothing to reassure about. There is no one-click fix: the old "Use 44100"
    fix set the *stream* rate, which WASAPI shared mode rejects -- the whole point
    of moving 44.1k to encode time.
    """
    target = _output_target(output_rate)
    if target is not None and device_rate != target:
        return CheckResult(
            OK,
            f"This device is set to {device_rate} in Windows — that's fine. Your "
            f"FLACs will be saved at {target:,} Hz automatically.")
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
