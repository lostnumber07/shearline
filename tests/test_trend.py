"""Tests for the forecast-environment trend tool (Task 3).

Pure trend logic (derive/trend.py) is tested with synthetic series; the fetch +
decode orchestration is tested offline by replaying the recorded RAP f03 subset
(fixtures/rap_subset_okc_f03.grib2) through respx for every forecast-hour request.
"""

from pathlib import Path

import pytest
import respx

from shearline import server
from shearline.bounds import OutOfBoundsError
from shearline.cache import CACHE
from shearline.derive.trend import interpret_trend, summarize
from shearline.envelope import DISCLAIMER
from shearline.sources import rap

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OKC_LAT, OKC_LON = 35.47, -97.52


def f03_bytes() -> bytes:
    return (FIXTURES / "rap_subset_okc_f03.grib2").read_bytes()


@pytest.fixture(autouse=True)
def clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


def _env(mlcape=None, shear6=None, srh1=None, scp=None, stp_eff=None, stp_fixed=None, valid="V"):
    return {
        "valid_utc": valid,
        "thermodynamics": {"mlcape_jkg": mlcape},
        "kinematics": {"bulk_shear_0_6km_kt": shear6, "srh_0_1km_m2s2": srh1},
        "composites": {"scp": scp, "stp_effective": stp_eff, "stp_fixed_layer": stp_fixed},
    }


# --- summarize ---------------------------------------------------------------


def test_summarize_pulls_fields_and_stp_effective_preferred():
    s = summarize(_env(mlcape=2000, shear6=40, srh1=200, scp=5, stp_eff=3.0, stp_fixed=1.0), 3)
    assert s == {
        "forecast_hour": 3,
        "valid_utc": "V",
        "mlcape_jkg": 2000,
        "bulk_shear_0_6km_kt": 40,
        "srh_0_1km_m2s2": 200,
        "scp": 5,
        "stp": 3.0,
    }


def test_summarize_falls_back_to_fixed_layer_stp():
    s = summarize(_env(stp_eff=None, stp_fixed=1.4), 0)
    assert s["stp"] == 1.4


# --- interpret_trend ---------------------------------------------------------


def test_trend_intensifying_leads_on_rising_stp():
    series = [summarize(_env(mlcape=1500, shear6=40, stp_eff=1.0), 0),
              summarize(_env(mlcape=2500, shear6=45, stp_eff=4.0), 3)]
    text = interpret_trend(series)
    assert "rising" in text and "tornado-favorable" in text
    assert "1 → 4" in text or "1 → 4" in text


def test_trend_weakening_leads_on_falling_stp():
    series = [summarize(_env(mlcape=2500, shear6=45, stp_eff=4.0), 0),
              summarize(_env(mlcape=1500, shear6=40, stp_eff=1.0), 6)]
    text = interpret_trend(series)
    assert "falling" in text and "closing" in text


def test_trend_destabilizing_on_rising_cape():
    series = [summarize(_env(mlcape=800, shear6=35, stp_eff=0.0), 0),
              summarize(_env(mlcape=2600, shear6=42, stp_eff=0.0), 6)]
    text = interpret_trend(series)
    assert "Destabilizing" in text


def test_trend_stabilizing_on_falling_cape():
    series = [summarize(_env(mlcape=3200, shear6=20, stp_eff=0.0), 0),
              summarize(_env(mlcape=1800, shear6=30, stp_eff=0.0), 6)]
    text = interpret_trend(series)
    assert "Stabilizing" in text


def test_trend_steady_when_little_change():
    series = [summarize(_env(mlcape=1500, shear6=35, stp_eff=0.5), 0),
              summarize(_env(mlcape=1600, shear6=36, stp_eff=0.6), 6)]
    text = interpret_trend(series)
    assert "steady" in text.lower()


def test_trend_single_time_and_empty():
    assert "Single-time" in interpret_trend([summarize(_env(mlcape=1000), 0)])
    assert "No forecast" in interpret_trend([])


# --- fetch orchestration (offline) -------------------------------------------


@respx.mock
async def test_fetch_forecast_profiles_anchors_and_tags_hours():
    route = respx.get(rap.FILTER_URL).respond(200, content=f03_bytes())
    cycle_iso, profiles = await rap.fetch_forecast_profiles(OKC_LAT, OKC_LON, fhrs=[0, 3])
    assert len(profiles) == 2
    assert [p["forecast_hour"] for p in profiles] == [0, 3]
    # the requested filenames carry the right forecast-hour tokens
    files = [dict(c.request.url.params)["file"] for c in route.calls]
    assert any("f00" in f for f in files) and any("f03" in f for f in files)
    assert cycle_iso.endswith("Z")


@respx.mock
async def test_tool_returns_trend_envelope():
    respx.get(rap.FILTER_URL).respond(200, content=f03_bytes())
    out = await server.get_environment_trend(OKC_LAT, OKC_LON)
    assert set(out) == {"data", "interpretation", "degraded", "disclaimer"}
    assert out["disclaimer"] == DISCLAIMER
    assert out["degraded"] == []
    assert out["data"]["forecast_hours"] == [0, 1, 3, 6]
    assert len(out["data"]["series"]) == 4
    for s in out["data"]["series"]:
        assert {"forecast_hour", "valid_utc", "mlcape_jkg", "bulk_shear_0_6km_kt",
                "srh_0_1km_m2s2", "scp", "stp"} <= set(s)
    assert out["interpretation"].strip()


@respx.mock
async def test_tool_degrades_when_no_cycle_available():
    respx.get(rap.FILTER_URL).respond(404)
    out = await server.get_environment_trend(OKC_LAT, OKC_LON)
    assert out["degraded"] == ["rap-trend"]
    assert "could not be computed" in out["interpretation"]


async def test_tool_rejects_out_of_bounds_without_network():
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(OutOfBoundsError):
            await server.get_environment_trend(51.5, -0.12)
        assert len(mock.calls) == 0
