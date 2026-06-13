"""SHEARLINE MCP server: analyst-grade US severe-weather tools.

Transports: stdio (default) and streamable HTTP (--http --port 8741).
Every tool returns {data, interpretation, degraded, disclaimer} and rejects
points outside CONUS. Upstream failures degrade gracefully — a tool returns
whatever sources succeeded plus a `degraded` list, never a bare traceback.
"""

import argparse
import asyncio
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .bounds import check_conus
from .derive import threat as threat_derive
from .derive import trend as trend_derive
from .derive.environment import compute_environment, interpret_environment
from .envelope import envelope
from .observability import observe
from .sources import iem, lightning, mrms, nexrad, nws, rap, spc

mcp = FastMCP(
    "shearline",
    instructions=(
        "Analyst-grade US severe-weather tools: live warnings with IBW tags, SPC "
        "outlooks, RAP-derived point environments, MRMS hail/rotation products, "
        "storm reports, and a composite threat brief. CONUS only. All output is "
        "informational and not a substitute for official NWS warnings."
    ),
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------
# Payload builders (shared between individual tools and the threat brief)
# --------------------------------------------------------------------------


async def _warnings_payload(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    degraded: list[str] = []
    point_feats: list[dict] = []
    try:
        point_feats = await nws.fetch_point_alerts(lat, lon)
    except Exception:
        degraded.append("nws-point-alerts")

    national: list[dict] = []
    for event in nws.SEVERE_WARNING_EVENTS:
        try:
            national.extend(await nws.fetch_event_alerts(event))
        except Exception:
            degraded.append(f"nws-{event.lower().replace(' ', '-')}")

    warnings: dict[str, dict] = {}
    for feat in national:
        props = feat.get("properties", {})
        if props.get("messageType") == "Cancel":
            continue
        parsed = nws._parse_warning(feat, lat, lon)
        if parsed["point_inside"] or (
            parsed.get("distance_km") is not None and parsed["distance_km"] <= radius_km
        ):
            warnings[parsed["id"]] = parsed

    watches: list[dict] = []
    for feat in point_feats:
        props = feat.get("properties", {})
        if props.get("messageType") == "Cancel":
            continue
        if nws.is_watch(feat):
            watches.append(
                {
                    "event": props.get("event"),
                    "expires_utc": props.get("expires"),
                    "headline": props.get("headline"),
                }
            )
        elif nws.is_warning(feat):
            # only severe-convective events pass is_warning; the point query
            # also returns winter/flood/fire products we must not count.
            parsed = nws._parse_warning(feat, lat, lon)
            if parsed["id"] not in warnings:
                if feat.get("geometry") is None:
                    # zone-based warning covering the point (no polygon)
                    parsed["point_inside"] = True
                    parsed["scope"] = "zone"
                warnings[parsed["id"]] = parsed

    ordered = sorted(
        warnings.values(),
        key=lambda x: (not x["point_inside"], x.get("distance_km") or 0),
    )
    counts: dict[str, int] = {}
    for warn in ordered:
        counts[warn["event"]] = counts.get(warn["event"], 0) + 1
    inside_any = any(x["point_inside"] for x in ordered)

    data = {
        "point": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "point_inside_warning": inside_any,
        "warnings": ordered,
        "watches_at_point": watches,
        "counts": counts,
    }
    return envelope(data, _interpret_warnings(data, degraded), degraded=degraded)


def _interpret_warnings(data: dict[str, Any], degraded: list[str]) -> str:
    warnings = data["warnings"]
    watches = data["watches_at_point"]
    radius = data["radius_km"]
    # A feed outage must never read as an all-clear (safety invariant).
    if degraded and not warnings and not watches:
        return (
            f"WARNING STATUS UNKNOWN: {len(degraded)} NWS alert feed(s) were unreachable "
            f"({', '.join(degraded)}), and no warnings were found in the feeds that did "
            "respond. Do NOT treat this as a confirmed all-clear — retry shortly and "
            "consult weather.gov directly."
        )
    if not warnings and not watches:
        return (
            f"No active severe weather warnings within {radius:.0f} km of the point, and "
            "no watches in effect there. Current officially-warned threat is nil; check "
            "the outlook and environment tools for what could develop."
        )
    s: list[str] = []
    if degraded:
        s.append(
            f"Note: {len(degraded)} NWS feed(s) were unreachable ({', '.join(degraded)}), "
            "so this picture may be incomplete."
        )
    inside = [w for w in warnings if w["point_inside"]]
    if inside:
        worst = inside[0]
        ibw = worst["ibw_tags"]
        tags = []
        if ibw.get("max_hail_size_in"):
            tags.append(f"hail to {ibw['max_hail_size_in']}\"")
        if ibw.get("max_wind_gust_mph"):
            tags.append(f"gusts to {ibw['max_wind_gust_mph']:.0f} mph")
        if ibw.get("tornado_damage_threat"):
            tags.append(f"tornado damage threat {ibw['tornado_damage_threat']}")
        elif ibw.get("tornado_detection"):
            tags.append(f"tornado {ibw['tornado_detection'].lower()}")
        s.append(
            f"The point is INSIDE a {worst['event']}"
            + (f" ({', '.join(tags)})" if tags else "")
            + f", expiring {worst['expires_utc']}."
        )
        motion = worst.get("storm_motion") or {}
        if motion.get("toward_compass"):
            s.append(
                f"Warned storm motion is toward the {motion['toward_compass']} at "
                f"{motion.get('speed_kt', '?')} kt."
            )
    nearby = [w for w in warnings if not w["point_inside"]]
    if nearby:
        nearest = nearby[0]
        s.append(
            f"{len(nearby)} other active warning(s) within {radius:.0f} km; nearest is a "
            f"{nearest['event']} {nearest['distance_km']} km {nearest.get('direction', '')} away."
        )
    if watches:
        names = ", ".join(sorted({x["event"] for x in watches}))
        s.append(f"The point is inside: {names} — conditions favor severe development.")
    return " ".join(s)


async def _outlook_payload(lat: float, lon: float, day: int) -> dict[str, Any]:
    degraded: list[str] = []
    layers = spc.LAYERS_BY_DAY[day]

    cat_layer: dict | None = None
    try:
        cat_layer = await spc.fetch_layer(layers["categorical"])
    except Exception:
        degraded.append("spc-categorical")

    categorical = (
        spc.categorical_at_point(cat_layer, lat, lon)
        if cat_layer
        else {"dn": None, "label": None, "description": None}
    )
    times = spc.layer_times(cat_layer) if cat_layer else {}
    ref_valid = spc.layer_valid(cat_layer) if cat_layer else None

    probabilities: dict[str, Any] = {}
    hazard_layers = {k: v for k, v in layers.items() if k != "categorical"}
    base_names = {k: v for k, v in hazard_layers.items() if not k.endswith("_cig")}
    if cat_layer is None:
        # Without the categorical layer's VALID we cannot detect SPC's frozen
        # relic files (they keep serving HTTP 200) — skip rather than risk
        # presenting a stale probability as current.
        degraded.extend(f"spc-{hazard}-unverified" for hazard in base_names)
    else:
        for hazard, layer_name in base_names.items():
            try:
                layer = await spc.fetch_layer(layer_name)
                if spc.is_stale(layer, ref_valid):
                    degraded.append(f"spc-{hazard}-stale")
                    continue
                result = spc.probability_at_point(layer, lat, lon)
                cig_name = hazard_layers.get(f"{hazard}_cig")
                if cig_name:
                    try:
                        cig_layer = await spc.fetch_layer(cig_name)
                        if not spc.is_stale(cig_layer, ref_valid):
                            cig_result = spc.probability_at_point(cig_layer, lat, lon)
                            result["conditional_intensity"] = (
                                result["conditional_intensity"]
                                or cig_result["conditional_intensity"]
                            )
                            # 'significant' is reserved for the legacy SIGN
                            # hatching; CIG groups are reported faithfully as
                            # conditional_intensity without overclaiming.
                            result["significant"] = bool(
                                result["significant"] or cig_result["significant"]
                            )
                    except Exception:
                        degraded.append(f"spc-{hazard}-cig")
                probabilities[hazard] = result
            except Exception:
                degraded.append(f"spc-{hazard}")

    data = {
        "point": {"lat": lat, "lon": lon},
        "day": day,
        **times,
        "categorical": categorical,
        "probabilities": probabilities,
    }
    return envelope(data, _interpret_outlook(data, degraded), degraded=degraded)


def _interpret_outlook(data: dict[str, Any], degraded: list[str]) -> str:
    cat = data["categorical"]
    probs = data["probabilities"]
    day = data["day"]
    label = cat.get("label")
    s: list[str] = []
    if "spc-categorical" in degraded:
        return (
            f"OUTLOOK STATUS UNKNOWN: the SPC day-{day} categorical layer was unreachable, "
            "so the risk at this point cannot be assessed right now. Do NOT treat this as "
            "'no risk' — retry shortly or check spc.noaa.gov directly."
        )
    if label is None:
        s.append(
            f"The point is outside any SPC day-{day} risk area: no thunderstorms are "
            "forecast there in this outlook."
        )
        return " ".join(s)
    s.append(f"SPC day {day} categorical risk at the point: {label} — {cat['description']}.")
    parts = []
    for hazard in ("tornado", "hail", "wind", "total_severe"):
        p = probs.get(hazard)
        if p and p.get("probability_pct"):
            txt = f"{hazard.replace('_', ' ')} {p['probability_pct']}%"
            if p.get("significant"):
                txt += " (significant-severe flagged)"
            elif p.get("conditional_intensity"):
                txt += f" (conditional intensity group {p['conditional_intensity']})"
            parts.append(txt)
    if parts:
        s.append("Probabilities within 25 miles of the point: " + ", ".join(parts) + ".")
    torn = (probs.get("tornado") or {}).get("probability_pct") or 0
    if label in ("MDT", "HIGH"):
        s.append(
            "This is a rare, well-advertised severe weather day — plan around it and keep "
            "multiple warning sources available."
        )
    elif label == "ENH" or torn >= 10:
        s.append(
            "Organized severe storms are expected in the area; stay weather-aware from "
            "midday onward and recheck warnings frequently."
        )
    elif label in ("MRGL", "SLGT"):
        s.append(
            "Isolated to scattered severe storms are possible but coverage should be "
            "limited — a normal-caution day."
        )
    else:
        s.append("Severe weather is not anticipated; garden-variety storms at most.")
    return " ".join(s)


async def _environment_payload(lat: float, lon: float) -> dict[str, Any]:
    try:
        profile = await rap.fetch_profile(lat, lon)
        data = await asyncio.to_thread(compute_environment, profile)
        return envelope(data, interpret_environment(data))
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}},
            f"The RAP point environment could not be computed: {exc}. "
            "Other tools (warnings, outlook, radar products) are unaffected.",
            degraded=["rap-environment"],
        )


