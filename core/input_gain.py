"""Windows capture-endpoint input level -- the "Recording" volume slider that
Windows Sound exposes per input device. GUI-free.

This is the *only* module that touches ``pycaw``/``comtypes``. It resolves the
``IAudioEndpointVolume`` for a capture endpoint by its FriendlyName (which matches
the name PortAudio/sounddevice reports for the same device) and reads/writes its
master scalar volume, 0.0..1.0 -- the same 0..100 the Windows slider shows.

Everything degrades to ``None``/``False`` when the endpoint is unreachable -- no
COM (a non-Windows or frozen build missing the interface), the device gone, or
insufficient rights. Callers hide the slider on ``None`` and log a single line;
nothing here raises into the GUI.

Why the WASAPI endpoint (not a software gain): the operator's problem is a signal
that clips before it reaches us. Applying gain *after* capture cannot un-clip it.
The Windows input level sits *ahead* of the ADC's digital path on class-compliant
USB interfaces, so it is the same knob the Windows Sound panel drives -- moving it
here moves it there, live, for whatever is metering.

Frozen (PyInstaller) note: ``comtypes`` generates COM interface wrappers into a
``comtypes.gen`` package at first use. A one-file build must ship those (either
collect ``comtypes`` + ``pycaw`` submodules, or set ``COMTYPES_GEN_DIR`` to a
writable path), else ``_resolve_endpoint`` raises on import/gen and the slider
simply stays hidden -- degraded, never crashed. See docs/BUILD notes for the
PyInstaller hidden-import list.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _resolve_endpoint(device_name: str):
    """The ``IAudioEndpointVolume`` for the capture endpoint whose FriendlyName is
    ``device_name``, or ``None`` if it can't be reached.

    Isolated behind this function so the COM dependency lives in one place and
    tests can inject a fake endpoint by passing their own ``resolver``.
    """
    try:
        import warnings

        from comtypes import CLSCTX_ALL, POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as exc:  # ImportError, or comtypes.gen failure in a frozen build
        log.info("Input gain: COM audio interface unavailable (%s); slider hidden.", exc)
        return None
    try:
        with warnings.catch_warnings():
            # Enumerating every endpoint trips benign per-property COMErrors that
            # pycaw surfaces as warnings; we only read FriendlyName, so hush them.
            warnings.simplefilter("ignore")
            for dev in AudioUtilities.GetAllDevices():
                if dev.FriendlyName == device_name:
                    iface = dev._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    return cast(iface, POINTER(IAudioEndpointVolume))
    except Exception as exc:
        log.info("Input gain: could not open '%s' (%s); slider hidden.", device_name, exc)
    return None


class EndpointGain:
    """A live handle on one capture endpoint's master volume, 0.0..1.0.

    Build one with :meth:`for_device`, which returns ``None`` when the endpoint is
    inaccessible -- the caller hides the slider on ``None``. :meth:`get` and
    :meth:`set` also fail soft (``None``/``False``) if the device vanishes mid-run,
    so a device unplugged while metering never throws into the UI.
    """

    def __init__(self, endpoint) -> None:
        self._ep = endpoint

    @classmethod
    def for_device(cls, device_name: str, *, resolver=_resolve_endpoint) -> "EndpointGain | None":
        """Resolve the endpoint for ``device_name``; ``None`` if it can't be reached."""
        if not device_name:
            return None
        endpoint = resolver(device_name)
        return cls(endpoint) if endpoint is not None else None

    def get(self) -> float | None:
        """Current level as 0.0..1.0, or ``None`` if the read failed."""
        try:
            return float(self._ep.GetMasterVolumeLevelScalar())
        except Exception as exc:
            log.info("Input gain: read failed (%s).", exc)
            return None

    def set(self, level: float) -> bool:
        """Set the level (clamped to 0.0..1.0). ``True`` on success."""
        level = max(0.0, min(1.0, float(level)))
        try:
            self._ep.SetMasterVolumeLevelScalar(level, None)
            return True
        except Exception as exc:
            log.info("Input gain: write failed (%s).", exc)
            return False
