"""Choosing a cover image from disk, wherever the app admits it has none.

The amber "no cover art" state was honest and useless: it told you something was
missing and then ended the conversation. A record with no art in the Cover Art
Archive is common -- private presses, reissues, anything obscure -- and the
person looking at that warning very often has the sleeve scanned on their own
disk.

So every place that reports missing art gains the same affordance, built once
here: Re-tag, Full Rip's preview row, the Record tab's album row, and the lookup
dialog. It produces the same :class:`~core.metadata_lookup.CoverArt` the network
path produces, so it travels through the identical ``cover=`` plumbing and gets
embedded by the identical code. Nothing downstream can tell where the bytes came
from, which is the point.

**Sanity caps, and why these numbers.** 10 MB and 5000x5000. A cover is embedded
into *every track*, so a 40 MB scan becomes 40 MB times fourteen inside the
album -- the file size stops being about the music. 5000px is comfortably beyond
what any player displays and beyond a 600dpi scan of a 12" sleeve, so anything
larger is a photograph of something else or a mistake. Both are refused with the
actual limit named, never silently resized: re-encoding someone's artwork behind
their back is not this function's business.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog

#: Bytes. Embedded once per track, so this is really a per-album multiplier.
MAX_COVER_BYTES = 10 * 1024 * 1024

#: Pixels, each dimension. Past any player's display and past a 600dpi sleeve scan.
MAX_COVER_PIXELS = 5000

#: What the picker will accept. JPEG and PNG are what the embedding paths and
#: every player agree on; adding more formats here would mean adding them there.
_FILTER = "Cover image (*.jpg *.jpeg *.png)"

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def load_cover_file(path) -> tuple[object | None, str]:
    """Read ``path`` as cover art. Returns ``(CoverArt, "")`` or ``(None, why)``.

    The ``why`` is a finished sentence for the user, naming the limit that was
    exceeded and the value that exceeded it -- "too big" without a number is a
    dead end of exactly the kind this feature exists to remove.
    """
    from core.metadata_lookup import CoverArt

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in _MIME:
        return None, (f"Cover art: {path.name} is not a JPEG or PNG. "
                      "Those are the formats players agree on.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"Cover art: could not read {path.name} ({exc})."

    if not data:
        return None, f"Cover art: {path.name} is empty."
    if len(data) > MAX_COVER_BYTES:
        return None, (
            f"Cover art: {path.name} is {len(data) / 1024 / 1024:.1f} MB, over "
            f"the {MAX_COVER_BYTES // 1024 // 1024} MB limit. The image is "
            "embedded in every track, so a large scan multiplies across the "
            "whole album. Save it smaller and try again.")

    width, height = _dimensions(data)
    if width and (width > MAX_COVER_PIXELS or height > MAX_COVER_PIXELS):
        return None, (
            f"Cover art: {path.name} is {width}x{height}, over the "
            f"{MAX_COVER_PIXELS}x{MAX_COVER_PIXELS} limit. Nothing displays a "
            "cover that large. Scale it down and try again.")

    return CoverArt(data=data, mime=_MIME[suffix]), ""


def _dimensions(data: bytes) -> tuple[int, int]:
    """``(width, height)``, or ``(0, 0)`` if they cannot be determined.

    Uses Qt, which is already loaded, rather than adding an imaging dependency
    for two integers. An image Qt cannot measure is *not* rejected on that
    basis -- the byte cap still applies, and refusing a file for being
    unmeasurable would fail closed on something a player might read perfectly.
    """
    try:
        from PySide6.QtGui import QImage

        image = QImage()
        if not image.loadFromData(data):
            return 0, 0
        return image.width(), image.height()
    except Exception:
        return 0, 0


def choose_cover_file(parent=None, *, start_dir: str = "") -> tuple[object | None, str]:
    """Ask for an image, then load it. ``(None, "")`` means the user cancelled."""
    chosen, _ = QFileDialog.getOpenFileName(
        parent, "Choose cover image", start_dir or str(Path.home()), _FILTER)
    if not chosen:
        return None, ""
    return load_cover_file(chosen)
