"""GOES GLM lightning (GLM-L2-LCFA) from NOAA's anonymous GOES Open Data S3.

Empirically verified 2026-06-13:
- Operational GOES-East bucket is noaa-goes19 (G19); West is noaa-goes18. The
  old noaa-goes16/17 still exist but carry NO current GLM data — do NOT hardcode
  a satellite from memory. GOES-East sees the entire CONUS, so it is used here.
- Keys: GLM-L2-LCFA/{YYYY}/{DDD}/{HH}/OR_GLM-L2-LCFA_G19_s{YYYYDDDHHMMSSf}_e..._c....nc
  where the s-tag is the coverage START (year, day-of-year, HH:MM:SS, tenths).
  Granules are ~20 s (~180/hour); keys sort chronologically.
- File is netCDF4/HDF5 → decoded with xarray engine='h5netcdf' (h5netcdf + h5py
  are required deps; the project's cfgrib/scipy backends cannot read it).
- flash_lat/flash_lon are float32 coordinates; longitude is -180..180 (negative
  = Western Hemisphere). flash_time_offset_of_first_event is datetime64[ns].
- GLM detects TOTAL lightning (in-cloud + cloud-to-ground), not CG only.
"""

import asyncio
import io
import math
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import CACHE, TTL_MRMS
from ..geo import distance_bearing

# Verified operational GOES-East GLM bucket (2026-06-13). The canary watches for
# this going empty (a satellite transition would surface as persistent failure).
BUCKET_EAST = "noaa-goes19"
SATELLITE_EAST = "GOES-East (G19)"
PRODUCT_PREFIX = "GLM-L2-LCFA"

# Bound the work: at ~180 granules/hour a long window is a lot of small files.
MAX_GRANULES = 120
_FETCH_SEMAPHORE = asyncio.Semaphore(8)

_START_RE = re.compile(r"_s(\d{14})_")

_s3_client = None
_s3_lock = threading.Lock()


def _s3():
    # boto3 client CREATION is not thread-safe; guard the lazy init.
    global _s3_client
    with _s3_lock:
        if _s3_client is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config

            _s3_client = boto3.Session().client(
                "s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1"
            )
    return _s3_client


def _parse_start(key: str) -> datetime | None:
    m = _START_RE.search(key)
    if not m:
        return None
    s = m.group(1)  # YYYYDDDHHMMSSf
    try:
        base = datetime.strptime(s[:13], "%Y%j%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return base + timedelta(seconds=int(s[13]) / 10.0)


def _hour_prefixes(start: datetime, end: datetime) -> list[str]:
    cur = start.replace(minute=0, second=0, microsecond=0)
    prefixes = []
    while cur <= end:
        prefixes.append(f"{PRODUCT_PREFIX}/{cur.strftime('%Y/%j/%H')}/")
        cur += timedelta(hours=1)
    return prefixes


def _list_window_keys_sync(bucket: str, start: datetime, end: datetime) -> list[str]:
    s3 = _s3()
    keys: list[str] = []
    for prefix in _hour_prefixes(start, end):
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".nc"):
                continue
            st = _parse_start(key)
            if st is not None and start <= st <= end:
                keys.append(key)
    return sorted(keys)


def _sample_granule_sync(
    nc_bytes: bytes, lat: float, lon: float, radius_km: float
) -> list[dict[str, Any]]:
    """Flashes within radius_km of the point in one granule."""
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(io.BytesIO(nc_bytes), engine="h5netcdf")
    try:
        if ds.sizes.get("number_of_flashes", 0) == 0:
            return []
        flat = ds["flash_lat"].values.astype(float)
        flon = ds["flash_lon"].values.astype(float)
        times = ds["flash_time_offset_of_first_event"].values  # datetime64[ns]

        phi1 = math.radians(lat)
        phi2 = np.radians(flat)
        dphi = np.radians(flat - lat)
        dlam = np.radians(flon - lon)
        a = np.sin(dphi / 2) ** 2 + math.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
        dist_km = 2 * 6371.0 * np.arcsin(np.sqrt(a))

        flashes = []
        for i in np.where(dist_km <= radius_km)[0]:
            db = distance_bearing(lat, lon, float(flat[i]), float(flon[i]))
            t = times[i]
            time_utc = None if np.isnat(t) else np.datetime_as_string(t, unit="s") + "Z"
            flashes.append({**db, "time_utc": time_utc})
        return flashes
    finally:
        ds.close()


