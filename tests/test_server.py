"""Tests for shearline.server: envelope contract, CONUS bounds, clamping.

All HTTP is intercepted by respx (the shared httpx.AsyncClient in
shearline.fetch is patched at the transport level), so every test here runs
fully offline. The bounds tests additionally assert ZERO recorded HTTP calls,
proving check_conus fires before any upstream fetch.
"""

import httpx
import pytest
import respx

from shearline import server
from shearline.bounds import OutOfBoundsError
from shearline.cache import CACHE
from shearline.envelope import DISCLAIMER

# Every MCP tool coroutine exposed by the server. In mcp 1.27 the
# @mcp.tool() decorator returns the original async function, so these are
# directly awaitable with (lat, lon).
ALL_TOOLS = [
    server.get_active_warnings,
    server.get_spc_outlook,
    server.get_point_environment,
    server.get_mrms_severe,
    server.get_storm_reports,
    server.get_threat_brief,
    server.get_radar_snapshot,
]

ENVELOPE_KEYS = {"data", "interpretation", "degraded", "disclaimer"}


@pytest.fixture(autouse=True)
def clear_cache():
    """CACHE is a module-level singleton; never let entries leak across tests."""
    CACHE.clear()
    yield
    CACHE.clear()


# --- tool registration --------------------------------------------------------


async def test_mcp_registers_exactly_seven_tools():
    tools = await server.mcp.list_tools()
    assert len(tools) == 7
    assert {t.name for t in tools} == {
        "get_active_warnings",
        "get_spc_outlook",
        "get_point_environment",
        "get_mrms_severe",
        "get_storm_reports",
        "get_threat_brief",
        "get_radar_snapshot",
    }


# --- CONUS bounds enforcement (invariant 2) ------------------------------------


@pytest.mark.parametrize(
    "lat,lon",
    [
        pytest.param(51.5, -0.12, id="london"),
        pytest.param(21.3, -157.85, id="honolulu"),
    ],
)
async def test_every_tool_rejects_out_of_bounds_without_network(lat, lon):
    # No routes registered: any HTTP attempt would blow up inside respx, and
    # we additionally assert nothing was even attempted.
    with respx.mock(assert_all_called=False) as mock:
        for tool in ALL_TOOLS:
            with pytest.raises(OutOfBoundsError):
                await tool(lat, lon)
        assert len(mock.calls) == 0


# --- _clamp ---------------------------------------------------------------------


def test_clamp_below_returns_lo():
    assert server._clamp(1, 5, 250) == 5


def test_clamp_above_returns_hi():
    assert server._clamp(9999, 5, 250) == 250


def test_clamp_inside_returns_value():
    assert server._clamp(40, 5, 250) == 40


def test_clamp_at_bounds_is_identity():
    assert server._clamp(5, 5, 250) == 5
    assert server._clamp(250, 5, 250) == 250


@respx.mock
async def test_radius_clamped_through_get_active_warnings():
    respx.route(host="api.weather.gov").mock(return_value=httpx.Response(500))
    out = await server.get_active_warnings(35.0, -97.0, radius_km=9999)
    assert out["data"]["radius_km"] == 250
    out = await server.get_active_warnings(35.0, -97.0, radius_km=1)
    assert out["data"]["radius_km"] == 5


# --- envelope contract under total upstream failure ------------------------------


@respx.mock
async def test_warnings_payload_all_nws_down_keeps_envelope_contract():
    route = respx.route(host="api.weather.gov").mock(return_value=httpx.Response(500))
    out = await server._warnings_payload(35.0, -97.0, 40.0)

    assert set(out.keys()) == ENVELOPE_KEYS
    # point query + the three national event sweeps each failed
    assert out["degraded"] == [
        "nws-point-alerts",
        "nws-tornado-warning",
        "nws-severe-thunderstorm-warning",
        "nws-flash-flood-warning",
    ]
    assert route.call_count == 5  # point query retries once on failure; event sweeps do not
    assert out["disclaimer"] == "Informational only. Not a substitute for official NWS warnings."
    assert out["disclaimer"] == DISCLAIMER

    data = out["data"]
    assert data["point"] == {"lat": 35.0, "lon": -97.0}
    assert data["radius_km"] == 40.0
    assert data["point_inside_warning"] is False
    assert data["warnings"] == []
    assert data["watches_at_point"] == []
    assert data["counts"] == {}
    # graceful degradation, never a bare traceback
    assert "WARNING STATUS UNKNOWN" in out["interpretation"]  # outage must not read as all-clear


@respx.mock
async def test_outlook_payload_all_spc_down_label_none_and_degraded():
    respx.route(host="www.spc.noaa.gov").mock(return_value=httpx.Response(500))
    out = await server._outlook_payload(35.0, -97.0, 1)

    assert set(out.keys()) == ENVELOPE_KEYS
    assert "spc-categorical" in out["degraded"]
    # the three day-1 hazard layers fail too
    assert out["degraded"] == [
        "spc-categorical",
        "spc-tornado-unverified",
        "spc-wind-unverified",
        "spc-hail-unverified",
    ]  # hazard layers are skipped when staleness cannot be verified
    assert out["disclaimer"] == "Informational only. Not a substitute for official NWS warnings."

    data = out["data"]
    assert data["day"] == 1
    assert data["categorical"] == {"dn": None, "label": None, "description": None}
    assert data["probabilities"] == {}
    assert isinstance(out["interpretation"], str) and out["interpretation"]


# --- input validation beyond bounds ----------------------------------------------


async def test_spc_outlook_rejects_day_4():
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="day must be 1, 2, or 3"):
            await server.get_spc_outlook(35.0, -97.0, day=4)
        assert len(mock.calls) == 0
