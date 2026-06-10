"""Tests for shearline.sources.iem against the recorded 2026-06-10 IA/WI/IL derecho LSRs.

Fixture hand-count (fixtures/iem_lsr_bypoint_sample.geojson): 52 features,
30 type "G" (TSTM WND GST, mph) + 22 type "D" (TSTM WND DMG, no magnitude).
Oldest valid 2026-06-10T16:19:00Z, newest 2026-06-10T19:55:00Z, peak gust 88 mph.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import respx

from shearline.cache import CACHE
from shearline.sources.iem import API, count_reports, fetch_reports, interpret

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Center on the Dubuque Regional Arpt report so one entry sits at distance 0.
CENTER_LAT, CENTER_LON = 42.4, -90.71

COMPASS_16 = {
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
}


def load_fixture() -> dict:
    return json.loads((FIXTURES / "iem_lsr_bypoint_sample.geojson").read_text())


@pytest.fixture(autouse=True)
def clear_cache():
    """CACHE is a module-level singleton; keep entries from leaking across tests."""
    CACHE.clear()
    yield
    CACHE.clear()


def mock_lsr_api() -> respx.Route:
    return respx.get(API).respond(200, json=load_fixture())


# --- fetch_reports: normalization + ordering ----------------------------------


@respx.mock
async def test_fetch_reports_normalized_and_sorted_newest_first():
    mock_lsr_api()

    reports = await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    assert len(reports) == 52
    times = [r["time_utc"] for r in reports]
    assert times == sorted(times, reverse=True)

    # Newest entry: the 19:55Z wind-damage report near Hampshire, IL (type D).
    newest = reports[0]
    assert newest["time_utc"] == "2026-06-10T19:55:00Z"
    assert newest["category"] == "thunderstorm_wind_damage"
    assert newest["type_text"] == "TSTM WND DMG"
    assert newest["city"] == "2 WNW Hampshire"
    assert newest["state"] == "IL"
    assert newest["magnitude"] is None
    assert newest["magnitude_units"] is None  # no magnitude -> units suppressed

    # Oldest entry: the 16:19Z 62 mph gust near Williamstown, IA (type G).
    oldest = reports[-1]
    assert oldest["time_utc"] == "2026-06-10T16:19:00Z"
    assert oldest["category"] == "thunderstorm_wind_gust"
    assert oldest["type_text"] == "TSTM WND GST"
    assert oldest["magnitude"] == 62.0
    assert oldest["magnitude_units"] == "mph"
    assert oldest["city"] == "2 SW Williamstown"
    assert oldest["county"] == "Johnson"
    assert oldest["state"] == "IA"
    assert oldest["source"] == "Trained Spotter"

    # Only G and D codes exist in this fixture; check the mapping holds for all.
    for r in reports:
        if r["category"] == "thunderstorm_wind_gust":
            assert r["magnitude"] is not None
            assert r["magnitude_units"] == "mph"
        else:
            assert r["category"] == "thunderstorm_wind_damage"
            assert r["magnitude"] is None
            assert r["magnitude_units"] is None


# --- fetch_reports: request URL (begints+endts trap, radius conversion) -------


@respx.mock
async def test_fetch_reports_request_sends_both_begints_and_endts():
    route = mock_lsr_api()

    await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    url = str(route.calls.last.request.url)
    # Upstream trap: a lone begints is silently ignored; BOTH must be present.
    assert "begints=" in url
    assert "endts=" in url

    params = route.calls.last.request.url.params
    begin = datetime.strptime(params["begints"], "%Y-%m-%dT%H:%M:%SZ")
    end = datetime.strptime(params["endts"], "%Y-%m-%dT%H:%M:%SZ")
    # Window = hours back from now, plus a 10-minute forward pad.
    assert end - begin == timedelta(hours=6, minutes=10)


@respx.mock
async def test_fetch_reports_request_radius_and_point_params():
    route = mock_lsr_api()

    await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    params = route.calls.last.request.url.params
    # 80 km / 1.609344 km-per-mile = 49.7098... -> formatted as 49.7 miles.
    assert params["radius_miles"] == "49.7"
    assert params["lat"] == "42.4000"
    assert params["lon"] == "-90.7100"


# --- fetch_reports: distance/bearing enrichment --------------------------------


@respx.mock
async def test_fetch_reports_distance_bearing_present_and_plausible():
    mock_lsr_api()

    reports = await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    for r in reports:
        assert 0.0 <= r["distance_km"] <= 200.0  # farthest fixture report ~182 km
        assert 0 <= r["bearing_deg"] <= 360
        assert r["direction"] in COMPASS_16

    by_city = {r["city"]: r for r in reports}

    # Center sits exactly on the Dubuque Regional Arpt report.
    arpt = by_city["Dubuque Regional Arpt"]
    assert arpt["distance_km"] == 0.0

    # Galena, IL (42.41, -90.43) is ~23 km nearly due east of the center.
    galena = by_city["Galena"]
    assert galena["distance_km"] == pytest.approx(23.0, abs=0.5)
    assert 80 <= galena["bearing_deg"] <= 100
    assert galena["direction"] == "E"

    # Williamstown, IA (41.54, -91.75) is ~129 km to the southwest.
    williamstown = by_city["2 SW Williamstown"]
    assert williamstown["distance_km"] == pytest.approx(128.6, abs=1.0)
    assert williamstown["direction"] == "SW"


# --- count_reports --------------------------------------------------------------


@respx.mock
async def test_count_reports_fixture_buckets():
    mock_lsr_api()
    reports = await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    # Hand-counted from the fixture: 30 "G" gusts + 22 "D" damage = 52 wind.
    assert count_reports(reports) == {
        "tornado": 0,
        "hail": 0,
        "wind": 52,
        "flood": 0,
        "other": 0,
    }


def test_count_reports_synthetic_category_buckets():
    reports = [
        {"category": "tornado"},
        {"category": "waterspout"},  # bucketed with tornado
        {"category": "hail"},
        {"category": "thunderstorm_wind_gust"},
        {"category": "non_thunderstorm_wind_damage"},
        {"category": "flash_flood"},
        {"category": "heavy_rain"},  # bucketed with flood
        {"category": "funnel_cloud"},  # other
        {"category": "dust"},  # other
    ]
    assert count_reports(reports) == {
        "tornado": 2,
        "hail": 1,
        "wind": 2,
        "flood": 2,
        "other": 2,
    }


# --- interpret -------------------------------------------------------------------


def test_interpret_empty_mentions_radius_and_no_reports():
    text = interpret([], radius_km=80, hours=6)
    assert "No local storm reports" in text
    assert "80 km" in text
    assert "6" in text  # the lookback window in hours


@respx.mock
async def test_interpret_wind_fixture_summary():
    mock_lsr_api()
    reports = await fetch_reports(CENTER_LAT, CENTER_LON, radius_km=80, hours=6)

    text = interpret(reports, radius_km=80, hours=6)

    assert "52 local storm reports within 80 km" in text
    assert "52 wind report(s)" in text
    assert "peak gust 88 mph" in text  # strongest G report in the fixture
    # 17 reports lie within radius/2 (40 km) of the center -> escalation line.
    assert "Multiple nearby reports" in text
    # No tornadoes in this fixture, so no tornadic-activity escalation.
    assert "tornado" not in text.lower()


def test_interpret_tornado_reports_mentions_count_and_danger():
    reports = [
        {
            "category": "tornado",
            "magnitude": None,
            "time_utc": "2026-06-10T19:30:00Z",
            "city": "Anamosa",
            "state": "IA",
            "distance_km": 12.0,
        },
        {
            "category": "waterspout",
            "magnitude": None,
            "time_utc": "2026-06-10T18:45:00Z",
            "city": "Bellevue",
            "state": "IA",
            "distance_km": 35.0,
        },
    ]
    text = interpret(reports, radius_km=80, hours=6)

    assert "2 tornado/waterspout report(s)" in text
    # "most recent" is the first tornado entry in the newest-first list.
    assert "Anamosa, IA at 2026-06-10T19:30:00Z" in text
    assert "active, dangerous situation" in text
