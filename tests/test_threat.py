"""Tests for shearline.derive.threat.build_threat_brief.

build_threat_brief is pure (no I/O), so every scenario here is synthetic:
we hand it envelope-shaped dicts ({"data": {...}}) and assert on the exact
level cascade, threat_logic wording, hazard ranking, attention window,
nearest storm signature, and interpretation string.
"""

import pytest

from shearline.cache import CACHE
from shearline.derive.threat import build_threat_brief

LAT, LON = 35.3395, -97.4867  # Moore, OK


@pytest.fixture(autouse=True)
def _clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


def _env(**data):
    """Wrap a payload in the envelope shape the tool layer passes around."""
    return {"data": data}


def brief(warnings=None, outlook=None, environment=None, mrms=None, reports=None):
    return build_threat_brief(LAT, LON, warnings, outlook, environment, mrms, reports)


def tor_warning(inside=True, ibw=None, expires=None, dist=None, direction=None):
    warn = {"event": "Tornado Warning", "point_inside": inside}
    if ibw is not None:
        warn["ibw_tags"] = ibw
    if expires is not None:
        warn["expires_utc"] = expires
    if dist is not None:
        warn["distance_km"] = dist
    if direction is not None:
        warn["direction"] = direction
    return warn


def svr_warning(inside=True, ibw=None, expires=None):
    warn = {"event": "Severe Thunderstorm Warning", "point_inside": inside}
    if ibw is not None:
        warn["ibw_tags"] = ibw
    if expires is not None:
        warn["expires_utc"] = expires
    return warn


# ---- Quiet / outlook-only cascade ----


def test_all_none_inputs_is_level_none_with_fallback_logic():
    data, interp = brief()
    assert data["threat_level"] == "none"
    assert len(data["threat_logic"]) == 1
    assert data["threat_logic"][0].startswith("No active warnings")
    assert "no SPC outlook risk area" in data["threat_logic"][0]
    # Everything downstream of the missing envelopes should be empty/None.
    assert data["point"] == {"lat": LAT, "lon": LON}
    assert data["outlook_summary"] is None
    assert data["environment_summary"] is None
    assert data["nearest_storm_signature"] is None
    assert data["warnings_summary"]["active_at_point"] == 0
    assert data["warnings_summary"]["active_within_radius"] == 0
    assert all(h["score"] == 0 and h["level"] == "none" for h in data["hazards_ranked"])
    assert "Overall threat level: NONE." in interp


def test_tstm_categorical_only_is_none_with_general_storms_logic():
    data, _ = brief(outlook=_env(categorical={"label": "TSTM"}))
    assert data["threat_level"] == "none"
    assert "General (non-severe) thunderstorms" in data["threat_logic"][0]
    assert data["outlook_summary"]["categorical"] == "TSTM"


def test_mrgl_outlook_is_marginal():
    data, _ = brief(outlook=_env(categorical={"label": "MRGL"}))
    assert data["threat_level"] == "marginal"
    assert "SPC Marginal risk (1/5)" in data["threat_logic"][0]


def test_enh_outlook_is_elevated():
    data, _ = brief(outlook=_env(categorical={"label": "ENH"}))
    assert data["threat_level"] == "elevated"
    assert "SPC Enhanced risk (3/5)" in data["threat_logic"][0]


def test_mdt_quiet_is_elevated():
    data, _ = brief(outlook=_env(categorical={"label": "MDT"}))
    assert data["threat_level"] == "elevated"
    assert "SPC Moderate risk (4/5)." in data["threat_logic"][0]
    assert "storms already active" not in data["threat_logic"][0]


def test_mdt_with_active_storms_is_significant():
    data, _ = brief(
        outlook=_env(categorical={"label": "MDT"}),
        mrms=_env(composite_reflectivity={"max_dbz": 55}),
    )
    assert data["threat_level"] == "significant"
    assert "SPC Moderate risk (4/5) with storms already active" in data["threat_logic"][0]


def test_high_risk_is_significant_even_when_quiet():
    data, _ = brief(outlook=_env(categorical={"label": "HIGH"}))
    assert data["threat_level"] == "significant"
    assert "SPC High risk (5/5)" in data["threat_logic"][0]


# ---- Warnings cascade ----


