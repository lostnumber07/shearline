"""CONUS bounds enforcement (invariant 2)."""

CONUS_LAT_MIN = 24.0
CONUS_LAT_MAX = 50.0
CONUS_LON_MIN = -125.5
CONUS_LON_MAX = -66.5


class OutOfBoundsError(ValueError):
    """Raised when coordinates fall outside the continental United States."""


def check_conus(lat: float, lon: float) -> None:
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise OutOfBoundsError("Latitude and longitude must be numbers.")
    if not (CONUS_LAT_MIN <= lat <= CONUS_LAT_MAX) or not (
        CONUS_LON_MIN <= lon <= CONUS_LON_MAX
    ):
        raise OutOfBoundsError(
            f"Point ({lat}, {lon}) is outside the continental United States. "
            f"SHEARLINE covers CONUS only (lat {CONUS_LAT_MIN} to {CONUS_LAT_MAX}, "
            f"lon {CONUS_LON_MIN} to {CONUS_LON_MAX}). Longitude west of the prime "
            "meridian must be negative, e.g. Oklahoma City is lon=-97.5."
        )
