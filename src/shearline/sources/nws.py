"""NWS api.weather.gov active alerts: warning polygons, IBW tags, storm motion.

Empirically verified 2026-06-10 (live MO/IA outbreak):
- User-Agent header is hard-required (403 HTML without one).
- properties.parameters values are ALWAYS arrays of strings, even singletons.
- IBW keys (exact casing): hailThreat, windThreat, maxHailSize, maxWindGust,
  thunderstormDamageThreat, tornadoDetection, tornadoDamageThreat,
  eventMotionDescription. Damage-threat keys are absent on base-tier warnings.
- maxHailSize is inches but not always numeric ("Up to .75").
- eventMotionDescription: '...240DEG...41KT...40.15,-94.16 39.96,-94.24' —
  DEG is direction the storm moves FROM, speed in knots, trailing lat,lon
  centroid pairs (lat first).
- SVR/TOR warnings always carry a Polygon; watches are zone-based (null
  geometry) and only appear in the point query.
"""

import re
from typing import Any

from shapely.geometry import Point, shape

from .. import geo
from ..cache import TTL_ALERTS
from ..fetch import get_json
from ..geo import distance_bearing

NWS_API = "https://api.weather.gov"
ACCEPT = {"Accept": "application/geo+json"}

# Warning types we sweep nationally for radius search (polygon-bearing).
SEVERE_WARNING_EVENTS = [
    "Tornado Warning",
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
]

_IBW_KEYS = {
    "tornado_detection": "tornadoDetection",
    "tornado_damage_threat": "tornadoDamageThreat",
    "thunderstorm_damage_threat": "thunderstormDamageThreat",
    "hail_threat": "hailThreat",
    "wind_threat": "windThreat",
}


def _param(params: dict[str, Any], key: str) -> str | None:
    val = params.get(key)
    if isinstance(val, list) and val:
        return str(val[0])
    return None


def _parse_float(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d*\.?\d+)", text)
    return float(m.group(1)) if m else None


def parse_storm_motion(desc: str | None) -> dict[str, Any] | None:
    """Decode eventMotionDescription's DEG/KT machine encoding."""
    if not desc:
        return None
    deg = re.search(r"(\d{1,3})DEG", desc)
    kt = re.search(r"(\d{1,3})KT", desc)
    cells = [
        {"lat": float(la), "lon": float(lo)}
        for la, lo in re.findall(r"(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)", desc)
    ]
    if deg is None and kt is None and not cells:
        return None
    from_deg = int(deg.group(1)) if deg else None
    toward_deg = (from_deg + 180) % 360 if from_deg is not None else None
    return {
        "from_deg": from_deg,
        "toward_deg": toward_deg,
        "toward_compass": geo.compass_point(toward_deg) if toward_deg is not None else None,
        "speed_kt": int(kt.group(1)) if kt else None,
        "storm_cells": cells,
    }


def _parse_warning(feature: dict, lat: float, lon: float) -> dict[str, Any]:
    props = feature.get("properties", {})
    params = props.get("parameters", {}) or {}
    geom = feature.get("geometry")

    point_inside = False
    dist_brg: dict[str, Any] = {}
    polygon_lonlat = None
    if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
        poly = shape(geom)
        polygon_lonlat = geom["coordinates"]
        pt = Point(lon, lat)
        if poly.intersects(pt):
            point_inside = True
            dist_brg = {"distance_km": 0.0, "bearing_deg": None, "direction": None}
        else:
            # nearest_points on lon/lat is a planar approximation — fine at
            # warning-polygon scales for a distance estimate.
            from shapely.ops import nearest_points

            nearest = nearest_points(poly, pt)[0]
            dist_brg = distance_bearing(lat, lon, nearest.y, nearest.x)

    ibw = {out: _param(params, src) for out, src in _IBW_KEYS.items()}
    ibw["max_hail_size_in"] = _parse_float(_param(params, "maxHailSize"))
    ibw["max_wind_gust_mph"] = _parse_float(_param(params, "maxWindGust"))

    return {
        "id": props.get("id"),
        "event": props.get("event"),
        "severity": props.get("severity"),
        "certainty": props.get("certainty"),
        "headline": props.get("headline"),
        "area_desc": props.get("areaDesc"),
        "sender": props.get("senderName"),
        "effective_utc": props.get("effective"),
        "expires_utc": props.get("expires"),
        "ends_utc": props.get("ends"),
        "message_type": props.get("messageType"),
        "point_inside": point_inside,
        **dist_brg,
        "ibw_tags": ibw,
        "storm_motion": parse_storm_motion(_param(params, "eventMotionDescription")),
        "polygon_lonlat": polygon_lonlat,
    }


async def fetch_point_alerts(lat: float, lon: float) -> list[dict]:
    data = await get_json(
        f"{NWS_API}/alerts/active?point={lat:.4f},{lon:.4f}",
        ttl=TTL_ALERTS,
        headers=ACCEPT,
    )
    return data.get("features", [])


async def fetch_event_alerts(event: str) -> list[dict]:
    data = await get_json(
        f"{NWS_API}/alerts/active?event={event.replace(' ', '%20')}",
        ttl=TTL_ALERTS,
        headers=ACCEPT,
    )
    return data.get("features", [])


def is_warning(feature: dict) -> bool:
    props = feature.get("properties", {})
    return (
        props.get("messageType") in ("Alert", "Update")
        and "Warning" in (props.get("event") or "")
    )


def is_watch(feature: dict) -> bool:
    return "Watch" in (feature.get("properties", {}).get("event") or "")
