"""Test-wide isolation.

``gui.main_window.Settings`` calls :func:`core.config.load` on construction and
:func:`core.config.save` on every change -- against the *real* per-user settings
file. Any test that builds a MainWindow therefore read and wrote the developer's
actual config, and a test release named "Art"/"Alb" ended up persisted as their
remembered artist/album (which is why those turned up looking like placeholder
text in the Full Rip fields).

Point the config at a throwaway file for the whole session so tests can never
touch real user state.
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
