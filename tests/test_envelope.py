from shearline.envelope import DISCLAIMER, SCHEMA_VERSION, envelope


def test_envelope_has_required_fields():
    out = envelope({"x_mm": 1}, "Quiet conditions.")
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["data"] == {"x_mm": 1}
    assert out["interpretation"] == "Quiet conditions."
    assert out["degraded"] == []
    assert out["disclaimer"] == DISCLAIMER


def test_schema_version_is_semver():
    parts = SCHEMA_VERSION.split(".")
    assert len(parts) == 2 and all(p.isdigit() for p in parts)


def test_disclaimer_exact_wording():
    assert DISCLAIMER == "Informational only. Not a substitute for official NWS warnings."


def test_degraded_passthrough():
    out = envelope({}, "MRMS unavailable.", degraded=["mrms"])
    assert out["degraded"] == ["mrms"]
