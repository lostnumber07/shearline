"""MRMS severe products from AWS Open Data (s3://noaa-mrms-pds), anonymous.

Empirically verified 2026-06-10:
- Keys: CONUS/{Product}_00.50/{YYYYMMDD}/MRMS_{Product}_00.50_{YYYYMMDD-HHMMSS}.grib2.gz,
  ~2-minute cadence; timestamps have irregular seconds, so the latest file is
  found by listing the date folder and taking the lexicographic max.
- cfgrib decodes every MRMS field as variable 'unknown' (NOAA local tables);
  the product identity comes from the S3 key.
- Grids: lats DESCENDING (54.995 -> 20.005), lons 0-360 east. MESH/VIL/
  reflectivity are 0.01 deg (3500x7000); RotationTrack* are 0.005 deg.
- Sentinels: MESH/VIL use -3 (no coverage) / -1 (missing); reflectivity uses
  -999/-99 with a real-data floor of -15.5 dBZ; RotationTrack encodes
  missing/no-rotation as 0. Rotation values are in 0.001/s.
"""

import asyncio
import gzip
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import CACHE, TTL_MRMS
from ..geo import distance_bearing

BUCKET = "noaa-mrms-pds"

PRODUCTS: dict[str, dict[str, Any]] = {
    "mesh_max_60min": {
        "prefix": "CONUS/MESH_Max_60min_00.50/",
        "label": "Maximum Estimated Size of Hail (MESH), 60-minute max",
        "window": "last 60 minutes",
        "min_valid": 0.001,  # values are mm; anything > 0 is a real hail signal
    },
    "rotation_track_ml_60min": {
        "prefix": "CONUS/RotationTrackML60min_00.50/",
        "label": "Mid-level (3-6 km AGL) rotation track, 60-minute max azimuthal shear",
        "window": "last 60 minutes",
        "min_valid": 0.001,
    },
    "rotation_track_ll_60min": {
        "prefix": "CONUS/RotationTrack60min_00.50/",
        "label": "Low-level (0-2 km AGL) rotation track, 60-minute max azimuthal shear",
        "window": "last 60 minutes",
        "min_valid": 0.001,
    },
    "vil": {
        "prefix": "CONUS/VIL_00.50/",
        "label": "Vertically integrated liquid (instantaneous)",
        "window": "latest scan",
        "min_valid": 0.001,
    },
    "composite_reflectivity": {
        "prefix": "CONUS/MergedReflectivityQCComposite_00.50/",
        "label": "Composite reflectivity, QC'd (instantaneous)",
        "window": "latest scan",
        "min_valid": -30.0,  # real data floor is -15.5 dBZ; sentinels are -99/-999
    },
}

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config

        _s3_client = boto3.client(
            "s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1"
        )
    return _s3_client


def _latest_gz_sync(prefix: str) -> tuple[str, bytes]:
    """Newest object under prefix (today's folder, falling back to yesterday)."""
    s3 = _s3()
    now = datetime.now(UTC)
    for day in (now, now - timedelta(days=1)):
        day_prefix = f"{prefix}{day.strftime('%Y%m%d')}/"
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=day_prefix)
        keys = [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".grib2.gz")]
        while resp.get("IsTruncated"):
            resp = s3.list_objects_v2(
                Bucket=BUCKET,
                Prefix=day_prefix,
                ContinuationToken=resp["NextContinuationToken"],
            )
            keys.extend(
                o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".grib2.gz")
            )
        if keys:
            key = max(keys)
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            return key, body
    raise RuntimeError(f"No MRMS objects found under {prefix} for today or yesterday")


def _sample_sync(
    gz_bytes: bytes, lat: float, lon: float, radius_km: float, min_valid: float
) -> dict[str, Any]:
    """Max value within radius_km of the point, with location and valid time."""
    import math

    import numpy as np
    import xarray as xr

    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.decompress(gz_bytes))
        ds = xr.open_dataset(
            path,
            engine="cfgrib",
            decode_timedelta=True,
            backend_kwargs={"indexpath": ""},
        )
        da = ds["unknown"]
        valid_utc = str(ds["valid_time"].values)[:16] + "Z"

        lon360 = lon % 360.0
        dlat = radius_km / 111.195 * 1.05
        dlon = radius_km / (111.195 * math.cos(math.radians(lat))) * 1.05
        # latitudes are descending, so the slice runs north -> south
        sub = da.sel(
            latitude=slice(lat + dlat, lat - dlat),
            longitude=slice(lon360 - dlon, lon360 + dlon),
        )
        vals = sub.values
        lats = sub["latitude"].values
        lons = sub["longitude"].values
        ds.close()

        if vals.size == 0:
            return {"max": None, "valid_utc": valid_utc, "cells_in_radius": 0}

        lat2d, lon2d = np.meshgrid(lats, lons, indexing="ij")
        # vectorized haversine
        phi1 = math.radians(lat)
        phi2 = np.radians(lat2d)
        dphi = np.radians(lat2d - lat)
        dlam = np.radians(lon2d - lon360)
        a = np.sin(dphi / 2) ** 2 + math.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
        dist_km = 2 * 6371.0 * np.arcsin(np.sqrt(a))

        mask = (dist_km <= radius_km) & (vals >= min_valid)
        n_cells = int(mask.sum())
        if n_cells == 0:
            return {"max": None, "valid_utc": valid_utc, "cells_in_radius": 0}

        masked = np.where(mask, vals, -np.inf)
        yi, xi = np.unravel_index(np.argmax(masked), masked.shape)
        max_lat = float(lat2d[yi, xi])
        max_lon = float(lon2d[yi, xi]) - 360.0
        return {
            "max": float(vals[yi, xi]),
            "valid_utc": valid_utc,
            "cells_in_radius": n_cells,
            "max_location": distance_bearing(lat, lon, max_lat, max_lon),
        }
    finally:
        os.unlink(path)


