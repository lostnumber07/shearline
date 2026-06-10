"""Tests for shearline.sources.nws against recorded 2026-06-10 MO/IA outbreak fixtures."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from shearline.cache import CACHE
from shearline.sources.nws import (
    _parse_float,
    _parse_warning,
    fetch_point_alerts,
    is_warning,
    is_watch,
    parse_storm_motion,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Exact observed eventMotionDescription from the SVR fixture.
SVR_MOTION = "2026-06-10T21:28:00-00:00...storm...240DEG...41KT...40.15,-94.16 39.96,-94.24"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def clear_cache():
    """CACHE is a module-level singleton; keep entries from leaking across tests."""
    CACHE.clear()
    yield
    CACHE.clear()


# --- parse_storm_motion -----------------------------------------------------


def test_parse_storm_motion_observed_format():
    motion = parse_storm_motion(SVR_MOTION)
    assert motion is not None
    assert motion["from_deg"] == 240
    assert motion["toward_deg"] == 60
    assert motion["toward_compass"] == "ENE"
    assert motion["speed_kt"] == 41
    # Trailing centroid pairs are lat-first.
    assert motion["storm_cells"] == [
        {"lat": 40.15, "lon": -94.16},
        {"lat": 39.96, "lon": -94.24},
    ]


def test_parse_storm_motion_none_and_garbage():
    assert parse_storm_motion(None) is None
    assert parse_storm_motion("") is None
    # Human-readable text with no DEG/KT/coords machine encoding.
    assert parse_storm_motion("moving northeast at 45 mph") is None


# --- _parse_warning: SVR fixture ---------------------------------------------


def test_parse_warning_svr_inside_point():
    feature = load_fixture("nws_alert_svr_sample.json")
    # Near the planar centroid of the warning polygon (north-central MO).
    warning = _parse_warning(feature, 40.10, -93.97)

    assert warning["id"] == (
        "urn:oid:2.49.0.1.840.0.f65dcc1b150bd22fd4871aaa1c6574e9f39bf6de.001.1"
    )
    assert warning["event"] == "Severe Thunderstorm Warning"
    assert warning["severity"] == "Severe"
    assert warning["certainty"] == "Observed"
    assert warning["message_type"] == "Alert"
    assert warning["sender"] == "NWS Kansas City/Pleasant Hill MO"
    assert warning["effective_utc"] == "2026-06-10T16:28:00-05:00"
    assert warning["expires_utc"] == "2026-06-10T17:30:00-05:00"

    assert warning["point_inside"] is True
    assert warning["distance_km"] == 0.0
    assert warning["bearing_deg"] is None
    assert warning["direction"] is None

    # Exact IBW tag values recorded in the fixture.
    ibw = warning["ibw_tags"]
    assert ibw["hail_threat"] == "RADAR INDICATED"
    assert ibw["wind_threat"] == "RADAR INDICATED"
    assert ibw["thunderstorm_damage_threat"] == "CONSIDERABLE"
    assert ibw["tornado_detection"] == "POSSIBLE"
    assert ibw["tornado_damage_threat"] is None  # absent on this SVR
    assert ibw["max_hail_size_in"] == 1.75
    assert ibw["max_wind_gust_mph"] == 60.0

    motion = warning["storm_motion"]
    assert motion["from_deg"] == 240
    assert motion["speed_kt"] == 41
    assert len(motion["storm_cells"]) == 2

    # Polygon passes through untouched, lon-first as in GeoJSON.
    assert warning["polygon_lonlat"] == feature["geometry"]["coordinates"]
    assert warning["polygon_lonlat"][0][0] == [-93.6, 40.41]


def test_parse_warning_svr_outside_point_distance_bearing():
    feature = load_fixture("nws_alert_svr_sample.json")
    # Kansas City area: south-southwest of the polygon's SW vertex (-94.36, 39.89).
    warning = _parse_warning(feature, 39.10, -94.58)

    assert warning["point_inside"] is False
    assert warning["distance_km"] == 89.8
    assert warning["bearing_deg"] == 12
    assert warning["direction"] == "NNE"


# --- _parse_warning: TOR fixture ---------------------------------------------


def test_parse_warning_tor_inside_point():
    feature = load_fixture("nws_alert_tor_sample.json")
    # Inside the Putnam County polygon (lon -93.05..-92.68, lat 40.46..40.59).
    warning = _parse_warning(feature, 40.52, -92.90)

    assert warning["event"] == "Tornado Warning"
    assert warning["severity"] == "Extreme"
    assert warning["message_type"] == "Update"
    assert warning["area_desc"] == "Putnam, MO"
    assert warning["ends_utc"] == "2026-06-10T17:00:00-05:00"

    assert warning["point_inside"] is True
    assert warning["distance_km"] == 0.0

    # Exact IBW tag values recorded in the fixture (PDS confirmed tornado).
    ibw = warning["ibw_tags"]
    assert ibw["tornado_detection"] == "OBSERVED"
    assert ibw["tornado_damage_threat"] == "CONSIDERABLE"
    assert ibw["thunderstorm_damage_threat"] is None  # absent on TOR statements
    assert ibw["hail_threat"] is None
    assert ibw["wind_threat"] is None
    assert ibw["max_hail_size_in"] == 1.5
    assert ibw["max_wind_gust_mph"] is None

    motion = warning["storm_motion"]
    assert motion["from_deg"] == 251
    assert motion["toward_deg"] == 71
    assert motion["toward_compass"] == "ENE"
    assert motion["speed_kt"] == 25
    assert motion["storm_cells"] == [{"lat": 40.52, "lon": -92.98}]


def test_parse_warning_tor_outside_point_distance_bearing():
    feature = load_fixture("nws_alert_tor_sample.json")
    # Due south of the polygon's flat southern edge at lat 40.46.
    warning = _parse_warning(feature, 40.30, -92.90)

    assert warning["point_inside"] is False
    assert warning["distance_km"] == 17.8
    assert warning["bearing_deg"] == 0
    assert warning["direction"] == "N"


# --- _parse_float ------------------------------------------------------------


def test_parse_float_values():
    assert _parse_float("1.75") == 1.75
    assert _parse_float("Up to .75") == 0.75
    assert _parse_float("0.00") == 0.0
    assert _parse_float(None) is None


# --- is_warning / is_watch ---------------------------------------------------


def test_is_warning_on_fixtures_and_cancel():
    svr = load_fixture("nws_alert_svr_sample.json")
    tor = load_fixture("nws_alert_tor_sample.json")
    assert is_warning(svr) is True  # messageType Alert
    assert is_warning(tor) is True  # messageType Update
    cancel = {"properties": {"messageType": "Cancel", "event": "Tornado Warning"}}
    assert is_warning(cancel) is False
    watch = {"properties": {"messageType": "Alert", "event": "Tornado Watch"}}
    assert is_warning(watch) is False
    assert is_warning({"properties": {}}) is False


def test_is_watch():
    watch = {"properties": {"event": "Tornado Watch"}}
    assert is_watch(watch) is True
    svr = load_fixture("nws_alert_svr_sample.json")
    assert is_watch(svr) is False
    assert is_watch({"properties": {}}) is False


# --- fetch_point_alerts ------------------------------------------------------


@respx.mock
async def test_fetch_point_alerts_moore_empty():
    payload = load_fixture("nws_alerts_point_moore.json")
    route = respx.get("https://api.weather.gov/alerts/active").respond(
        200, json=payload
    )

    features = await fetch_point_alerts(35.339, -97.487)

    assert features == []
    assert route.called
    request = route.calls.last.request
    # Coordinates formatted to 4 decimal places.
    assert (
        str(request.url)
        == "https://api.weather.gov/alerts/active?point=35.3390,-97.4870"
    )
    assert request.headers["Accept"] == "application/geo+json"


@respx.mock
async def test_fetch_point_alerts_uses_cache_within_ttl():
    payload = load_fixture("nws_alerts_point_moore.json")
    route = respx.get("https://api.weather.gov/alerts/active").respond(
        200, json=payload
    )

    first = await fetch_point_alerts(35.339, -97.487)
    second = await fetch_point_alerts(35.339, -97.487)

    assert first == second == []
    assert route.call_count == 1  # second call served from CACHE


@respx.mock
async def test_fetch_point_alerts_raises_on_http_error():
    respx.get("https://api.weather.gov/alerts/active").respond(403)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_point_alerts(35.339, -97.487)
