"""Tests for RAP grib decoding (_decode_profile_sync) and the derived
environment (compute_environment / interpret_environment).

The decode tests run against the real recorded subset in
fixtures/rap_subset_moore.grib2 (Moore, OK point 35.339,-97.487).
No HTTP is involved anywhere in this file — the decode and derive
functions are pure/synchronous — so everything passes offline.
"""

import itertools
import math
import re
from pathlib import Path

import pytest

from shearline.cache import CACHE
from shearline.derive.environment import compute_environment, interpret_environment
from shearline.sources.rap import _decode_profile_sync

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

MOORE_LAT = 35.339
MOORE_LON = -97.487


@pytest.fixture(autouse=True)
def clear_cache():
    """CACHE is a module-level singleton; keep state from leaking between tests."""
    CACHE.clear()
    yield
    CACHE.clear()


@pytest.fixture(scope="module")
def profile():
    """Decode the Moore grib subset once for the whole module (slow: ~2-4 s)."""
    grib = (FIXTURES / "rap_subset_moore.grib2").read_bytes()
    return _decode_profile_sync(grib, MOORE_LAT, MOORE_LON)


@pytest.fixture(scope="module")
def environment(profile):
    """Derived parameters from the decoded profile (also CPU-heavy; compute once)."""
    return compute_environment(profile)


def sentence_count(text: str) -> int:
    # Split on sentence-ending periods (period followed by whitespace or end of
    # string) — decimals like "SCP 0.5" have no trailing space so they don't split.
    return len([s for s in re.split(r"\.(?:\s+|$)", text) if s.strip()])


# --- _decode_profile_sync on the real Moore fixture ---


def test_decode_nearest_gridpoint(profile):
    gp = profile["grid_point"]
    assert gp["lat"] == pytest.approx(35.341, abs=0.005)
    assert gp["lon"] == pytest.approx(-97.497, abs=0.005)
    assert 0.0 <= gp["distance_km"] < 2.0


def test_decode_pressure_levels(profile):
    levels = profile["pressure_hpa"]
    assert len(levels) == 37
    assert levels[0] == 1000.0
    assert levels[-1] == 100.0
    # bottom-up ordering, strictly decreasing in 25-hPa steps
    assert all(a - b == 25.0 for a, b in itertools.pairwise(levels))
    # every column variable is aligned with the level coordinate
    for var in ("temp_k", "rh_pct", "height_gpm", "u_ms", "v_ms"):
        assert len(profile[var]) == 37


def test_decode_surface_and_2m_values(profile):
    assert profile["surface_pressure_pa"] == pytest.approx(96448.0, abs=100.0)
    assert profile["t2m_k"] == pytest.approx(306.78, abs=0.1)


def test_decode_model_reported_srh(profile):
    assert profile["model_reported"]["srh_0_3km_m2s2"] == pytest.approx(211.0, abs=1.0)


def test_decode_cycle_and_valid_time(profile):
    # f00 analysis: valid time equals the cycle time
    assert profile["cycle_utc"] == "2026-06-10T20:00Z"
    assert profile["valid_utc"] == profile["cycle_utc"]


# --- compute_environment regression on the Moore profile ---


def test_environment_thermodynamics_regression(environment):
    th = environment["thermodynamics"]
    assert th["sbcape_jkg"] == pytest.approx(2320, rel=0.10)
    assert th["mlcin_jkg"] == pytest.approx(-234, rel=0.10)
    assert th["lcl_m_agl"] == pytest.approx(1747, rel=0.10)


def test_environment_kinematics_regression(environment):
    kin = environment["kinematics"]
    assert kin["srh_0_3km_m2s2"] == pytest.approx(189, rel=0.10)
    assert kin["bulk_shear_0_6km_kt"] == pytest.approx(33, rel=0.10)


def test_environment_composites_finite_nonnegative(environment):
    comp = environment["composites"]
    for key in ("scp", "stp_fixed_layer"):
        value = comp[key]
        assert isinstance(value, (int, float))
        assert math.isfinite(value)
        assert value >= 0.0


def test_environment_carries_grid_point_and_model(environment):
    assert environment["model"] == "RAP 13-km analysis (f00)"
    assert environment["grid_point"]["lat"] == pytest.approx(35.341, abs=0.005)
    # model_reported values pass through (rounded to 1 decimal)
    assert environment["model_reported"]["srh_0_3km_m2s2"] == pytest.approx(211.0, abs=1.0)


def test_interpret_real_environment_is_2_to_5_sentences(environment):
    text = interpret_environment(environment)
    assert 2 <= sentence_count(text) <= 5


# --- interpret_environment regime branches (synthetic data) ---


def make_env(
    mlcape=0,
    mucape=None,
    shear6=0,
    srh1=0,
    lcl=1500,
    scp=0.0,
    stp_effective=0.0,
    stp_fixed=0.0,
):
    """Minimal synthetic data dict shaped like compute_environment output."""
    return {
        "thermodynamics": {
            "mlcape_jkg": mlcape,
            "mucape_jkg": mucape if mucape is not None else mlcape,
            "lcl_m_agl": lcl,
        },
        "kinematics": {
            "bulk_shear_0_6km_kt": shear6,
            "srh_0_1km_m2s2": srh1,
        },
        "composites": {
            "scp": scp,
            "stp_effective": stp_effective,
            "stp_fixed_layer": stp_fixed,
        },
    }


def test_interpret_stable_regime():
    text = interpret_environment(make_env(mlcape=50, mucape=40, shear6=20))
    assert "stable" in text
    assert "deep convection is not supported" in text
    assert 2 <= sentence_count(text) <= 5


def test_interpret_stable_with_strong_shear_notes_shear():
    text = interpret_environment(make_env(mlcape=0, mucape=0, shear6=55))
    assert "stable" in text
    assert "55 kt" in text
    assert 2 <= sentence_count(text) <= 5


def test_interpret_pulse_regime():
    text = interpret_environment(make_env(mlcape=2500, shear6=15))
    assert "pulse" in text
    assert "2500" in text
    assert 2 <= sentence_count(text) <= 5


def test_interpret_cool_season_regime():
    text = interpret_environment(make_env(mlcape=500, shear6=50))
    assert "cool-season" in text
    assert "low-CAPE/high-shear" in text
    assert 2 <= sentence_count(text) <= 5


def test_interpret_classic_supercell_regime():
    text = interpret_environment(
        make_env(
            mlcape=2500,
            shear6=45,
            srh1=250,
            lcl=900,
            scp=8.0,
            stp_effective=2.1,
        )
    )
    assert "supercell parameter space" in text
    # favorable srh/lcl combination should be called out
    assert "tornado-favorable" in text
    # composites above thresholds get the "underline the threat" sentence
    assert "underline the threat" in text
    assert 2 <= sentence_count(text) <= 5


def test_interpret_handles_none_fields():
    # _round can emit None for NaN inputs; interpretation must still work
    env = make_env(mlcape=1200, shear6=38, scp=0.0, stp_fixed=0.0)
    env["composites"]["stp_effective"] = None
    env["kinematics"]["srh_0_1km_m2s2"] = None
    text = interpret_environment(env)
    assert "supercell parameter space" in text
    assert 2 <= sentence_count(text) <= 5