async def sample_product(
    product: str, lat: float, lon: float, radius_km: float
) -> dict[str, Any]:
    spec = PRODUCTS[product]

    async def fetch_bytes() -> tuple[str, bytes]:
        return await asyncio.to_thread(_latest_gz_sync, spec["prefix"])

    key, gz = await CACHE.get_or_fetch(f"mrms:{product}", TTL_MRMS, fetch_bytes)

    async def sample() -> dict[str, Any]:
        out = await asyncio.to_thread(
            _sample_sync, gz, lat, lon, radius_km, spec["min_valid"]
        )
        out["product"] = spec["label"]
        out["window"] = spec["window"]
        out["s3_key"] = key
        return out

    return await CACHE.get_or_fetch(
        f"mrms-sample:{product}:{lat:.2f},{lon:.2f},{radius_km:.0f}:{key}",
        TTL_MRMS,
        sample,
    )


def shape_results(samples: dict[str, dict | None]) -> dict[str, Any]:
    """Convert raw per-product samples into the tool's data payload."""
    data: dict[str, Any] = {}

    mesh = samples.get("mesh_max_60min")
    if mesh is not None:
        mm = mesh.get("max")
        data["hail_mesh"] = {
            "max_mesh_mm": round(mm, 1) if mm else None,
            "max_mesh_in": round(mm / 25.4, 2) if mm else None,
            **{k: mesh.get(k) for k in ("valid_utc", "window", "max_location", "cells_in_radius")},
        }

    for name, out_key, level_name in (
        ("rotation_track_ml_60min", "rotation_midlevel", "3-6 km AGL"),
        ("rotation_track_ll_60min", "rotation_lowlevel", "0-2 km AGL"),
    ):
        rot = samples.get(name)
        if rot is not None:
            raw = rot.get("max")
            data[out_key] = {
                "max_azimuthal_shear_s1": round(raw * 0.001, 4) if raw else None,
                "layer": level_name,
                **{k: rot.get(k) for k in ("valid_utc", "window", "max_location", "cells_in_radius")},
            }

    vil = samples.get("vil")
    if vil is not None:
        data["vil"] = {
            "max_vil_kg_m2": round(vil["max"], 1) if vil.get("max") else None,
            **{k: vil.get(k) for k in ("valid_utc", "window", "max_location", "cells_in_radius")},
        }

    refl = samples.get("composite_reflectivity")
    if refl is not None:
        data["composite_reflectivity"] = {
            "max_dbz": round(refl["max"], 1) if refl.get("max") is not None else None,
            **{k: refl.get(k) for k in ("valid_utc", "window", "max_location", "cells_in_radius")},
        }

    return data


def interpret(data: dict[str, Any], radius_km: float) -> str:
    """Analyst sentences for the MRMS sample."""
    refl = (data.get("composite_reflectivity") or {}).get("max_dbz")
    mesh_in = (data.get("hail_mesh") or {}).get("max_mesh_in")
    rot_ml = (data.get("rotation_midlevel") or {}).get("max_azimuthal_shear_s1")
    rot_ll = (data.get("rotation_lowlevel") or {}).get("max_azimuthal_shear_s1")
    vil = (data.get("vil") or {}).get("max_vil_kg_m2")

    s: list[str] = []
    if refl is None or refl < 35:
        s.append(
            f"Radar is quiet within {radius_km:.0f} km: "
            + (
                "no echoes of consequence"
                if refl is None
                else f"max composite reflectivity only {refl} dBZ"
            )
            + ", so no active convection is near the point."
        )
    elif refl < 50:
        loc = (data.get("composite_reflectivity") or {}).get("max_location") or {}
        s.append(
            f"Showers/storms are in range: max composite reflectivity {refl} dBZ "
            f"about {loc.get('distance_km', '?')} km {loc.get('direction', '')} of the point — "
            "convective but below typical severe cores."
        )
    else:
        loc = (data.get("composite_reflectivity") or {}).get("max_location") or {}
        s.append(
            f"Strong convection is nearby: {refl} dBZ max composite reflectivity "
            f"{loc.get('distance_km', '?')} km {loc.get('direction', '')} of the point"
            + (" — a core intense enough for hail." if refl >= 60 else ".")
        )

    if mesh_in:
        sev = (
            "significant (2-inch+) hail"
            if mesh_in >= 2
            else "severe-caliber hail"
            if mesh_in >= 1
            else "sub-severe hail"
        )
        loc = (data.get("hail_mesh") or {}).get("max_location") or {}
        s.append(
            f"MESH peaked at {mesh_in}\" ({(data.get('hail_mesh') or {}).get('max_mesh_mm')} mm) "
            f"in the last hour, {loc.get('distance_km', '?')} km {loc.get('direction', '')} — {sev}."
        )

    rot_best = max(filter(None, [rot_ml or 0, rot_ll or 0]), default=0)
    if rot_best >= 0.004:
        which = "low-level" if (rot_ll or 0) >= (rot_ml or 0) else "mid-level"
        strength = (
            "intense" if rot_best >= 0.012 else "strong" if rot_best >= 0.008 else "notable"
        )
        s.append(
            f"Rotation tracks show a {strength} {which} circulation "
            f"(max azimuthal shear {rot_best} /s in the last hour) — "
            + (
                "mesocyclone-caliber rotation worth close attention."
                if rot_best >= 0.008
                else "worth monitoring for organization."
            )
        )

    if vil and vil >= 35:
        s.append(
            f"VIL of {vil} kg/m2 indicates heavy hydrometeor loading, consistent with "
            "a hail-capable updraft."
        )
    return " ".join(s)
