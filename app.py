"""Entry point for the Ripped Record Formatter desktop GUI.

Run from the repo root::

    python app.py

Launching from the repo root means ``core`` and ``gui`` are importable without
any sys.path juggling. The legacy terminal UI remains available at
``v2/Ripped_Record_Formatter.py``.
"""

import sys

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
