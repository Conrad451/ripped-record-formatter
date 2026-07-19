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
import threading

log = logging.getLogger(__name__)


#: eCapture / DEVICE_STATE_ACTIVE -- we enumerate recording endpoints that exist
#: right now, so a render endpoint can never be matched by name.
_E_CAPTURE = 1
_DEVICE_STATE_ACTIVE = 1
_STGM_READ = 0

#: PKEY_Device_FriendlyName -- the name Windows Sound shows, which is also the
#: name PortAudio reports, which is how we match the selected device.
_PKEY_FRIENDLY_NAME = ("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)


def release(pointer) -> bool:
    """Explicitly release a COM pointer and neutralise it. Returns whether it did.

    This is the whole discipline in one function. ``comtypes`` releases a pointer
    from :meth:`_compointer_base.__del__`, which means the release happens
    whenever the garbage collector gets round to it, on whatever thread it
    happens to be running -- and if the underlying object is already gone, the
    call faults inside a destructor where the traceback is useless.

    So every pointer this module obtains is released here, on purpose, at a time
    we choose. Nulling the raw value afterwards is what makes that safe:
    ``__del__`` is guarded by ``if self:``, so a null pointer is skipped, and the
    object cannot be released twice.

    The value has to be set through ``c_void_p``'s own descriptor because
    ``_compointer_base`` redefines ``.value`` as a read-only property returning
    ``self``.
    """
    if pointer is None:
        return False
    try:
        from ctypes import c_void_p
    except Exception:                       # pragma: no cover - ctypes is stdlib
        return False
    try:
        if not pointer:                     # already null: nothing owned
            return False
        pointer.Release()
        return True
    except Exception as exc:
        log.debug("Input gain: releasing %r failed (%s).", pointer, exc)
        return False
    finally:
        try:
            c_void_p.value.__set__(pointer, None)
        except Exception:                   # not a comtypes pointer; nothing to do
            pass


def release_all(*pointers) -> None:
    """Release several pointers, in reverse acquisition order, never raising."""
    for pointer in reversed(pointers):
        release(pointer)


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

        from comtypes import CLSCTX_ALL, GUID, CoCreateInstance
        from pycaw.api.endpointvolume import IAudioEndpointVolume
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY
        from pycaw.constants import CLSID_MMDeviceEnumerator
    except Exception as exc:  # ImportError, or comtypes.gen failure in a frozen build
        log.info("Input gain: COM audio interface unavailable (%s); slider hidden.", exc)
        return None

    enumerator = endpoints = None
    try:
        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        endpoints = enumerator.EnumAudioEndpoints(_E_CAPTURE, _DEVICE_STATE_ACTIVE)
        key = PROPERTYKEY(GUID(_PKEY_FRIENDLY_NAME[0]), _PKEY_FRIENDLY_NAME[1])
        for i in range(endpoints.GetCount()):
            # Every one of these is an interface pointer we own and must give
            # back. Enumerating a machine with eight capture endpoints used to
            # leave sixteen pointers for the garbage collector.
            device = store = iface = None
            try:
                device = endpoints.Item(i)
                store = device.OpenPropertyStore(_STGM_READ)
                name = store.GetValue(byref(key)).GetValue()
                if str(name) != device_name:
                    continue
                iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                # QueryInterface, *not* cast. cast() reinterprets the same raw
                # pointer into a second Python object without an AddRef, so two
                # objects each believe they own the one reference and each calls
                # Release() from __del__ -- the second one releasing memory that
                # is already gone. That is the crash this whole change exists to
                # fix. QueryInterface returns a properly counted pointer, and
                # the Activate result is then ours to hand back.
                return iface.QueryInterface(IAudioEndpointVolume)
            finally:
                release_all(device, store, iface)
        log.info("Input gain: no active capture endpoint named '%s'; slider hidden.",
                 device_name)
    except Exception as exc:
        log.info("Input gain: could not open '%s' (%s); slider hidden.", device_name, exc)
    finally:
        release_all(enumerator, endpoints)
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
        #: The thread that created the interface, and the only one allowed to
        #: touch it. COM apartment rules are per-thread, and comtypes calls
        #: CoInitialize lazily per thread -- so a pointer created on the GUI
        #: thread and released on, say, a worker or the audio callback thread is
        #: a fault waiting for the right timing. Recorded so misuse is a loud
        #: log line rather than an intermittent crash.
        self._owner_thread = threading.get_ident()

    @classmethod
    def for_device(cls, device_name: str, *, resolver=None) -> "EndpointGain | None":
        """Resolve the endpoint for ``device_name``; ``None`` if it can't be reached.

        ``resolver`` defaults to :func:`_resolve_endpoint`, looked up at call
        time rather than bound as a default argument -- binding it at class
        definition made the module attribute impossible to substitute, which is
        exactly what a lifetime test needs to do.
        """
        if not device_name:
            return None
        endpoint = (resolver or _resolve_endpoint)(device_name)
        return cls(endpoint) if endpoint is not None else None

    @property
    def closed(self) -> bool:
        return self._ep is None

    def _check_thread(self, what: str) -> bool:
        if threading.get_ident() == self._owner_thread:
            return True
        log.warning(
            "Input gain: %s attempted from thread %s but the endpoint belongs to "
            "%s; ignoring. COM pointers are not thread-free.",
            what, threading.get_ident(), self._owner_thread)
        return False

    def close(self) -> None:
        """Give the interface back, now, on the thread that acquired it.

        Idempotent, and the only place the endpoint is released. Call it when
        rebinding to another device and when shutting down -- dropping the
        reference and letting ``__del__`` do it is precisely the bug.
        """
        endpoint, self._ep = self._ep, None
        if endpoint is None:
            return
        if not self._check_thread("close"):
            # Better to leak one pointer than to release it from the wrong
            # apartment: a leak is a bounded cost, a bad release is a crash.
            return
        release(endpoint)

    #: Alias, for callers that think in terms of shutting a subsystem down.
    shutdown = close

    def __enter__(self) -> "EndpointGain":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get(self) -> float | None:
        """Current level as 0.0..1.0, or ``None`` if the read failed."""
        if self._ep is None or not self._check_thread("read"):
            return None
        try:
            return float(self._ep.GetMasterVolumeLevelScalar())
        except Exception as exc:
            log.info("Input gain: read failed (%s).", exc)
            return None

    def set(self, level: float) -> bool:
        """Set the level (clamped to 0.0..1.0). ``True`` on success."""
        if self._ep is None or not self._check_thread("write"):
            return False
        level = max(0.0, min(1.0, float(level)))
        try:
            self._ep.SetMasterVolumeLevelScalar(level, None)
            return True
        except Exception as exc:
            log.info("Input gain: write failed (%s).", exc)
            return False
