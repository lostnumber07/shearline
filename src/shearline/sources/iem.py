"""Iowa Environmental Mesonet Local Storm Reports, point+radius search.

Empirically verified 2026-06-10 against
https://mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point.geojson:
- params lon, lat, radius_miles (<1000), begints/endts (UTC ISO). BOTH
  begints AND endts must be sent — with only one, the time filter is
  silently ignored and the full multi-year archive comes back.
- magnitude is a number or null (tornado LSRs have no magnitude — EF ratings
  come later in surveys); hail is inches, wind gusts are MPH (marine too).
- valid is the event time, UTC.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import TTL_LSR
from ..fetch import get_json
from ..geo import distance_bearing

API = "https://mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point.geojson"

KM_PER_MILE = 1.609344

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


async def fetch_reports(
    lat: float, lon: float, radius_km: float, hours: float
) -> list[dict[str, Any]]:
    radius_miles = min(radius_km / KM_PER_MILE, 999.0)
    now = datetime.now(UTC)
    begin = now - timedelta(hours=hours)
    end = now + timedelta(minutes=10)  # small pad so brand-new reports aren't excluded
    url = (
        f"{API}?lon={lon:.4f}&lat={lat:.4f}&radius_miles={radius_miles:.1f}"
        f"&begints={begin.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&endts={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    data = await get_json(
        url,
        ttl=TTL_LSR,
        cache_key=f"lsr:{lat:.2f},{lon:.2f},{radius_km:.0f},{hours:.0f}",
    )
    reports = []
    for feat in data.get("features", []):
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