async def _environment_trend_payload(lat: float, lon: float) -> dict[str, Any]:
    try:
        cycle_iso, profiles = await rap.fetch_forecast_profiles(lat, lon)
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}},
            f"The RAP forecast-environment trend could not be computed: {exc}. "
            "Other tools are unaffected.",
            degraded=["rap-trend"],
        )
    series = []
    for profile in profiles:
        env = await asyncio.to_thread(compute_environment, profile)
        series.append(trend_derive.summarize(env, profile["forecast_hour"]))
    data = {
        "point": {"lat": lat, "lon": lon},
        "model": "RAP 13-km forecast",
        "cycle_utc": cycle_iso,
        "forecast_hours": [s["forecast_hour"] for s in series],
        "series": series,
    }
    return envelope(data, trend_derive.interpret_trend(series))


async def _lightning_payload(
    lat: float, lon: float, radius_km: float, minutes: float
) -> dict[str, Any]:
    try:
        data = await lightning.fetch_lightning(lat, lon, radius_km, minutes)
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}, "radius_km": radius_km, "window_minutes": minutes},
            f"GOES GLM lightning is currently unavailable ({exc}).",
            degraded=["goes-glm"],
        )
    data["point"] = {"lat": lat, "lon": lon}
    return envelope(data, lightning.interpret(data))


