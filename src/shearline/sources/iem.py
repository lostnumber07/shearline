"""Iowa Environmental Mesonet Local Storm Reports, point+radius search.

Empirically verified 2026-06-10 and 2026-06-13 against
https://mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point.geojson:
- params lon, lat, radius_miles (STRICTLY <1000), begints/endts (UTC ISO). BOTH
  begints AND endts must be sent — with only one, the time filter is
  silently ignored and the full multi-year archive comes back.
- magnitude is a number or null (tornado LSRs have no magnitude — EF ratings
  come later in surveys); hail is inches, wind gusts are MPH (marine too).
- valid is the event time, UTC. `product_id` can lag the event by days
  (delayed/amended LSRs) — always use `valid` for the event time.
- The SAME endpoint serves ARBITRARY PAST single-day windows (historical tool),
  back to ~2005 at interior points. There is NO server-side row cap here, so
  callers must keep the radius small and the window to one day. Pre-2005 dates
  return an empty 200 indistinguishable from a quiet day.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import TTL_HISTORICAL, TTL_LSR
from ..fetch import get_json
from ..geo import distance_bearing

API = "https://mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point.geojson"

KM_PER_MILE = 1.609344
# IEM Local Storm Report point coverage realistically begins here; earlier dates
# return an empty result that cannot be distinguished from a quiet day.
EARLIEST_DATA_YEAR = 2005

# type code -> (category, magnitude units)
TYPE_INFO: dict[str, tuple[str, str | None]] = {
    "T": ("tornado", None),
    "C": ("funnel_cloud", None),
    "W": ("waterspout", None),
    "H": ("hail", "inches"),
    "G": ("thunderstorm_wind_gust", "mph"),
    "D": ("thunderstorm_wind_damage", None),
    "N": ("non_thunderstorm_wind_gust", "mph"),
    "A": ("high_sustained_wind", "mph"),
    "O": ("non_thunderstorm_wind_damage", None),
    "M": ("marine_thunderstorm_wind", "mph"),
    "F": ("flash_flood", None),
    "E": ("flood", None),
    "R": ("heavy_rain", "inches"),
    "2": ("dust", None),
}

WIND_CATEGORIES = {
    "thunderstorm_wind_gust",
    "thunderstorm_wind_damage",
    "non_thunderstorm_wind_gust",
    "high_sustained_wind",
    "non_thunderstorm_wind_damage",
    "marine_thunderstorm_wind",
}


def _parse_features(features: list[dict], lat: float, lon: float) -> list[dict[str, Any]]:
    reports = []
    for feat in features:
        props = feat.get("properties", {})
        code = props.get("type") or ""
        fallback = ((props.get("typetext") or "other").lower().replace(" ", "_"), None)
        category, units = TYPE_INFO.get(code, fallback)
        r_lat, r_lon = props.get("lat"), props.get("lon")
        if r_lat is None or r_lon is None:
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None]
            r_lon, r_lat = coords[0], coords[1]
        entry: dict[str, Any] = {
            "category": category,
            "type_text": props.get("typetext"),
            "magnitude": props.get("magnitude"),
            "magnitude_units": units if props.get("magnitude") is not None else None,
            "time_utc": props.get("valid"),
            "city": props.get("city"),
            "county": props.get("county"),
            "state": props.get("state") or props.get("st"),
            "source": props.get("source"),
            "remarks": props.get("remark"),
        }
        if r_lat is not None and r_lon is not None:
            entry.update(distance_bearing(lat, lon, float(r_lat), float(r_lon)))
        reports.append(entry)
    reports.sort(key=lambda r: r.get("time_utc") or "", reverse=True)
    return reports


async def _fetch_window(
    lat: float,
    lon: float,
    radius_km: float,
    begints: str,
    endts: str,
    *,
    cache_key: str,
    ttl: float,
) -> list[dict[str, Any]]:
    """Fetch + normalize LSRs for an explicit UTC window. BOTH begints and endts
    are always sent (a lone bound is silently ignored upstream)."""
    radius_miles = min(radius_km / KM_PER_MILE, 999.0)
    url = (
        f"{API}?lon={lon:.4f}&lat={lat:.4f}&radius_miles={radius_miles:.1f}"
        f"&begints={begints}&endts={endts}"
    )
    data = await get_json(url, ttl=ttl, cache_key=cache_key)
    return _parse_features(data.get("features", []), lat, lon)


async def fetch_reports(
    lat: float, lon: float, radius_km: float, hours: float
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    begin = now - timedelta(hours=hours)
    end = now + timedelta(minutes=10)  # small pad so brand-new reports aren't excluded
    return await _fetch_window(
        lat,
        lon,
        radius_km,
        begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        cache_key=f"lsr:{lat:.2f},{lon:.2f},{radius_km:.0f},{hours:.0f}",
        ttl=TTL_LSR,
    )


async def fetch_historical_reports(
    lat: float, lon: float, radius_km: float, date_iso: str
) -> list[dict[str, Any]]:
    """LSRs for a single past UTC calendar day ('YYYY-MM-DD')."""
    return await _fetch_window(
        lat,
        lon,
        radius_km,
        f"{date_iso}T00:00:00Z",
        f"{date_iso}T23:59:59Z",
        cache_key=f"lsr-hist:{lat:.2f},{lon:.2f},{radius_km:.0f},{date_iso}",
        ttl=TTL_HISTORICAL,
    )


def count_reports(reports: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"tornado": 0, "hail": 0, "wind": 0, "flood": 0, "other": 0}
    for r in reports:
        cat = r["category"]
        if cat in ("tornado", "waterspout"):
            counts["tornado"] += 1
        elif cat == "hail":
            counts["hail"] += 1
        elif cat in WIND_CATEGORIES:
            counts["wind"] += 1
        elif cat in ("flash_flood", "flood", "heavy_rain"):
            counts["flood"] += 1
        else:
            counts["other"] += 1
    return counts


def interpret(reports: list[dict[str, Any]], radius_km: float, hours: float) -> str:
    counts = count_reports(reports)
    if not reports:
        return (
            f"No local storm reports within {radius_km:.0f} km in the past {hours:.0f} "
            "hours — spotters and offices have had nothing severe to report near this point."
        )
    parts = []
    if counts["tornado"]:
        nearest = next(
            (r for r in reports if r["category"] in ("tornado", "waterspout")), None
        )
        parts.append(
            f"{counts['tornado']} tornado/waterspout report(s), most recent near "
            f"{nearest['city']}, {nearest['state']} at {nearest['time_utc']}"
        )
    if counts["hail"]:
        biggest = max(
            (r for r in reports if r["category"] == "hail" and r["magnitude"] is not None),
            key=lambda r: r["magnitude"],
            default=None,
        )
        parts.append(
            f"{counts['hail']} hail report(s)"
            + (f", largest {biggest['magnitude']}\"" if biggest else "")
        )
    if counts["wind"]:
        strongest = max(
            (r for r in reports if r["category"] in WIND_CATEGORIES and r["magnitude"] is not None),
            key=lambda r: r["magnitude"],
            default=None,
        )
        parts.append(
            f"{counts['wind']} wind report(s)"
            + (f", peak gust {strongest['magnitude']:.0f} mph" if strongest else "")
        )
    if counts["flood"]:
        parts.append(f"{counts['flood']} flood/rain report(s)")
    if counts["other"]:
        parts.append(f"{counts['other']} other report(s)")

    lead = (
        f"{len(reports)} local storm reports within {radius_km:.0f} km over the past "
        f"{hours:.0f} hours: " + "; ".join(parts) + "."
    )
    recent = [r for r in reports if r.get("distance_km", 1e9) <= radius_km / 2]
    if counts["tornado"]:
        lead += " Confirmed tornadic activity in the area means this is an active, dangerous situation."
    elif len(recent) >= 3:
        lead += " Multiple nearby reports indicate ongoing severe weather close to the point."
    return lead


def validate_historical_date(date_iso: str) -> str:
    """Validate a 'YYYY-MM-DD' UTC calendar day for the historical tool.

    Returns the normalized ISO date string, or raises ValueError with an
    actionable message (future date, bad format, or before coverage begins).
    """
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError as exc:
        raise ValueError(
            f"date must be a single UTC calendar day formatted 'YYYY-MM-DD' (got {date_iso!r})."
        ) from exc
    today = datetime.now(UTC).date()
    if d > today:
        raise ValueError(
            f"date {date_iso} is in the future; get_historical_storm_reports queries past "
            "events. Use get_storm_reports for the current situation."
        )
    if d.year < EARLIEST_DATA_YEAR:
        raise ValueError(
            f"date {date_iso} predates reliable IEM Local Storm Report point coverage "
            f"(~{EARLIEST_DATA_YEAR}); an empty result for an older date cannot be "
            "distinguished from a quiet day, so this tool does not serve it."
        )
    return d.isoformat()


def interpret_historical(
    reports: list[dict[str, Any]], radius_km: float, date_iso: str
) -> str:
    """Analyst sentences for a single past day's reports (data provenance noted)."""
    counts = count_reports(reports)
    provenance = (
        "Source: NWS/spotter Local Storm Reports via the Iowa Environmental Mesonet "
        "(preliminary reports, not the final NCEI Storm Events record)."
    )
    sparse = ""
    if int(date_iso[:4]) < 2008:
        sparse = (
            " Note: LSR archiving is sparse before ~2008, so an empty or thin result for "
            "this date may reflect incomplete archiving rather than a quiet day."
        )
    if not reports:
        return (
            f"No Local Storm Reports within {radius_km:.0f} km of the point on {date_iso} (UTC). "
            f"{provenance}{sparse}"
        )
    parts = []
    if counts["tornado"]:
        parts.append(f"{counts['tornado']} tornado/waterspout report(s)")
    if counts["hail"]:
        biggest = max(
            (r for r in reports if r["category"] == "hail" and r["magnitude"] is not None),
            key=lambda r: r["magnitude"],
            default=None,
        )
        parts.append(
            f"{counts['hail']} hail report(s)"
            + (f", largest {biggest['magnitude']}\"" if biggest else "")
        )
    if counts["wind"]:
        strongest = max(
            (r for r in reports if r["category"] in WIND_CATEGORIES and r["magnitude"] is not None),
            key=lambda r: r["magnitude"],
            default=None,
        )
        parts.append(
            f"{counts['wind']} wind report(s)"
            + (f", peak gust {strongest['magnitude']:.0f} mph" if strongest else "")
        )
    if counts["flood"]:
        parts.append(f"{counts['flood']} flood/rain report(s)")
    if counts["other"]:
        parts.append(f"{counts['other']} other report(s)")
    nearest = min(reports, key=lambda r: r.get("distance_km", 1e9))
    lead = (
        f"On {date_iso} (UTC), {len(reports)} Local Storm Reports within {radius_km:.0f} km "
        f"of the point: " + "; ".join(parts) + ". "
        f"Nearest was a {nearest['type_text'] or nearest['category']} "
        f"{nearest.get('distance_km', '?')} km {nearest.get('direction', '')} of the point "
        f"at {nearest['time_utc']}. {provenance}{sparse}"
    )
    return lead
