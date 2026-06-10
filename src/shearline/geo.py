"""Great-circle geometry helpers: distance, bearing, compass names."""

import math

EARTH_RADIUS_KM = 6371.0

_COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing FROM point 1 TO point 2, degrees clockwise from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_point(bearing_deg: float) -> str:
    idx = int((bearing_deg % 360.0) / 22.5 + 0.5) % 16
    return _COMPASS_16[idx]


def distance_bearing(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float
) -> dict:
    """Distance/bearing of a target as seen from a reference point."""
    bearing = initial_bearing_deg(from_lat, from_lon, to_lat, to_lon)
    return {
        "distance_km": round(haversine_km(from_lat, from_lon, to_lat, to_lon), 1),
        "bearing_deg": round(bearing),
        "direction": compass_point(bearing),
    }