async def _mrms_payload(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    degraded: list[str] = []
    samples: dict[str, dict | None] = {}
    results = await asyncio.gather(
        *(mrms.sample_product(name, lat, lon, radius_km) for name in mrms.PRODUCTS),
        return_exceptions=True,
    )
    for name, result in zip(mrms.PRODUCTS, results, strict=True):
        if isinstance(result, BaseException):
            degraded.append(f"mrms-{name}")
            samples[name] = None
        else:
            samples[name] = result
    data = {
        "point": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        **mrms.shape_results(samples),
    }
    if all(v is None for v in samples.values()):
        interp = "All MRMS products are currently unavailable — radar-derived hail/rotation status unknown."
    else:
        interp = mrms.interpret(data, radius_km)
    return envelope(data, interp, degraded=degraded)


async def _reports_payload(
    lat: float, lon: float, radius_km: float, hours: float
) -> dict[str, Any]:
    try:
        reports = await iem.fetch_reports(lat, lon, radius_km, hours)
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}, "radius_km": radius_km, "hours": hours},
            f"Local storm reports are currently unavailable ({exc}).",
            degraded=["iem-lsr"],
        )
    data = {
        "point": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "hours": hours,
        "counts": iem.count_reports(reports),
        "reports": reports[:100],
    }
    return envelope(data, iem.interpret(reports, radius_km, hours))


