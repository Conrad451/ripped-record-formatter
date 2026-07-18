"""EndpointGain: the Windows capture-endpoint level, over a fake COM endpoint."""

from __future__ import annotations

from core.input_gain import EndpointGain


class FakeEndpoint:
    """Stand-in for IAudioEndpointVolume -- a scalar 0..1 with the pycaw signature."""

    def __init__(self, level=0.5):
        self.level = level

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, value, _event_ctx):
        self.level = value


def test_gain_reads_and_writes_the_endpoint_scalar():
    ep = FakeEndpoint(0.4)
    gain = EndpointGain.for_device("USB Audio CODEC", resolver=lambda name: ep)
    assert gain is not None
    assert gain.get() == 0.4
    assert gain.set(0.75) is True
    assert ep.level == 0.75
    assert gain.get() == 0.75


def test_gain_clamps_out_of_range_values():
    ep = FakeEndpoint()
    gain = EndpointGain.for_device("dev", resolver=lambda name: ep)
    gain.set(2.0)
    assert ep.level == 1.0
    gain.set(-0.5)
    assert ep.level == 0.0


def test_for_device_returns_none_when_endpoint_unreachable():
    # A resolver that finds nothing -> hide the slider (caller checks for None).
    assert EndpointGain.for_device("Ghost", resolver=lambda name: None) is None
    # An empty device name never even tries to resolve.
    called = []
    assert EndpointGain.for_device("", resolver=lambda name: called.append(name)) is None
    assert called == []


def test_get_and_set_fail_soft_when_the_device_vanishes():
    class Dead:
        def GetMasterVolumeLevelScalar(self):
            raise OSError("device gone")

        def SetMasterVolumeLevelScalar(self, value, ctx):
            raise OSError("device gone")

    gain = EndpointGain.for_device("dev", resolver=lambda name: Dead())
    assert gain.get() is None       # never raises into the caller
    assert gain.set(0.5) is False
