from shearline.envelope import DISCLAIMER, envelope


def test_envelope_has_required_fields():
    out = envelope({"x_mm": 1}, "Quiet conditions.")
    assert out["data"] == {"x_mm": 1}
    assert out["interpretation"] == "Quiet conditions."
    assert out["degraded"] == []
    assert out["disclaimer"] == DISCLAIMER


def test_disclaimer_exact_wording():
    assert DISCLAIMER == "Informational only. Not a substitute for official NWS warnings."


def test_degraded_passthrough():
    out = envelope({}, "MRMS unavailable.", degraded=["mrms"])
    assert out["degraded"] == ["mrms"]
