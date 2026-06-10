"""Tests for shearline.sources.spc against recorded SPC outlook fixtures.

Point coordinates below were derived by intersecting the fixture polygons
offline (shapely representative points of the relevant wedding-cake rings),
so each assertion pins a real value from the recorded data.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from shearline.cache import CACHE
from shearline.sources import spc

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


@pytest.fixture(scope="module")
def day1_cat() -> dict:
    return load("spc_day1_cat.geojson")


@pytest.fixture(scope="module")
def day1_cat_archive() -> dict:
    return load("spc_day1_cat_archive_20250315_mdt_high.geojson")


@pytest.fixture(scope="module")
def day1_torn() -> dict:
    return load("spc_day1_torn.geojson")


@pytest.fixture(scope="module")
def day1_cigtorn() -> dict:
    return load("spc_day1_cigtorn.geojson")


@pytest.fixture(scope="module")
def sigtorn_populated() -> dict:
    return load("spc_day1_sigtorn_archive_20250315_populated.geojson")


@pytest.fixture(scope="module")
def sigtorn_empty() -> dict:
    return load("spc_day1_sigtorn_empty_stale.geojson")


@pytest.fixture(scope="module")
def day2_relic() -> dict:
    return load("spc_day2_prob_stale_2020_relic.geojson")


@pytest.fixture(scope="module")
def day2_cat() -> dict:
    return load("spc_day2_cat.geojson")


# --- categorical_at_point ---------------------------------------------------


def test_categorical_max_dn_wins_in_nested_polygons(day1_cat):
    # (42.47, -89.857) sits inside the ENH polygon; wedding-cake nesting means
    # it is also inside SLGT/MRGL/TSTM — the max DN (5 = ENH) must win.
    result = spc.categorical_at_point(day1_cat, 42.47, -89.857)
    assert result["dn"] == 5
    assert result["label"] == "ENH"
    assert result["description"] == spc.CAT_DESCRIPTIONS["ENH"]


def test_categorical_ocean_point_is_none(day1_cat):
    # Mid-Atlantic, far outside every outlook polygon.
    result = spc.categorical_at_point(day1_cat, 30.0, -50.0)
    assert result["dn"] is None
    assert result["label"] is None
    assert result["description"] == "No thunderstorms forecast"


def test_categorical_archive_dn8_maps_to_high(day1_cat_archive):
    # Inside the 2025-03-15 HIGH polygon over west-central Alabama.
    result = spc.categorical_at_point(day1_cat_archive, 32.715, -88.178)
    assert result["dn"] == 8
    assert result["label"] == "HIGH"
    assert result["description"] == spc.CAT_DESCRIPTIONS["HIGH"]


def test_categorical_archive_dn6_maps_to_mdt(day1_cat_archive):
    # Inside the MDT ring but outside the nested HIGH polygon.
    result = spc.categorical_at_point(day1_cat_archive, 32.96, -85.192)
    assert result["dn"] == 6
    assert result["label"] == "MDT"
    assert result["description"] == spc.CAT_DESCRIPTIONS["MDT"]


# --- probability_at_point ---------------------------------------------------


def test_probability_max_contour_with_cig(day1_torn):
    # Inside the 10% tornado contour AND the CIG1 polygon.
    result = spc.probability_at_point(day1_torn, 40.71, -92.683)
    assert result["probability_pct"] == 10
    assert result["conditional_intensity"] == "CIG1"
    assert result["significant"] is False


def test_probability_two_percent_ring_only(day1_torn):
    # Inside the 2% contour only (outside 0.05 and outside CIG1): the genuine
    # 0.02 LABEL yields 2%, and no CIG is reported.
    result = spc.probability_at_point(day1_torn, 43.645, -93.105)
    assert result["probability_pct"] == 2
    assert result["conditional_intensity"] is None
    assert result["significant"] is False


def test_probability_cig_dn2_does_not_pollute_probability(day1_torn):
    # This point is inside CIG1 (whose DN is 2, same as the 2% contour) and
    # inside the 5% contour but outside 10%. Probability must come from the
    # fraction LABELs (5%), never from the CIG feature's DN.
    result = spc.probability_at_point(day1_torn, 42.17, -90.078)
    assert result["probability_pct"] == 5
    assert result["conditional_intensity"] == "CIG1"


def test_probability_ocean_point_all_empty(day1_torn):
    result = spc.probability_at_point(day1_torn, 30.0, -50.0)
    assert result == {"probability_pct": None, "conditional_intensity": None, "significant": False}


def test_probability_cig_only_layer(day1_cigtorn):
    # The dedicated cigtorn layer has only the CIG1 feature: conditional
    # intensity is reported with no probability contour.
    result = spc.probability_at_point(day1_cigtorn, 42.17, -90.078)
    assert result["probability_pct"] is None
    assert result["conditional_intensity"] == "CIG1"
    assert result["significant"] is False


def test_probability_sign_label_sets_significant(sigtorn_populated):
    # Archived sig-tornado layer: LABEL 'SIGN' marks significant severe.
    result = spc.probability_at_point(sigtorn_populated, 32.655, -87.368)
    assert result["significant"] is True
    assert result["probability_pct"] is None
    assert result["conditional_intensity"] is None


# --- empty-layer handling ---------------------------------------------------


def test_empty_layer_categorical_no_crash(sigtorn_empty):
    result = spc.categorical_at_point(sigtorn_empty, 35.0, -97.0)
    assert result["dn"] is None
    assert result["label"] is None


def test_empty_layer_probability_no_crash(sigtorn_empty):
    result = spc.probability_at_point(sigtorn_empty, 35.0, -97.0)
    assert result == {"probability_pct": None, "conditional_intensity": None, "significant": False}


# --- staleness --------------------------------------------------------------


def test_relic_layer_is_stale_against_fresh_reference(day2_relic, day2_cat):
    # The 2020 relic carries VALID 202001311200; the fresh day-2 categorical
    # layer says 202606111200 — mismatch means the relic must be discarded.
    assert spc.layer_valid(day2_relic) == "202001311200"
    fresh_ref = spc.layer_valid(day2_cat)
    assert fresh_ref == "202606111200"
    assert spc.is_stale(day2_relic, fresh_ref) is True


def test_matching_valid_is_not_stale(day1_torn, day1_cat):
    assert spc.is_stale(day1_torn, spc.layer_valid(day1_cat)) is False


def test_is_stale_with_no_reference_is_false(day2_relic):
    assert spc.is_stale(day2_relic, None) is False


def test_empty_stale_layer_still_exposes_its_valid(sigtorn_empty):
    # Even the empty placeholder feature carries timestamps, so the frozen
    # sig* file is detectable as stale against a fresh categorical VALID.
    assert spc.layer_valid(sigtorn_empty) == "202603031300"
    assert spc.is_stale(sigtorn_empty, "202606102000") is True


# --- layer_times ------------------------------------------------------------


def test_layer_times_extraction(day1_cat):
    times = spc.layer_times(day1_cat)
    assert times == {
        "valid_utc": "202606102000",
        "expire_utc": "202606111200",
        "issue_utc": "202606101959",
    }


def test_layer_times_no_features_all_none():
    assert spc.layer_times({"features": []}) == {
        "valid_utc": None,
        "expire_utc": None,
        "issue_utc": None,
    }


# --- DN map invariants ------------------------------------------------------


def test_cat_by_dn_skips_7_and_maps_8_to_high():
    assert 7 not in spc.CAT_BY_DN
    assert spc.CAT_BY_DN[8] == "HIGH"


# --- fetch_layer (HTTP, mocked) ----------------------------------------------


@respx.mock
async def test_fetch_layer_requests_spc_url_and_caches(day1_cat):
    route = respx.get("https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson").mock(
        return_value=httpx.Response(200, json=day1_cat)
    )
    layer = await spc.fetch_layer("day1otlk_cat")
    assert spc.layer_valid(layer) == "202606102000"
    assert spc.categorical_at_point(layer, 42.47, -89.857)["label"] == "ENH"

    # Second call within TTL_OUTLOOK must be served from CACHE, not upstream.
    await spc.fetch_layer("day1otlk_cat")
    assert route.call_count == 1
