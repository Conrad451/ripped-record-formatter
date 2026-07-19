"""One way to launch a subprocess, so none of them flash a console window.

Every external tool this app runs -- ffmpeg, ffprobe, the declick stage, the MP3
encoder -- is a console program. Launched from a windowed application on
Windows, each one gets its own console window: a black rectangle that appears,
sits in front of whatever the user was doing, and vanishes. An album is dozens
of these.

**Why no test ever caught it.** A console process spawned *from a console* --
which is every test run, every ``python app.py`` from a terminal, every CI
job -- inherits that console and shows nothing. The flashing only happens in the
windowed build, which is the only place users actually live. There was no test
that could have failed, because the defect is invisible from where tests run.

So the guard is structural rather than observational: one helper, one place that
knows about ``CREATE_NO_WINDOW``, and a test asserting the flag is applied there
rather than trying to detect windows that never appear under test.
"""

from __future__ import annotations

import subprocess
import sys

#: Windows: run without allocating a console. Absent on other platforms, where
#: there is no console to allocate and the flag does not exist.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window_kwargs() -> dict:
    """``creationflags`` for a silent launch, or ``{}`` off Windows."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


def run(command, **kwargs):
    """:func:`subprocess.run`, without a console window.

    A drop-in for the call sites that were spawning windows. Any explicit
    ``creationflags`` a caller passes is honoured and OR-ed with the no-window
    flag, so this can only ever make a launch quieter, never louder.
    """
    flags = kwargs.pop("creationflags", 0)
    silent = no_window_kwargs()
    if silent:
        kwargs["creationflags"] = flags | silent["creationflags"]
    elif flags:
        kwargs["creationflags"] = flags
    return subprocess.run(command, **kwargs)


def popen(command, **kwargs):
    """:class:`subprocess.Popen`, without a console window."""
    flags = kwargs.pop("creationflags", 0)
    silent = no_window_kwargs()
    if silent:
        kwargs["creationflags"] = flags | silent["creationflags"]
    elif flags:
        kwargs["creationflags"] = flags
    return subprocess.Popen(command, **kwargs)


def silence_pydub() -> None:
    """Make pydub's own ffmpeg spawns silent too.

    pydub calls ``subprocess.Popen`` inside its own module, so it cannot be
    routed through :func:`popen` from outside. Its ``utils`` module is patched
    once, at startup, to default ``creationflags`` -- the same treatment applied
    at the one place that knows about the flag, rather than a second policy.

    Best-effort by design: a pydub that has moved its internals gets a console
    window, which is a cosmetic regression, not a broken app.
    """
    if sys.platform != "win32":
        return
    # pydub spawns from two places and imports Popen differently in each:
    # ``utils`` holds a bare ``Popen`` name, ``audio_segment`` calls
    # ``subprocess.Popen``. Both are patched, and each is guarded on its own --
    # a pydub that has moved its internals costs a console window, which is
    # cosmetic, not a broken app.
    for module_name, attribute in (("pydub.utils", "Popen"),
                                   ("pydub.audio_segment", "subprocess")):
        try:
            module = __import__(module_name, fromlist=["_"])
        except Exception:
            continue
        if getattr(module, "_rrf_silenced", False):
            continue
        try:
            if attribute == "Popen":
                original = module.Popen
                module.Popen = _quieted(original)
            else:
                target = getattr(module, attribute)
                original = target.Popen
                target.Popen = _quieted(original)
            module._rrf_silenced = True
        except Exception:
            continue


def _quieted(original):
    """Wrap a Popen so it defaults to launching without a console."""
    def quiet_popen(*args, **kwargs):
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        return original(*args, **kwargs)

    return quiet_popen
