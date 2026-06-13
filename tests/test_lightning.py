"""Tests for the GOES GLM lightning tool (Task 4).

Pure parsing/interpretation is tested directly; the S3 fetch + netCDF decode is
tested offline by replaying the recorded granule (fixtures/goes_glm_lcfa_sample.nc,
a real G19 file with 500 flashes over the Americas, 2026-06-13 19:03:40-19:04:00Z)
through monkeypatched boto3 calls — no network, no live S3.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from shearline import server
from shearline.bounds import OutOfBoundsError
from shearline.cache import CACHE
from shearline.envelope import DISCLAIMER
from shearline.sources import lightning

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GRANULE = (FIXTURES / "goes_glm_lcfa_sample.nc").read_bytes()
# A real flash cluster inside the recorded granule (~19 flashes within 40 km;
# densest 0.5-deg cell, northern Arkansas / southern Missouri).
FLASH_LAT, FLASH_LON = 36.0, -92.0


@pytest.fixture(autouse=True)
def clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


# --- key parsing -------------------------------------------------------------


def test_parse_start_decodes_stag():
    key = "GLM-L2-LCFA/2026/164/19/OR_GLM-L2-LCFA_G19_s20261641903400_e20261641904000_c20261641904023.nc"
    dt = lightning._parse_start(key)
    assert dt == datetime(2026, 6, 13, 19, 3, 40, tzinfo=UTC)


def test_parse_start_rejects_garbage():
    assert lightning._parse_start("not-a-glm-key.nc") is None


def test_hour_prefixes_spans_boundary():
    start = datetime(2026, 6, 13, 18, 58, tzinfo=UTC)
    end = datetime(2026, 6, 13, 19, 3, tzinfo=UTC)
    prefixes = lightning._hour_prefixes(start, end)
    assert prefixes == ["GLM-L2-LCFA/2026/164/18/", "GLM-L2-LCFA/2026/164/19/"]


# --- granule sampling --------------------------------------------------------


def test_sample_granule_finds_flashes_in_radius():
    flashes = lightning._sample_granule_sync(GRANULE, FLASH_LAT, FLASH_LON, radius_km=40)
    assert flashes  # at least one flash near the cluster
    assert all(f["distance_km"] <= 40 for f in flashes)
    assert all("direction" in f and "time_utc" in f for f in flashes)


def test_sample_granule_empty_far_from_any_flash():
    # mid-Pacific: no GLM flashes from a CONUS-facing granule
    assert lightning._sample_granule_sync(GRANULE, 35.0, -97.5, radius_km=40) == []


# --- interpretation tiers ----------------------------------------------------


def test_interpret_quiet():
    data = {"flash_count": 0, "window_minutes": 15, "radius_km": 40}
    text = lightning.interpret(data)
    assert "No lightning detected" in text and "GOES-East GLM" in text


def test_interpret_overhead_danger():
    data = {
        "flash_count": 5, "window_minutes": 15, "radius_km": 40, "flashes_per_min": 0.3,
        "nearest_strike": {"distance_km": 3.0, "direction": "NW", "time_utc": "2026-06-13T19:03:40Z"},
    }
    text = lightning.interpret(data)
    assert "DANGER" in text and "30-30" in text


def test_interpret_striking_distance():
    data = {
        "flash_count": 2, "window_minutes": 15, "radius_km": 40, "flashes_per_min": 0.1,
        "nearest_strike": {"distance_km": 13.0, "direction": "S", "time_utc": "2026-06-13T19:03:40Z"},
    }
    text = lightning.interpret(data)
    assert "striking distance" in text and "shelter" in text


def test_interpret_in_the_area():
    data = {
        "flash_count": 1, "window_minutes": 15, "radius_km": 40, "flashes_per_min": 0.1,
        "nearest_strike": {"distance_km": 30.0, "direction": "E", "time_utc": "2026-06-13T19:03:40Z"},
    }
    text = lightning.interpret(data)
    assert "in the area" in text and "Monitor" in text


# --- tool (offline via monkeypatched S3) -------------------------------------


@pytest.fixture
def fake_s3(monkeypatch):
    """Stub the lightning module's S3 client: one granule in the window."""
    key = "GLM-L2-LCFA/2026/164/19/OR_GLM-L2-LCFA_G19_s20261641903400_e20261641904000_c20261641904023.nc"

    class FakeBody:
        def read(self):
            return GRANULE

    class FakeS3:
        def list_objects_v2(self, **kw):
            return {"Contents": [{"Key": key}]}

        def get_object(self, **kw):
            return {"Body": FakeBody()}

    # _parse_start filters by the window; force the window to include the granule
    # by monkeypatching _list_window_keys_sync to return the key directly.
    monkeypatch.setattr(lightning, "_s3", lambda: FakeS3())
    monkeypatch.setattr(lightning, "_list_window_keys_sync", lambda b, s, e: [key])
    return key


async def test_tool_returns_envelope_with_flashes(fake_s3):
    out = await server.get_lightning(FLASH_LAT, FLASH_LON, radius_km=40, minutes=15)
    assert set(out) == {"schema_version", "data", "interpretation", "degraded", "disclaimer"}
    assert out["disclaimer"] == DISCLAIMER
    assert out["degraded"] == []
    assert out["data"]["flash_count"] > 0
    assert out["data"]["satellite"] == lightning.SATELLITE_EAST
    assert out["data"]["nearest_strike"]["distance_km"] <= 40


async def test_tool_quiet_point_reports_zero(fake_s3):
    out = await server.get_lightning(35.0, -97.5, radius_km=40, minutes=15)  # far from any flash
    assert out["data"]["flash_count"] == 0
    assert "No lightning detected" in out["interpretation"]


async def test_tool_degrades_on_s3_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("S3 unreachable")

    monkeypatch.setattr(lightning, "_list_window_keys_sync", boom)
    out = await server.get_lightning(FLASH_LAT, FLASH_LON)
    assert out["degraded"] == ["goes-glm"]
    assert "unavailable" in out["interpretation"]


async def test_tool_rejects_out_of_bounds():
    with pytest.raises(OutOfBoundsError):
        await server.get_lightning(51.5, -0.12)


async def test_tool_clamps_minutes_and_radius(fake_s3, monkeypatch):
    captured = {}

    async def fake_fetch(lat, lon, radius_km, minutes):
        captured["radius_km"] = radius_km
        captured["minutes"] = minutes
        return {"flash_count": 0, "window_minutes": minutes, "radius_km": radius_km}

    monkeypatch.setattr(lightning, "fetch_lightning", fake_fetch)
    await server.get_lightning(FLASH_LAT, FLASH_LON, radius_km=9999, minutes=9999)
    assert captured["radius_km"] == 100  # clamp ceiling
    assert captured["minutes"] == 30
