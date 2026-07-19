"""Test-wide isolation.

``gui.main_window.Settings`` calls :func:`core.config.load` on construction and
:func:`core.config.save` on every change -- against the *real* per-user settings
file. Any test that builds a MainWindow therefore read and wrote the developer's
actual config, and a test release named "Art"/"Alb" ended up persisted as their
remembered artist/album (which is why those turned up looking like placeholder
text in the Full Rip fields).

Point the config at a throwaway file for the whole session so tests can never
touch real user state.

The same applies to the sound card. Since the Record tab became the landing
tab, showing a MainWindow activates it, which opens a real capture stream on
the developer's actual device -- tens of thousands of audio callbacks over a
suite run, and a suite that can block on hardware. Tests that genuinely care
about the monitor stub it themselves (see ``no_hardware``); everyone else gets
silence by default.
"""

from __future__ import annotations

import pytest

from core import config as core_config


@pytest.fixture(autouse=True, scope="session")
def isolated_user_config(tmp_path_factory):
    real = core_config.config_path
    path = tmp_path_factory.mktemp("config") / "settings.json"
    core_config.config_path = lambda: path
    yield path
    core_config.config_path = real


class _SilentStream:
    """A PortAudio stream that exists, answers, and never touches hardware."""

    def __init__(self, *args, **kwargs):
        self.latency = 0.0

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def unremembered_ui_preferences():
    """Remembered UI state must not leak from one test into the next.

    The config file is isolated from the *user*, but it is still shared across
    the session -- so a test that expands the log pane changes what "the
    default" looks like for every test after it. Preferences whose whole job is
    to persist are reset per test; the tests that care about persistence set
    them explicitly and still see it work within their own run.
    """
    from core import config as core_config

    saved = core_config.load()
    previous = saved.log_expanded
    saved.log_expanded = False
    core_config.save(saved)
    yield
    restored = core_config.load()
    restored.log_expanded = previous
    core_config.save(restored)


@pytest.fixture(autouse=True)
def silent_audio_hardware(monkeypatch):
    """No test opens a real device unless it deliberately does.

    Patched at the hardware boundary -- ``sounddevice``'s stream classes --
    rather than on our own classes. That distinction matters: the recorder and
    the monitor both accept an injected ``stream_factory``, and the tests that
    care about their behaviour rely on driving the *real* start/stop paths with
    a fake stream. Stubbing our methods would have disabled the very code those
    tests exercise; stubbing PortAudio only removes the sound card.
    """
    import sounddevice as sd

    monkeypatch.setattr(sd, "InputStream", _SilentStream)
    monkeypatch.setattr(sd, "OutputStream", _SilentStream)


@pytest.fixture(scope="module")
def qapp_gui():
    """A QApplication for tests that build real windows."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])