async def _historical_reports_payload(
    lat: float, lon: float, date_iso: str, radius_km: float
) -> dict[str, Any]:
    try:
        reports = await iem.fetch_historical_reports(lat, lon, radius_km, date_iso)
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}, "radius_km": radius_km, "date": date_iso},
            f"Historical storm reports for {date_iso} are currently unavailable ({exc}).",
            degraded=["iem-lsr-historical"],
        )
    data = {
        "point": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "date": date_iso,
        "counts": iem.count_reports(reports),
        "reports": reports[:200],
    }
    return envelope(data, iem.interpret_historical(reports, radius_km, date_iso))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
@observe
async def get_active_warnings(lat: float, lon: float, radius_km: float = 40) -> dict[str, Any]:
    """Active NWS severe-weather warning polygons near a CONUS point.

    Returns tornado / severe thunderstorm / flash flood warnings within
    radius_km, each with Impact-Based Warning tags (max hail size, max wind
    gust, tornado detection / damage threat), parsed storm motion, expiration
    times, polygon geometry, and whether the exact point is inside the
    polygon. Watches in effect at the point are listed separately.
    """
    check_conus(lat, lon)
    return await _warnings_payload(lat, lon, _clamp(radius_km, 5, 250))


@mcp.tool()
@observe
async def get_spc_outlook(lat: float, lon: float, day: int = 1) -> dict[str, Any]:
    """SPC convective outlook at a CONUS point for day 1, 2, or 3.

    Returns the categorical risk (TSTM/MRGL/SLGT/ENH/MDT/HIGH) plus hazard
    probabilities (tornado/hail/wind for days 1-2; total severe for day 3)
    and significant-severe flags, with an interpretation calibrated to the
    category.
    """
    check_conus(lat, lon)
    if day not in (1, 2, 3):
        raise ValueError("day must be 1, 2, or 3 (SPC GeoJSON outlooks cover days 1-3).")
    return await _outlook_payload(lat, lon, day)