def test_tornado_warning_inside_base_tier_is_significant():
    data, _ = brief(warnings=_env(warnings=[tor_warning(inside=True)]))
    assert data["threat_level"] == "significant"
    assert data["threat_logic"][0] == "Tornado Warning in effect at the point."
    assert data["warnings_summary"]["active_at_point"] == 1
    assert data["hazards_ranked"][0]["hazard"] == "tornado"
    assert data["hazards_ranked"][0]["score"] == 60


def test_tornado_warning_corroborated_by_lsr_is_extreme():
    data, _ = brief(
        warnings=_env(warnings=[tor_warning(inside=True)]),
        reports=_env(counts={"tornado": 2}),
    )
    assert data["threat_level"] == "extreme"
    assert "corroborated by confirmed tornado reports nearby" in data["threat_logic"][0]
    assert "immediate life-safety situation" in data["threat_logic"][0]


def test_tornado_warning_considerable_damage_tag_is_extreme():
    data, _ = brief(
        warnings=_env(
            warnings=[tor_warning(inside=True, ibw={"tornado_damage_threat": "CONSIDERABLE"})]
        )
    )
    assert data["threat_level"] == "extreme"
    assert "'Considerable' damage threat" in data["threat_logic"][0]
    # tornado score: 60 base inside + 30 damage-tag boost
    tornado = next(h for h in data["hazards_ranked"] if h["hazard"] == "tornado")
    assert tornado["score"] == 90
    assert tornado["level"] == "extreme"


def test_svr_destructive_tag_is_significant():
    data, _ = brief(
        warnings=_env(
            warnings=[
                svr_warning(
                    inside=True,
                    ibw={
                        "thunderstorm_damage_threat": "DESTRUCTIVE",
                        "max_hail_size_in": 2.0,
                        "max_wind_gust_mph": 90,
                    },
                )
            ]
        )
    )
    assert data["threat_level"] == "significant"
    assert "tagged 'Destructive'" in data["threat_logic"][0]
    assert 'hail to 2.0"' in data["threat_logic"][0]
    assert "gusts to 90 mph" in data["threat_logic"][0]


def test_svr_inside_base_tier_is_elevated():
    data, _ = brief(warnings=_env(warnings=[svr_warning(inside=True)]))
    assert data["threat_level"] == "elevated"
    assert data["threat_logic"][0] == "Severe Thunderstorm Warning in effect at the point."


# ---- Environment cascade ----


def test_stp_with_active_storms_is_significant():
    data, _ = brief(
        environment=_env(composites={"stp_effective": 3.5}),
        mrms=_env(composite_reflectivity={"max_dbz": 55}),
    )
    assert data["threat_level"] == "significant"
    assert "Significant-tornado parameter of 3.5" in data["threat_logic"][0]
    assert "environment strongly supports tornadic supercells" in data["threat_logic"][0]


def test_stp_quiet_is_elevated_and_volatile():
    data, _ = brief(environment=_env(composites={"stp_effective": 3.5}))
    assert data["threat_level"] == "elevated"
    assert "Significant-tornado parameter of 3.5" in data["threat_logic"][0]
    assert "a volatile environment if storms can initiate" in data["threat_logic"][0]


# ---- MRMS cascade ----


def test_mesh_over_two_inches_is_significant():
    data, _ = brief(mrms=_env(hail_mesh={"max_mesh_in": 2.3}))
    assert data["threat_level"] == "significant"
    assert 'MRMS MESH of 2.3" hail' in data["threat_logic"][0]
    hail = next(h for h in data["hazards_ranked"] if h["hazard"] == "hail")
    assert hail["score"] == 45


def test_intense_lowlevel_rotation_is_significant():
    data, _ = brief(mrms=_env(rotation_lowlevel={"max_azimuthal_shear_s1": 0.012}))
    assert data["threat_level"] == "significant"
    assert "Intense rotation track (azimuthal shear 0.012 /s)" in data["threat_logic"][0]


# ---- Hazard ranking ----


def test_hazards_ranked_tornado_outranks_hail_with_tor_and_reports():
    data, _ = brief(
        warnings=_env(warnings=[tor_warning(inside=True)]),
        reports=_env(counts={"tornado": 1, "hail": 2}),
    )
    hazards = {h["hazard"]: h for h in data["hazards_ranked"]}
    # tornado: 60 (inside TOR) + 20 (corroborated) + 35 (LSR count) = 115
    assert hazards["tornado"]["score"] == 115
    assert hazards["tornado"]["level"] == "extreme"
    # hail: 15 from the hail report count only
    assert hazards["hail"]["score"] == 15
    assert hazards["hail"]["level"] == "low"
    order = [h["hazard"] for h in data["hazards_ranked"]]
    assert order.index("tornado") < order.index("hail")
    assert data["hazards_ranked"][0]["hazard"] == "tornado"


