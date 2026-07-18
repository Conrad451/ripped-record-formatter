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


#: eCapture / DEVICE_STATE_ACTIVE -- we enumerate recording endpoints that exist
#: right now, so a render endpoint can never be matched by name.
_E_CAPTURE = 1
_DEVICE_STATE_ACTIVE = 1
_STGM_READ = 0

#: PKEY_Device_FriendlyName -- the name Windows Sound shows, which is also the
#: name PortAudio reports, which is how we match the selected device.
_PKEY_FRIENDLY_NAME = ("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)


def _resolve_endpoint(device_name: str):
    """The ``IAudioEndpointVolume`` for the capture endpoint whose FriendlyName is
    ``device_name``, or ``None`` if it can't be reached.

    Isolated behind this function so the COM dependency lives in one place and
    tests can inject a fake endpoint by passing their own ``resolver``.

    Goes at ``IMMDeviceEnumerator`` directly rather than through
    ``pycaw.utils.AudioUtilities``: that convenience layer enumerates *every*
    endpoint in every state and reads every property (which raises benign
    COMErrors it surfaces as warnings), and it drags ``psutil`` into the bundle
    for session bookkeeping we never use. Asking for active capture endpoints and
    one property is both narrower and correct by construction.
    """
    try:
        from ctypes import byref

        from comtypes import CLSCTX_ALL, GUID, POINTER, CoCreateInstance, cast
        from pycaw.api.endpointvolume import IAudioEndpointVolume
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY
        from pycaw.constants import CLSID_MMDeviceEnumerator
    except Exception as exc:  # ImportError, or comtypes.gen failure in a frozen build
        log.info("Input gain: COM audio interface unavailable (%s); slider hidden.", exc)
        return None

    try:
        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        endpoints = enumerator.EnumAudioEndpoints(_E_CAPTURE, _DEVICE_STATE_ACTIVE)
        key = PROPERTYKEY(GUID(_PKEY_FRIENDLY_NAME[0]), _PKEY_FRIENDLY_NAME[1])
        for i in range(endpoints.GetCount()):
            device = endpoints.Item(i)
            name = device.OpenPropertyStore(_STGM_READ).GetValue(byref(key)).GetValue()
            if str(name) == device_name:
                iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                return cast(iface, POINTER(IAudioEndpointVolume))
        log.info("Input gain: no active capture endpoint named '%s'; slider hidden.",
                 device_name)
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
