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

from gui.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
