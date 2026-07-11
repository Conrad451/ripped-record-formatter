"""Core, UI-agnostic logic for Ripped Record Formatter.

Nothing in this package may call ``input()``, ``print()`` or import ``tkinter``.
All user interaction happens in the CLI/GUI layers, which drive these modules
and receive progress via callbacks and return values.
"""
