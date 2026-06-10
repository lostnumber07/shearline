"""Tests for the offline parts of shearline.sources.nexrad.

Covers load_stations() parsing of the bundled NCEI HOMR fixed-width table,
nearest_sites() geometry, and the VCP meaning map. No network, no boto3 —
nothing here touches S3 or HTTP.
"""

import pytest

from shearline.sources.nexrad import VCP_MEANINGS, load_stations, nearest_sites

# --- load_stations: parsing and filtering -------------------------------------


def test_load_stations_parses_full_wsr88d_roster():
    stations = load_stations()
    # The bundled table yields 162 WSR-88D sites after filtering (spec: >150).
    assert len(stations) == 161  # excludes TDWRs and ROC/NSSL test radars (KCRI, KOUN)
    ids = [s["id"] for s in stations]
    assert len(ids) == len(set(ids)), "station ids must be unique"


def test_load_stations_excludes_tdwrs_but_keeps_tjua():
    ids = {s["id"] for s in load_stations()}
    # The raw table carries 47 TDWR rows (STNTYPE 'TDWR'); none may survive.
    assert "TFLL" not in ids  # Ft Lauderdale TDWR
    assert "TOKC" not in ids  # Oklahoma City TDWR
    assert "TSJU" not in ids  # San Juan TDWR
    # TJUA is a T-prefixed WSR-88D (STNTYPE NEXRAD) — classification must come
    # from STNTYPE, never the ICAO prefix.
    assert "TJUA" in ids
    assert [i for i in ids if i.startswith("T")] == ["TJUA"]


def test_load_stations_excludes_roc_test_radars():
    stations = load_stations()
    ids = {s["id"] for s in stations}
    assert "KCRI" not in ids  # "ROC FAA REDUNDANT RDA 1" next door to KTLX
    for s in stations:
        upper = s["name"].upper()
        assert "RDA" not in upper
        assert not upper.startswith("ROC ")


def test_ktlx_fields_parsed_from_fixed_width_columns():
    stations = {s["id"]: s for s in load_stations()}
    ktlx = stations["KTLX"]
    assert ktlx["lat"] == pytest.approx(35.333, abs=0.001)
    assert ktlx["lon"] == pytest.approx(-97.278, abs=0.001)
    assert ktlx["state"] == "OK"
    assert ktlx["name"] == "Oklahoma City"
    assert ktlx["elev_ft"] == 1278.0


def test_station_coordinates_match_network_geography():
    stations = load_stations()
    # Five overseas 88Ds sit outside the -180..-60 longitude band: the Azores,
    # Guam, two Korea sites, and Okinawa. Everything else (CONUS + AK + HI +
    # PR) fits lat 17..72, lon -180..-60.
    overseas = {s["id"] for s in stations if not (-180.0 <= s["lon"] <= -60.0)}
    assert overseas == {"LPLA", "PGUA", "RKJK", "RKSG", "RODN"}
    for s in stations:
        if s["id"] in overseas:
            continue
        assert 17.0 <= s["lat"] <= 72.0, s["id"]
        assert -180.0 <= s["lon"] <= -60.0, s["id"]


def test_load_stations_result_is_cached():
    # lru_cache: repeat calls return the identical list object.
    assert load_stations() is load_stations()


# --- nearest_sites --------------------------------------------------------------


def test_nearest_sites_moore_ok_includes_ktlx_at_19km_east():
    sites = nearest_sites(35.339, -97.487)  # Moore, OK
    assert len(sites) == 3  # default n
    for s in sites:
        assert {"distance_km", "bearing_deg", "direction"} <= set(s)
        assert {"id", "name", "state", "lat", "lon", "elev_ft"} <= set(s)
    # Sorted nearest-first.
    distances = [s["distance_km"] for s in sites]
    assert distances == sorted(distances)
    by_id = {s["id"]: s for s in sites}
    assert "KTLX" in by_id
    ktlx = by_id["KTLX"]
    assert ktlx["distance_km"] == pytest.approx(19.0, abs=0.5)
    assert ktlx["bearing_deg"] == 92
    assert ktlx["direction"] == "E"


def test_nearest_sites_at_radar_location_is_that_radar():
    # Query from KABR's exact coordinates: it must come back first at 0 km.
    sites = nearest_sites(45.455833, -98.413333, n=1)
    assert len(sites) == 1
    assert sites[0]["id"] == "KABR"
    assert sites[0]["state"] == "SD"
    assert sites[0]["distance_km"] == 0.0


# --- VCP meanings ---------------------------------------------------------------


def test_vcp_meanings_cover_key_patterns():
    assert VCP_MEANINGS[12] == "severe-weather precipitation mode (fast low-level updates)"
    assert VCP_MEANINGS[212] == VCP_MEANINGS[12]
    assert VCP_MEANINGS[35] == "clear-air mode"
