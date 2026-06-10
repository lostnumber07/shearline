"""Offline tests for shearline.sources.mrms against recorded MRMS grib2 fixtures.

Ground truth (decoded independently from the fixtures in a scratch script):
- mrms_mesh_sample.grib2.gz: MESH_Max_60min, valid 2026-06-10T21:30Z,
  0.01-degree CONUS grid (3500x7000), lats descending 54.995 -> 20.005,
  lons 230.005 -> 299.995 east. Global max is a single cell of 37.5 mm at
  (31.355, -103.585) in west Texas. Sentinels present: -1 (missing, e.g. the
  ocean off Maine) and -3 (no coverage, e.g. the far-NW Pacific corner).
- mrms_rotationtrackml60min_sample.grib2.gz: RotationTrackML60min, same valid
  time, 0.005-degree grid (7000x14000). Global max raw value 23.0
  (units of 0.001/s) at (50.1075, -94.3975) near the MN/Ontario border.

No HTTP or S3 is touched: only _sample_sync on local fixture bytes plus the
pure functions shape_results/interpret on synthetic dicts.
"""

from pathlib import Path

import pytest

from shearline.cache import CACHE
from shearline.geo import distance_bearing
from shearline.sources.mrms import _sample_sync, interpret, shape_results

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MESH_GZ = (FIXTURES / "mrms_mesh_sample.grib2.gz").read_bytes()
ROT_ML_GZ = (FIXTURES / "mrms_rotationtrackml60min_sample.grib2.gz").read_bytes()

VALID_UTC = "2026-06-10T21:30Z"

# The single 37.5 mm MESH max cell, located by decoding the fixture directly.
MESH_MAX_MM = 37.5
MESH_MAX_LAT, MESH_MAX_LON = 31.355, -103.585

# Sample centers (offset from the max so distance/bearing are non-trivial).
MESH_CENTER = (31.30, -103.50)
ROT_CENTER = (50.05, -94.30)


@pytest.fixture(autouse=True)
def _clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


# --- _sample_sync on the MESH fixture -----------------------------------------


def test_sample_sync_finds_mesh_max():
    out = _sample_sync(MESH_GZ, *MESH_CENTER, radius_km=40.0, min_valid=0.001)

    assert out["max"] == MESH_MAX_MM  # mm, the file's unique global max
    assert out["valid_utc"] == VALID_UTC
    assert out["cells_in_radius"] == 256
    # max_location must point from the sample center to the known max cell
    expected = distance_bearing(*MESH_CENTER, MESH_MAX_LAT, MESH_MAX_LON)
    assert out["max_location"] == expected
    assert expected == {"distance_km": 10.1, "bearing_deg": 307, "direction": "NW"}


def test_sample_sync_quiet_ocean_returns_no_max():
    # Open ocean off Maine: inside the grid, uniformly -1 (missing sentinel).
    out = _sample_sync(MESH_GZ, 43.0, -68.0, radius_km=60.0, min_valid=0.001)

    assert out["max"] is None
    assert out["cells_in_radius"] == 0
    assert out["valid_utc"] == VALID_UTC  # valid time still reported
    assert "max_location" not in out


def test_sample_sync_masks_negative_sentinels():
    # Far-NW Pacific corner of the grid: uniformly -3 (no coverage). The
    # v >= min_valid mask must exclude every cell, never reporting -3 as a max.
    out = _sample_sync(MESH_GZ, 54.5, -129.5, radius_km=60.0, min_valid=0.001)

    assert out["max"] is None
    assert out["cells_in_radius"] == 0
    assert "max_location" not in out


# --- rotation fixture + 0.001/s scaling through shape_results ------------------


