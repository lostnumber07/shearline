"""Tests for the historical storm-report tool (Task 2), offline via respx.

Fixture fixtures/iem_lsr_historical_sample.geojson is a real 43-feature single-UTC-day
pull for OKC (lon=-97.52, lat=35.47, radius 50 mi, 2024-05-07): types
{D:20, G:9, H:8, T:3, F:2, R:1}, 3 tornadoes, largest hail 1.75", peak gust 73 mph.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx

from shearline import server
from shearline.bounds import OutOfBoundsError
from shearline.cache import CACHE
from shearline.envelope import DISCLAIMER
from shearline.sources.iem import (
    API,
    fetch_historical_reports,
    interpret_historical,
    validate_historical_date,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OKC_LAT, OKC_LON = 35.47, -97.52
DATE = "2024-05-07"


def load_fixture() -> dict:
    return json.loads((FIXTURES / "iem_lsr_historical_sample.geojson").read_text())


@pytest.fixture(autouse=True)
def clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


def mock_api() -> respx.Route:
    return respx.get(API).respond(200, json=load_fixture())


# --- date validation ---------------------------------------------------------


def test_validate_accepts_past_date():
    assert validate_historical_date("2024-05-07") == "2024-05-07"


def test_validate_rejects_future_date():
    future = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    with pytest.raises(ValueError, match="future"):
        validate_historical_date(future)


@pytest.mark.parametrize("bad", ["May 7 2024", "2024/05/07", "20240507", "2024-13-01", ""])
def test_validate_rejects_bad_format(bad):
    with pytest.raises(ValueError):
        validate_historical_date(bad)


def test_validate_rejects_pre_coverage():
    with pytest.raises(ValueError, match="coverage"):
        validate_historical_date("1999-05-03")


# --- fetch + normalize -------------------------------------------------------


@respx.mock
async def test_fetch_historical_sends_full_day_window():
    route = mock_api()
    await fetch_historical_reports(OKC_LAT, OKC_LON, radius_km=80, date_iso=DATE)
    url = str(route.calls.last.request.url)
    assert "begints=2024-05-07T00%3A00%3A00Z" in url or "begints=2024-05-07T00:00:00Z" in url
    assert "endts=2024-05-07T23%3A59%3A59Z" in url or "endts=2024-05-07T23:59:59Z" in url
    # 80 km -> ~49.7 miles
    assert "radius_miles=49.7" in url


@respx.mock
async def test_fetch_historical_normalizes_and_sorts():
    mock_api()
    reports = await fetch_historical_reports(OKC_LAT, OKC_LON, radius_km=80, date_iso=DATE)
    assert len(reports) == 43
    # newest-first
    times = [r["time_utc"] for r in reports]
    assert times == sorted(times, reverse=True)
    # a hail report carries inch units; a tornado carries no magnitude
    hail = [r for r in reports if r["category"] == "hail"]
    assert hail and all(h["magnitude_units"] == "inches" for h in hail if h["magnitude"] is not None)
    tor = [r for r in reports if r["category"] == "tornado"]
    assert len(tor) == 3 and all(t["magnitude"] is None for t in tor)
    # distance/bearing present
    assert all("distance_km" in r and "direction" in r for r in reports)


# --- interpretation ----------------------------------------------------------


@respx.mock
async def test_interpret_historical_mentions_date_counts_and_provenance():
    mock_api()
    reports = await fetch_historical_reports(OKC_LAT, OKC_LON, radius_km=80, date_iso=DATE)
    text = interpret_historical(reports, 80, DATE)
    assert DATE in text
    assert "tornado" in text.lower()
    assert "Iowa Environmental Mesonet" in text  # provenance stated
    assert '1.75"' in text  # largest hail


def test_interpret_historical_empty_is_not_an_error():
    text = interpret_historical([], 80, "2024-05-07")
    assert "No Local Storm Reports" in text
    assert "2024-05-07" in text


def test_interpret_historical_pre_2008_adds_sparse_caveat():
    text = interpret_historical([], 80, "2006-04-01")
    assert "sparse" in text.lower()


# --- tool: envelope, bounds, clamps ------------------------------------------


@respx.mock
async def test_tool_returns_envelope_and_counts():
    mock_api()
    out = await server.get_historical_storm_reports(OKC_LAT, OKC_LON, date=DATE, radius_km=80)
    assert set(out) == {"schema_version", "data", "interpretation", "degraded", "disclaimer"}
    assert out["disclaimer"] == DISCLAIMER
    assert out["degraded"] == []
    assert out["data"]["date"] == DATE
    assert out["data"]["counts"]["tornado"] == 3
    assert out["data"]["counts"]["hail"] == 8


async def test_tool_rejects_out_of_bounds_without_network():
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(OutOfBoundsError):
            await server.get_historical_storm_reports(51.5, -0.12, date=DATE)
        assert len(mock.calls) == 0


async def test_tool_rejects_future_date_without_network():
    future = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="future"):
            await server.get_historical_storm_reports(OKC_LAT, OKC_LON, date=future)
        assert len(mock.calls) == 0


@respx.mock
async def test_tool_clamps_radius():
    route = mock_api()
    await server.get_historical_storm_reports(OKC_LAT, OKC_LON, date=DATE, radius_km=9999)
    url = str(route.calls.last.request.url)
    # clamp ceiling is 200 km -> ~124.3 miles, well under the IEM <1000 bound
    assert "radius_miles=124.3" in url