async def _sample_key(key: str, lat: float, lon: float, radius_km: float) -> list[dict]:
    async def fetch() -> list[dict]:
        async with _FETCH_SEMAPHORE:
            body = await asyncio.to_thread(
                lambda: _s3().get_object(Bucket=BUCKET_EAST, Key=key)["Body"].read()
            )
            return await asyncio.to_thread(_sample_granule_sync, body, lat, lon, radius_km)

    # Granules are immutable, so the per-granule sample is safely cacheable.
    return await CACHE.get_or_fetch(
        f"glm:{key}:{lat:.2f},{lon:.2f},{radius_km:.0f}", TTL_MRMS, fetch
    )


async def fetch_lightning(
    lat: float, lon: float, radius_km: float, minutes: float
) -> dict[str, Any]:
    now = datetime.now(UTC)
    start = now - timedelta(minutes=minutes)
    keys = await asyncio.to_thread(_list_window_keys_sync, BUCKET_EAST, start, now)
    truncated = len(keys) > MAX_GRANULES
    if truncated:
        keys = keys[-MAX_GRANULES:]  # keep the most recent granules

    samples = await asyncio.gather(*(_sample_key(k, lat, lon, radius_km) for k in keys))
    flashes = [f for granule in samples for f in granule]

    nearest = min(flashes, key=lambda f: f["distance_km"], default=None)
    most_recent = max(
        (f["time_utc"] for f in flashes if f["time_utc"]), default=None
    )
    return {
        "satellite": SATELLITE_EAST,
        "window_minutes": minutes,
        "radius_km": radius_km,
        "granules_used": len(keys),
        "window_truncated": truncated,
        "flash_count": len(flashes),
        "flashes_per_min": round(len(flashes) / minutes, 1) if minutes else None,
        "nearest_strike": (
            {
                "distance_km": nearest["distance_km"],
                "bearing_deg": nearest["bearing_deg"],
                "direction": nearest["direction"],
                "time_utc": nearest["time_utc"],
            }
            if nearest
            else None
        ),
        "most_recent_strike_utc": most_recent,
    }


def interpret(data: dict[str, Any]) -> str:
    """Tiered outdoor-safety interpretation."""
    count = data.get("flash_count") or 0
    minutes = data.get("window_minutes")
    radius = data.get("radius_km")
    provenance = "(GOES-East GLM total lightning — in-cloud and cloud-to-ground; ~20-40 s latency.)"
    if count == 0:
        return (
            f"No lightning detected within {radius:.0f} km of the point in the last "
            f"{minutes:.0f} minutes. No nearby electrical activity. {provenance}"
        )
    nearest = data.get("nearest_strike") or {}
    d = nearest.get("distance_km")
    rate = data.get("flashes_per_min")
    where = f"{d} km {nearest.get('direction', '')}".strip()
    base = (
        f"{count} lightning flash(es) within {radius:.0f} km in the last {minutes:.0f} min "
        f"({rate}/min); nearest {where} at {nearest.get('time_utc')}."
    )
    if d is not None and d <= 10:
        tier = (
            " DANGER — strikes are essentially overhead. Anyone outdoors should already be in "
            "a fully enclosed building or hard-topped vehicle (the 30-30 rule applies)."
        )
    elif d is not None and d <= 16:
        tier = (
            " Lightning is within striking distance (~10 miles). Suspend outdoor activity and "
            "seek shelter now; do not resume until 30 minutes after the last strike."
        )
    else:
        tier = (
            " Lightning is in the area but not yet within strike range. Monitor closely and be "
            "ready to move indoors if it approaches."
        )
    return base + tier + " " + provenance