def test_rotation_fixture_max_scaled_by_shape_results():
    sample = _sample_sync(ROT_ML_GZ, *ROT_CENTER, radius_km=40.0, min_valid=0.001)

    assert sample["max"] == 23.0  # raw units of 0.001/s
    assert sample["valid_utc"] == VALID_UTC
    assert sample["cells_in_radius"] > 0

    data = shape_results({"rotation_track_ml_60min": sample})
    rot = data["rotation_midlevel"]
    assert rot["max_azimuthal_shear_s1"] == pytest.approx(0.023)  # 23.0 * 0.001
    assert rot["layer"] == "3-6 km AGL"
    assert rot["valid_utc"] == VALID_UTC
    assert rot["max_location"] == sample["max_location"]
    assert rot["cells_in_radius"] == sample["cells_in_radius"]


# --- shape_results unit conversions and None passthrough -----------------------


def test_shape_results_converts_mesh_mm_to_inches():
    loc = {"distance_km": 5.0, "bearing_deg": 90, "direction": "E"}
    data = shape_results(
        {
            "mesh_max_60min": {
                "max": 38.1,
                "valid_utc": VALID_UTC,
                "window": "last 60 minutes",
                "max_location": loc,
                "cells_in_radius": 12,
            }
        }
    )
    hail = data["hail_mesh"]
    assert hail["max_mesh_mm"] == 38.1
    assert hail["max_mesh_in"] == 1.5  # 38.1 / 25.4
    assert hail["valid_utc"] == VALID_UTC
    assert hail["window"] == "last 60 minutes"
    assert hail["max_location"] == loc
    assert hail["cells_in_radius"] == 12


def test_shape_results_omits_missing_products():
    # Products that were never sampled (absent) or failed (None) produce no keys.
    assert shape_results({}) == {}
    assert (
        shape_results(
            {"mesh_max_60min": None, "rotation_track_ml_60min": None, "vil": None}
        )
        == {}
    )


def test_shape_results_passes_none_max_through():
    data = shape_results(
        {
            "mesh_max_60min": {"max": None, "valid_utc": VALID_UTC, "cells_in_radius": 0},
            "rotation_track_ll_60min": {
                "max": None,
                "valid_utc": VALID_UTC,
                "cells_in_radius": 0,
            },
            "composite_reflectivity": {
                "max": None,
                "valid_utc": VALID_UTC,
                "cells_in_radius": 0,
            },
        }
    )
    assert data["hail_mesh"]["max_mesh_mm"] is None
    assert data["hail_mesh"]["max_mesh_in"] is None
    assert data["rotation_lowlevel"]["max_azimuthal_shear_s1"] is None
    assert data["rotation_lowlevel"]["layer"] == "0-2 km AGL"
    assert data["composite_reflectivity"]["max_dbz"] is None


# --- interpret() wording --------------------------------------------------------


def test_interpret_quiet_when_no_reflectivity():
    text = interpret({}, radius_km=50.0)
    assert "quiet" in text
    assert "Radar is quiet within 50 km" in text
    assert "no echoes of consequence" in text


def test_interpret_active_hail_core():
    data = {
        "composite_reflectivity": {
            "max_dbz": 60.0,
            "max_location": {"distance_km": 12.3, "bearing_deg": 45, "direction": "NE"},
        },
        "hail_mesh": {
            "max_mesh_in": 1.5,
            "max_mesh_mm": 38.1,
            "max_location": {"distance_km": 12.3, "bearing_deg": 45, "direction": "NE"},
        },
    }
    text = interpret(data, radius_km=80.0)
    assert "Strong convection is nearby" in text
    assert "a core intense enough for hail" in text  # refl >= 60 wording
    assert 'MESH peaked at 1.5"' in text
    assert "severe-caliber hail" in text  # 1 <= mesh_in < 2


def test_interpret_strong_rotation_is_mesocyclone_caliber():
    data = {"rotation_lowlevel": {"max_azimuthal_shear_s1": 0.009}}
    text = interpret(data, radius_km=50.0)
    assert "strong low-level circulation" in text
    assert "max azimuthal shear 0.009 /s" in text
    assert "mesocyclone-caliber" in text