@mcp.tool()
@observe
async def get_point_environment(lat: float, lon: float) -> dict[str, Any]:
    """RAP-analysis severe-weather environment at a CONUS point.

    Downloads the latest RAP 13-km analysis profile and computes, with MetPy:
    MLCAPE/MUCAPE/SBCAPE and CINs, LCL height, 0-1 and 0-6 km bulk shear,
    0-1 and 0-3 km storm-relative helicity, Bunkers storm motion, effective
    inflow layer, effective SRH/shear, supercell composite (SCP) and
    significant-tornado parameter (STP). Interpretation reasons through the
    parameter space like an analyst. First call may take several seconds.
    """
    check_conus(lat, lon)
    return await _environment_payload(lat, lon)


@mcp.tool()
@observe
async def get_environment_trend(lat: float, lon: float) -> dict[str, Any]:
    """RAP forecast-environment trend at a CONUS point (the anticipatory view).

    Where get_point_environment is "now" (the f00 analysis), this returns a short
    forecast series (f00/f01/f03/f06) of the discriminating quantities — MLCAPE,
    0-6 km bulk shear, 0-1 km SRH, supercell composite, significant-tornado
    parameter — all from one consistent RAP cycle, with an interpretation of the
    TRAJECTORY (intensifying / stabilizing / steady). Downloads and decodes four
    forecast hours, so the first call can take ~15-20 seconds.
    """
    check_conus(lat, lon)
    return await _environment_trend_payload(lat, lon)


@mcp.tool()
@observe
async def get_mrms_severe(lat: float, lon: float, radius_km: float = 40) -> dict[str, Any]:
    """MRMS radar-derived severe weather products near a CONUS point.

    Samples within radius_km: max 60-minute MESH (hail size, inches and mm),
    max low-level and mid-level rotation-track azimuthal shear over the last
    hour, max VIL, and max composite reflectivity — each with valid time and
    the distance/bearing of the maximum from the point.
    """
    check_conus(lat, lon)
    return await _mrms_payload(lat, lon, _clamp(radius_km, 5, 200))


@mcp.tool()
@observe
async def get_lightning(
    lat: float, lon: float, radius_km: float = 40, minutes: float = 15
) -> dict[str, Any]:
    """GOES GLM total-lightning activity near a CONUS point in the recent window.

    Returns the flash count and rate within radius_km over the last `minutes`,
    the nearest strike (distance/bearing/time), and a tiered outdoor-safety
    interpretation (overhead / within-striking-distance / in-the-area). GLM
    detects total lightning — both in-cloud and cloud-to-ground — from the
    GOES-East satellite, with ~20-40 s latency.
    """
    check_conus(lat, lon)
    return await _lightning_payload(
        lat, lon, _clamp(radius_km, 5, 100), _clamp(minutes, 1, 30)
    )


@mcp.tool()
@observe
async def get_storm_reports(
    lat: float, lon: float, radius_km: float = 80, hours: float = 6
) -> dict[str, Any]:
    """Local Storm Reports (spotter/official reports) near a CONUS point.

    Normalized tornado, hail, wind, and flood reports within radius_km over
    the past N hours: type, magnitude with units, time, location,
    distance/bearing from the point, and remarks.
    """
    check_conus(lat, lon)
    return await _reports_payload(
        lat, lon, _clamp(radius_km, 5, 500), _clamp(hours, 1, 48)
    )