# ---- Attention window ----


def test_attention_window_now_uses_latest_warning_expiry():
    data, _ = brief(
        warnings=_env(
            warnings=[
                svr_warning(inside=True, expires="2026-06-10T21:30:00Z"),
                tor_warning(inside=False, expires="2026-06-10T20:00:00+00:00", dist=42.0, direction="SW"),
            ]
        )
    )
    att = data["attention_window"]
    assert att["window"] == "now"
    assert att["until_utc"] == "2026-06-10T21:30:00Z"
    assert "Active warnings" in att["reasoning"]


def test_attention_window_strong_storms_without_warnings():
    data, _ = brief(mrms=_env(composite_reflectivity={"max_dbz": 55}))
    att = data["attention_window"]
    assert att["window"] == "next 1-2 hours"
    assert att["until_utc"] is None


def test_attention_window_outlook_period_for_slgt_without_storms():
    data, _ = brief(
        outlook=_env(categorical={"label": "SLGT"}, expire_utc="2026-06-11T12:00:00+00:00")
    )
    assert data["threat_level"] == "marginal"  # SLGT with no storms active
    att = data["attention_window"]
    assert att["window"] == "through the outlook period"
    assert att["until_utc"] == "2026-06-11T12:00:00+00:00"


def test_attention_window_none_when_quiet():
    data, _ = brief()
    att = data["attention_window"]
    assert att["window"] == "none"
    assert att["until_utc"] is None


# ---- Nearest storm signature ----


def test_nearest_signature_picks_closest_qualifying_block():
    data, _ = brief(
        mrms=_env(
            composite_reflectivity={
                "max_dbz": 55,
                "max_location": {"distance_km": 30.0, "direction": "SW", "lat": 35.1, "lon": -97.8},
                "valid_utc": "2026-06-10T19:55:00+00:00",
            },
            hail_mesh={
                "max_mesh_in": 1.2,
                "max_location": {"distance_km": 12.0, "direction": "NE", "lat": 35.4, "lon": -97.4},
                "valid_utc": "2026-06-10T19:50:00+00:00",
            },
            # Closest of all, but 0.003 /s is below the 0.004 significance floor —
            # it must NOT be chosen.
            rotation_lowlevel={
                "max_azimuthal_shear_s1": 0.003,
                "max_location": {"distance_km": 5.0, "direction": "N", "lat": 35.38, "lon": -97.49},
                "valid_utc": "2026-06-10T19:58:00+00:00",
            },
        )
    )
    sig = data["nearest_storm_signature"]
    assert sig is not None
    assert sig["signature"] == "60-min MESH"
    assert sig["value"] == '1.2" hail'
    assert sig["distance_km"] == 12.0
    assert sig["direction"] == "NE"
    assert sig["valid_utc"] == "2026-06-10T19:50:00+00:00"


def test_nearest_signature_none_when_all_subthreshold():
    data, _ = brief(
        mrms=_env(
            composite_reflectivity={
                "max_dbz": 45,  # below the 50-dBZ floor
                "max_location": {"distance_km": 3.0, "direction": "E", "lat": 35.34, "lon": -97.45},
            },
            rotation_midlevel={
                "max_azimuthal_shear_s1": 0.003,  # below the 0.004 floor
                "max_location": {"distance_km": 8.0, "direction": "W", "lat": 35.34, "lon": -97.6},
            },
        )
    )
    assert data["nearest_storm_signature"] is None


# ---- Interpretation string ----


def test_interpretation_contains_level_caps_and_attention_window():
    data, interp = brief(
        warnings=_env(warnings=[tor_warning(inside=True, expires="2026-06-10T20:45:00+00:00")])
    )
    assert data["threat_level"] == "significant"
    assert "Overall threat level: SIGNIFICANT." in interp
    assert "Tornado Warning in effect at the point." in interp
    assert "Attention window: now" in interp
    assert "tornado" in interp  # ranked-hazards sentence


def test_interpretation_quiet_mentions_no_hazards_and_none_window():
    _, interp = brief()
    assert "Overall threat level: NONE." in interp
    assert "No individual hazard rises above background levels." in interp
    assert "Attention window: none" in interp
