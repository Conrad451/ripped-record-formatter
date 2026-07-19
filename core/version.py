"""The single source of truth for the application version.

Every place that reports a version imports it from here -- the GUI window title,
the CLI's ``--version``, and the MusicBrainz User-Agent (which MusicBrainz's
terms of use require to carry an application version). Never write the version
literal anywhere else; bump it here and all three follow.
"""

from __future__ import annotations

__version__ = "3.2.0"
