"""COM lifetime: no interface pointer is ever left for the garbage collector.

The bug this guards against, from a field faulthandler trace: the app died at
launch with a native access violation inside
``comtypes._compointer_base.__del__ -> Release()``, triggered incidentally by a
garbage collection that happened to fire during pyqtgraph's axis painting.

The mechanism, confirmed against real hardware while fixing it: the resolver
used ``comtypes.cast()`` to convert the ``Activate`` result into an
``IAudioEndpointVolume`` pointer. ``cast()`` reinterprets the *same raw pointer*
into a second Python object **without an AddRef** -- so two ``_compointer_base``
objects each believed they owned the single reference, and each called
``Release()`` from ``__del__``. The second one released memory that was already
freed. It was intermittent purely because collection timing is.

So these tests do not try to reproduce a crash by launching repeatedly, which
would be a coin flip. They force collection, and they assert the *discipline*:

* every acquisition is matched by an explicit release,
* nothing is released by ``__del__``,
* no pointer of ours survives shutdown,
* and none of that changes when the device is re-resolved.

The COM interfaces are mocked, because CI has no sound card -- but the mocks
count their own AddRef/Release traffic, so the lifetime rules are exercised for
real even where the hardware is not.
"""

from __future__ import annotations

import gc
import threading

import pytest

from core import input_gain


# --------------------------------------------------------------------------- #
# Mocks that keep score
# --------------------------------------------------------------------------- #
class ComLedger:
    """Records every acquisition and release, and who did the releasing."""

    def __init__(self) -> None:
        self.acquired: list[FakePointer] = []
        self.explicit_releases: list[FakePointer] = []
        #: The thing that must never happen: a pointer released from __del__.
        self.finalizer_releases: list[str] = []

    @property
    def outstanding(self) -> list["FakePointer"]:
        return [p for p in self.acquired if not p.released]


class FakePointer:
    """Stands in for a comtypes interface pointer.

    Mimics the two behaviours that matter: it is falsy once neutralised (which
    is how ``_compointer_base.__del__`` decides whether to release), and it
    releases itself from ``__del__`` if nobody released it first -- which is
    precisely the failure the sentinel below turns into a test failure.
    """

    def __init__(self, ledger: ComLedger, name: str) -> None:
        self._ledger = ledger
        self.name = name
        self.released = False
        self.neutralised = False
        self.release_thread: int | None = None
        ledger.acquired.append(self)

    def __bool__(self) -> bool:
        return not self.neutralised

    def Release(self) -> int:
        self.released = True
        self.release_thread = threading.get_ident()
        self._ledger.explicit_releases.append(self)
        return 0

    def QueryInterface(self, _interface):
        """A properly counted new reference -- what cast() failed to give."""
        return FakePointer(self._ledger, f"{self.name}->QI")

    def __del__(self):
        # The sentinel. If this fires on an unreleased pointer, our code let the
        # garbage collector do the releasing, which is the whole bug.
        if not self.released and not self.neutralised:
            self._ledger.finalizer_releases.append(self.name)


class FakeEndpointVolume(FakePointer):
    """The interface the gain control actually drives."""

    def __init__(self, ledger: ComLedger, name: str = "IAudioEndpointVolume") -> None:
        super().__init__(ledger, name)
        self.level = 0.5

    def GetMasterVolumeLevelScalar(self) -> float:
        if self.released:
            raise AssertionError("read from a released endpoint (use-after-free)")
        return self.level

    def SetMasterVolumeLevelScalar(self, level, _guid) -> None:
        if self.released:
            raise AssertionError("write to a released endpoint (use-after-free)")
        self.level = level


@pytest.fixture
def ledger(monkeypatch):
    """A resolver that hands out counted fake pointers, plus real release()."""
    book = ComLedger()

    def fake_resolver(device_name: str):
        if device_name == "missing":
            return None
        return FakeEndpointVolume(book, f"endpoint:{device_name}")

    # The real release() reaches into ctypes for comtypes pointers; for fakes it
    # must still mark them released and neutralised, so wrap rather than replace.
    real_release = input_gain.release

    def counting_release(pointer):
        if isinstance(pointer, FakePointer):
            if pointer is None or not pointer:
                return False
            pointer.Release()
            pointer.neutralised = True
            return True
        return real_release(pointer)

    monkeypatch.setattr(input_gain, "release", counting_release)
    monkeypatch.setattr(input_gain, "_resolve_endpoint", fake_resolver)
    return book


def assert_clean(book: ComLedger) -> None:
    """Every acquisition explicitly released, none by the collector."""
    gc.collect()
    assert book.finalizer_releases == [], (
        f"released by __del__ instead of explicitly: {book.finalizer_releases}")
    assert book.outstanding == [], (
        f"still holding: {[p.name for p in book.outstanding]}")


# --------------------------------------------------------------------------- #
# The discipline
# --------------------------------------------------------------------------- #
def test_a_closed_handle_released_its_endpoint_explicitly(ledger):
    gain = input_gain.EndpointGain.for_device("Line In")
    assert gain is not None
    assert len(ledger.acquired) == 1

    gain.close()

    assert ledger.explicit_releases, "close() released nothing"
    assert_clean(ledger)


def test_close_is_idempotent(ledger):
    gain = input_gain.EndpointGain.for_device("Line In")
    gain.close()
    gain.close()
    gain.close()

    assert len(ledger.explicit_releases) == 1, "released more than once"
    assert gain.closed
    assert_clean(ledger)