@mcp.tool()
@observe
async def get_historical_storm_reports(
    lat: float, lon: float, date: str, radius_km: float = 80
) -> dict[str, Any]:
    """Local Storm Reports near a CONUS point on a specific PAST date.

    Answers "what hail/wind/tornado hit this location on this day." `date` is a
    single UTC calendar day formatted 'YYYY-MM-DD'. Returns normalized reports
    (type, magnitude with units, time, location, distance/bearing, remarks) plus
    a summary. Coverage begins ~2005; the data is preliminary NWS/spotter reports
    via the Iowa Environmental Mesonet, not the final NCEI Storm Events record.
    For the current situation use get_storm_reports instead.
    """
    check_conus(lat, lon)
    date_iso = iem.validate_historical_date(date)
    return await _historical_reports_payload(
        lat, lon, date_iso, _clamp(radius_km, 5, 200)
    )


@mcp.tool()
@observe
async def get_threat_brief(lat: float, lon: float) -> dict[str, Any]:
    """Composite severe-weather threat brief for a CONUS point.

    Runs warnings, SPC outlook, RAP environment, MRMS products, and storm
    reports concurrently, then synthesizes: an overall threat level
    (none/marginal/elevated/significant/extreme) with stated logic, hazards
    ranked by concern, an environment summary, the nearest current storm
    signature, and a recommended attention window. The first call can take
    ~10 seconds while the RAP profile downloads.
    """
    check_conus(lat, lon)
    results = await asyncio.gather(
        _warnings_payload(lat, lon, 40),
        _outlook_payload(lat, lon, 1),
        _environment_payload(lat, lon),
        _mrms_payload(lat, lon, 40),
        _reports_payload(lat, lon, 80, 6),
        _lightning_payload(lat, lon, 40, 15),
        return_exceptions=True,
    )
    names = ["warnings", "spc-outlook", "rap-environment", "mrms", "storm-reports", "glm-lightning"]
    payloads: list[dict | None] = []
    degraded: list[str] = []
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            payloads.append(None)
            degraded.append(name)
        else:
            payloads.append(result)
            degraded.extend(result.get("degraded") or [])

    data, interp = threat_derive.build_threat_brief(lat, lon, *payloads)
    return envelope(data, interp, degraded=sorted(set(degraded)))


@mcp.tool()
@observe
async def get_radar_snapshot(lat: float, lon: float) -> dict[str, Any]:
    """Latest NEXRAD Level 2 volume metadata from the nearest WSR-88D radar.

    Metadata only (no imagery): radar site and distance, volume scan start
    time, VCP (scan strategy) with meaning, max reflectivity and its
    range/azimuth, and a coarse 18-dBZ echo-top estimate. Downloads a full
    Level 2 volume (~5-15 MB), so expect a few seconds on first call.
    """
    check_conus(lat, lon)
    try:
        data = await nexrad.radar_snapshot(lat, lon)
        return envelope(data, nexrad.interpret(data))
    except Exception as exc:
        return envelope(
            {"point": {"lat": lat, "lon": lon}},
            f"NEXRAD Level 2 snapshot unavailable: {exc}",
            degraded=["nexrad-level2"],
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="shearline",
        description="SHEARLINE — analyst-grade US severe-weather MCP server.",
    )
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8741, help="HTTP port (default 8741)")
    parser.add_argument("--version", action="version", version=f"shearline {__version__}")
    args = parser.parse_args()

    if args.http:
        _run_http(args.host, args.port)
    else:
        # stdio: no logging to stdout (it carries the JSON-RPC stream), no rate
        # limiting, no observability — behaviour is exactly as before.
        mcp.run()


def _run_http(host: str, port: int) -> None:
    """Serve streamable HTTP with structured per-request logging and a per-client
    rate limit. Observability is enabled here and only here (never in stdio)."""
    import uvicorn

    from . import observability
    from .ratelimit import RateLimitMiddleware

    observability.configure(os.environ.get("SHEARLINE_LOG_LEVEL", "INFO"))
    if os.environ.get("SHEARLINE_HTTP_LOG", "1") != "0":
        observability.enable()

    mcp.settings.host = host
    mcp.settings.port = port
    app = RateLimitMiddleware.from_env(mcp.streamable_http_app())
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
