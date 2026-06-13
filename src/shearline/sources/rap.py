"""NOMADS RAP grib-filter fetch + decode to a single-point profile.

Empirically verified 2026-06-10 and 2026-06-13:
- Latest f00 analysis appears ~48 min after the cycle hour; walk back up to
  4 hours (rolling into the previous day's directory near 00Z).
- Forecast hours fXX use the SAME filename/params/decode as f00 (only the
  f00->fXX token changes); all our state variables are instantaneous in fXX.
  Forecast range is cycle-dependent (f21 on 00/06/12/18Z, f51 on 03/09/15/21Z),
  so the trend series stays within f06, available on every cycle. fXX post over
  ~10 min and not strictly in order, so a fresh cycle may have f00 but not yet
  f06 — the series anchors to the latest cycle for which all hours are present.
- Moisture aloft is RH only — no dewpoint/specific humidity on pressure
  levels; dewpoint aloft must be derived (MetPy) from T + RH.
- cfgrib silently drops conflicting hypercubes: heightAboveGround must be
  opened separately for level=2 and level=10; ustm/vstm must each be opened
  by shortName or the hlcy group swallows them.
- Decoded longitudes are 0-360 east; the Lambert grid has 2-D lat/lon coords.
- Read valid_time off the decoded dataset rather than computing cycle + fhr.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..cache import CACHE, TTL_RAP
from ..fetch import client
from ..geo import haversine_km

FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_rap.pl"

VARS = ["TMP", "DPT", "RH", "HGT", "UGRD", "VGRD", "PRES", "CAPE", "CIN", "HLCY", "USTM", "VSTM"]
PRESSURE_LEVELS_MB = list(range(100, 1001, 25))  # 100..1000 hPa, 25-hPa steps (37 levels)
EXTRA_LEVELS = [
    "surface",
    "2_m_above_ground",
    "10_m_above_ground",
    "90-0_mb_above_ground",
    "180-0_mb_above_ground",
    "255-0_mb_above_ground",
    "1000-0_m_above_ground",
    "3000-0_m_above_ground",
    "0-6000_m_above_ground",
]
BOX_HALF_DEG = 0.6  # ~1.2-degree box around the point (brief: ~1 degree)
MAX_CYCLE_LOOKBACK_H = 4
# Forecast hours for the trend tool. All <= f21, so they exist on every cycle.
FORECAST_HOURS = [0, 1, 3, 6]
MAX_FORECAST_HOUR = 21  # the floor across all RAP cycles; guards the series


def _filter_params(
    date_str: str, hour: int, lat: float, lon: float, fhr: int = 0
) -> dict[str, str]:
    params: dict[str, str] = {
        "dir": f"/rap.{date_str}",
        "file": f"rap.t{hour:02d}z.awp130pgrbf{fhr:02d}.grib2",
        "subregion": "",
        "leftlon": f"{lon - BOX_HALF_DEG:.2f}",
        "rightlon": f"{lon + BOX_HALF_DEG:.2f}",
        "toplat": f"{lat + BOX_HALF_DEG:.2f}",
        "bottomlat": f"{lat - BOX_HALF_DEG:.2f}",
    }
    for var in VARS:
        params[f"var_{var}"] = "on"
    for mb in PRESSURE_LEVELS_MB:
        params[f"lev_{mb}_mb"] = "on"
    for lev in EXTRA_LEVELS:
        params[f"lev_{lev}"] = "on"
    return params


async def fetch_subset(lat: float, lon: float) -> tuple[bytes, str]:
    """Download a small grib2 subset around the point for the latest cycle.

    Returns (grib_bytes, cycle_iso). Cached per ~1 km cell for TTL_RAP.
    """
    key = f"rap:{lat:.2f},{lon:.2f}"

    async def fetch() -> tuple[bytes, str]:
        now = datetime.now(UTC)
        last_error: Exception | None = None
        for back in range(MAX_CYCLE_LOOKBACK_H + 1):
            cycle = now - timedelta(hours=back)
            try:
                resp = await client().get(
                    FILTER_URL,
                    params=_filter_params(cycle.strftime("%Y%m%d"), cycle.hour, lat, lon),
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code == 200 and resp.content[:4] == b"GRIB":
                return resp.content, cycle.strftime("%Y-%m-%dT%H:00Z")
            last_error = RuntimeError(f"HTTP {resp.status_code} for cycle t{cycle.hour:02d}z")
        raise RuntimeError(
            f"No RAP cycle available within {MAX_CYCLE_LOOKBACK_H} hours: {last_error}"
        )

    return await CACHE.get_or_fetch(key, TTL_RAP, fetch)


def _decode_profile_sync(grib: bytes, lat: float, lon: float) -> dict[str, Any]:
    """Decode the grib subset to plain floats at the nearest gridpoint.

    Heavy + synchronous (eccodes); call via asyncio.to_thread.
    """
    import os
    import tempfile

    import numpy as np
    import xarray as xr

    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(grib)

        def open_group(**filter_by_keys: Any) -> xr.Dataset:
            return xr.open_dataset(
                path,
                engine="cfgrib",
                decode_timedelta=True,
                backend_kwargs={"filter_by_keys": filter_by_keys, "indexpath": ""},
            )

        iso = open_group(typeOfLevel="isobaricInhPa")
        lat2d = iso["latitude"].values
        lon2d = iso["longitude"].values
        d2 = (lat2d - lat) ** 2 + (lon2d - (lon % 360.0)) ** 2
        yi, xi = np.unravel_index(np.argmin(d2), d2.shape)
        gp_lat = float(lat2d[yi, xi])
        gp_lon = float(lon2d[yi, xi])
        if gp_lon > 180.0:  # decoded RAP coords are 0-360 east
            gp_lon -= 360.0

        def col(ds: xr.Dataset, var: str) -> list[float]:
            return [float(v) for v in ds[var].values[..., yi, xi].ravel()]

        def pt(ds: xr.Dataset, var: str) -> float:
            return float(ds[var].values[yi, xi])

        sfc = open_group(typeOfLevel="surface")
        m2 = open_group(typeOfLevel="heightAboveGround", level=2)
        m10 = open_group(typeOfLevel="heightAboveGround", level=10)
        hlcy = open_group(typeOfLevel="heightAboveGroundLayer", shortName="hlcy")
        ustm = open_group(typeOfLevel="heightAboveGroundLayer", shortName="ustm")
        vstm = open_group(typeOfLevel="heightAboveGroundLayer", shortName="vstm")
        ml = open_group(typeOfLevel="pressureFromGroundLayer", shortName="cape")
        mlcin = open_group(typeOfLevel="pressureFromGroundLayer", shortName="cin")

        hlcy_levels = [float(v) for v in hlcy["heightAboveGroundLayer"].values]
        hlcy_vals = col(hlcy, "hlcy")
        srh_by_depth = dict(zip(hlcy_levels, hlcy_vals, strict=True))

        ml_levels = [float(v) for v in ml["pressureFromGroundLayer"].values]
        ml_cape_by_layer = dict(zip(ml_levels, col(ml, "cape"), strict=True))
        ml_cin_by_layer = dict(zip(ml_levels, col(mlcin, "cin"), strict=True))

        profile = {
            "cycle_utc": str(iso["time"].values)[:16] + "Z",
            "valid_utc": str(iso["valid_time"].values)[:16] + "Z",
            "grid_point": {
                "lat": round(gp_lat, 3),
                "lon": round(gp_lon, 3),
                "distance_km": round(haversine_km(lat, lon, gp_lat, gp_lon), 1),
            },
            "pressure_hpa": [float(v) for v in iso["isobaricInhPa"].values],
            "temp_k": col(iso, "t"),
            "rh_pct": col(iso, "r"),
            "height_gpm": col(iso, "gh"),
            "u_ms": col(iso, "u"),
            "v_ms": col(iso, "v"),
            "surface_pressure_pa": pt(sfc, "sp"),
            "elevation_m": pt(sfc, "orog"),
            "t2m_k": pt(m2, "t2m"),
            "d2m_k": pt(m2, "d2m"),
            "u10_ms": pt(m10, "u10"),
            "v10_ms": pt(m10, "v10"),
            "model_reported": {
                "sfc_cape_jkg": pt(sfc, "cape"),
                "sfc_cin_jkg": pt(sfc, "cin"),
                "ml_cape_90mb_jkg": ml_cape_by_layer.get(9000.0),
                "ml_cin_90mb_jkg": ml_cin_by_layer.get(9000.0),
                "srh_0_1km_m2s2": srh_by_depth.get(1000.0),
                "srh_0_3km_m2s2": srh_by_depth.get(3000.0),
                "storm_motion_u_ms": pt(ustm, "ustm"),
                "storm_motion_v_ms": pt(vstm, "vstm"),
            },
        }
        for ds in (iso, sfc, m2, m10, hlcy, ustm, vstm, ml, mlcin):
            ds.close()
        return profile
    finally:
        os.unlink(path)


async def fetch_profile(lat: float, lon: float) -> dict[str, Any]:
    grib, cycle = await fetch_subset(lat, lon)
    profile = await asyncio.to_thread(_decode_profile_sync, grib, lat, lon)
    profile["requested_cycle"] = cycle
    return profile


async def _fetch_subset_at(
    lat: float, lon: float, date_str: str, hour: int, fhr: int
) -> bytes:
    """Fetch one (cycle, forecast-hour) subset; raise if that file isn't posted."""
    key = f"rap:{lat:.2f},{lon:.2f}:{date_str}{hour:02d}f{fhr:02d}"

    async def fetch() -> bytes:
        resp = await client().get(
            FILTER_URL,
            params=_filter_params(date_str, hour, lat, lon, fhr),
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        if resp.status_code == 200 and resp.content[:4] == b"GRIB":
            return resp.content
        raise RuntimeError(
            f"RAP f{fhr:02d} for cycle {date_str} t{hour:02d}z unavailable (HTTP {resp.status_code})"
        )

    return await CACHE.get_or_fetch(key, TTL_RAP, fetch)


async def fetch_forecast_profiles(
    lat: float, lon: float, fhrs: list[int] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Decode a forecast series anchored to a SINGLE consistent RAP cycle.

    Walks back to the latest cycle for which ALL requested forecast hours are
    posted (fXX appear over ~10 min and out of order on a fresh cycle), so the
    trend is internally consistent. Returns (cycle_iso, [profile, ...]).
    """
    fhrs = fhrs or FORECAST_HOURS
    if any(f > MAX_FORECAST_HOUR for f in fhrs):
        raise ValueError(f"forecast hours must be <= f{MAX_FORECAST_HOUR:02d}")

    now = datetime.now(UTC)
    last_error: Exception | None = None
    for back in range(MAX_CYCLE_LOOKBACK_H + 1):
        cycle = now - timedelta(hours=back)
        date_str, hour = cycle.strftime("%Y%m%d"), cycle.hour
        try:
            gribs = await asyncio.gather(
                *(_fetch_subset_at(lat, lon, date_str, hour, f) for f in fhrs)
            )
        except Exception as exc:  # any missing fhr => cycle incomplete, walk back
            last_error = exc
            continue
        profiles = []
        for fhr, grib in zip(fhrs, gribs, strict=True):
            profile = await asyncio.to_thread(_decode_profile_sync, grib, lat, lon)
            profile["forecast_hour"] = fhr
            profiles.append(profile)
        return cycle.strftime("%Y-%m-%dT%H:00Z"), profiles

    raise RuntimeError(
        f"No complete RAP forecast cycle within {MAX_CYCLE_LOOKBACK_H} h: {last_error}"
    )
