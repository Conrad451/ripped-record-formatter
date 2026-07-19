"""Entry point for the Ripped Record Formatter desktop GUI.

Run from the repo root::

    python app.py

Launching from the repo root means ``core`` and ``gui`` are importable without
any sys.path juggling. The legacy terminal UI remains available at
``v2/Ripped_Record_Formatter.py``.
"""

import sys

# Before anything imports pydub: put the resolved ffmpeg on PATH so pydub's
# import-time probe finds it. Otherwise the frozen app prints a RuntimeWarning
# claiming ffmpeg is missing while it sits bundled a few directories away.
from core.ffmpeg_locator import prime_path

prime_path()

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import config as core_config  # noqa: E402
from core.store import Store  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402


def open_state() -> Store:
    """Open rrf.db and point the settings layer at it, migrating once if needed.

    Done here rather than lazily inside the config layer so the order is
    obvious: the store exists before anything reads a setting. If the database
    cannot be opened at all the app still runs -- on defaults, with nothing
    remembered -- because state is recoverable and the library is not.
    """
    store = Store()
    try:
        core_config.migrate_json_to_store(store)
        core_config.use_store(store)
    except Exception as exc:                      # pragma: no cover - defensive
        print(f"State: could not open {store.path} ({exc}); "
              "running with defaults this session.", file=sys.stderr)
    return store


def main() -> int:
    app = QApplication(sys.argv)
    store = open_state()
    window = MainWindow(store=store)
    window.show()
    try:
        return app.exec()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