def test_the_handle_is_dead_after_close_rather_than_using_freed_memory(ledger):
    gain = input_gain.EndpointGain.for_device("Line In")
    gain.close()

    # Reads and writes after close must be refused, not attempted -- the mock
    # raises on use-after-free, so this would be loud if it slipped through.
    assert gain.get() is None
    assert gain.set(0.8) is False
    assert_clean(ledger)


def test_the_context_manager_releases_on_the_way_out(ledger):
    with input_gain.EndpointGain.for_device("Line In") as gain:
        assert gain.get() == pytest.approx(0.5)
    assert_clean(ledger)


def test_nothing_survives_repeated_cycles_with_collection_forced(ledger):
    """The shape of the field crash: collection firing between and during use."""
    for _ in range(25):
        gain = input_gain.EndpointGain.for_device("Line In")
        gc.collect()                       # mid-life, as the trace showed
        gain.get()
        gain.set(0.7)
        gc.collect()
        gain.close()
        gc.collect()                       # and after
        assert ledger.finalizer_releases == []

    assert len(ledger.acquired) == 25
    assert len(ledger.explicit_releases) == 25
    assert_clean(ledger)


def test_re_resolution_releases_the_previous_endpoint_before_rebinding(ledger):
    """The leak site the audit predicted: switching devices used to drop the
    old pointer for the collector rather than handing it back."""
    gain = None
    for name in ("Line In", "USB Mic", "Line In", "Headset"):
        if gain is not None:
            gain.close()
        gain = input_gain.EndpointGain.for_device(name)
        gc.collect()
    gain.close()

    assert len(ledger.acquired) == 4
    assert len(ledger.explicit_releases) == 4
    assert_clean(ledger)


def test_an_unreachable_endpoint_acquires_nothing(ledger):
    """The degraded contract is unchanged: no endpoint, no handle, no leak."""
    assert input_gain.EndpointGain.for_device("missing") is None
    assert input_gain.EndpointGain.for_device("") is None
    assert ledger.acquired == []
    assert_clean(ledger)


# --------------------------------------------------------------------------- #
# Thread confinement
# --------------------------------------------------------------------------- #
def test_the_endpoint_refuses_use_from_another_thread(ledger):
    """COM apartments are per-thread. A pointer created on the GUI thread and
    touched from a worker is a fault waiting for the right timing, so it is
    refused loudly instead."""
    gain = input_gain.EndpointGain.for_device("Line In")
    results = {}

    def worker():
        results["get"] = gain.get()
        results["set"] = gain.set(0.9)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert results["get"] is None, "a read from the wrong thread was allowed"
    assert results["set"] is False, "a write from the wrong thread was allowed"

    gain.close()
    assert_clean(ledger)


def test_closing_from_another_thread_leaks_rather_than_faults(ledger):
    """Deliberate: a leak is a bounded cost, a release from the wrong apartment
    is a crash. The handle still stops being usable."""
    gain = input_gain.EndpointGain.for_device("Line In")

    thread = threading.Thread(target=gain.close)
    thread.start()
    thread.join()

    assert gain.closed                          # unusable from here on...
    assert ledger.explicit_releases == []       # ...but not released wrongly
    assert ledger.finalizer_releases == []      # and not handed to the collector


def test_release_is_confined_to_the_owning_thread(ledger):
    gain = input_gain.EndpointGain.for_device("Line In")
    owner = threading.get_ident()
    gain.close()

    assert ledger.explicit_releases[0].release_thread == owner
    assert_clean(ledger)


# --------------------------------------------------------------------------- #
# Through the GUI, which is where the crash actually happened
# --------------------------------------------------------------------------- #
def test_the_record_tab_releases_the_endpoint_on_device_change(ledger, qapp_gui):
    """Rebinding self._gain used to drop the old pointer silently."""
    from gui.main_window import MainWindow

    window = MainWindow()
    tab = window.record_tab

    # Constructing the window already resolves whatever device is selected, so
    # measure the change across the re-resolve rather than an absolute count.
    tab._sync_gain_slider("Line In")
    before = len(ledger.explicit_releases)
    held = ledger.outstanding
    assert len(held) == 1, f"expected one live endpoint, got {[p.name for p in held]}"

    tab._sync_gain_slider("USB Mic")             # re-resolve
    gc.collect()

    assert len(ledger.explicit_releases) == before + 1, (
        "the previous endpoint was not handed back before rebinding")
    assert held[0].released

    tab._release_gain()
    window.close()
    assert_clean(ledger)


def test_the_record_tab_releases_the_endpoint_on_shutdown(ledger, qapp_gui):
    """A clean close must not be a smaller version of the same crash."""
    from gui.main_window import MainWindow

    window = MainWindow()
    window.record_tab._sync_gain_slider("Line In")
    assert len(ledger.outstanding) == 1

    window.close()                               # closeEvent -> shutdown()
    gc.collect()

    assert_clean(ledger)


def test_closing_the_window_tears_the_record_tab_down(qapp_gui):
    """The regression underneath: RecordTab.shutdown() was never called at all,
    so the COM pointer, both audio streams and the UI timer were left to
    interpreter teardown."""
    from gui.main_window import MainWindow

    window = MainWindow()
    called = []
    window.record_tab.shutdown = lambda: called.append(True)

    window.close()

    assert called == [True], "the Record tab was never shut down on close"
