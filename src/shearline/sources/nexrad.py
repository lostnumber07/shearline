"""NEXRAD Level 2 latest-volume metadata (stretch tool, no rendering).

Empirically verified 2026-06-10:
- The old s3://noaa-nexrad-level2 bucket is DEAD (403 since 2025-09);
  the live archive is s3://unidata-nexrad-level2 (anonymous, us-east-1).
- Keys: YYYY/MM/DD/SITE/SITEYYYYMMDD_HHMMSS_V06 (WSR-88D; _V08 = TDWR).
  Sidecar *_MDM files are Model Data Messages — filter them out. Objects
  appear only once a volume completes; latest = lexicographic max.
- Station table: NCEI HOMR nexrad-stations.txt, fixed-width; classify by the
  STNTYPE column (TJUA is a T-prefixed WSR-88D, so never trust the ICAO).
- MetPy's Level2File parses a full volume in ~2 s; no extra deps needed.
"""

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

from ..cache import CACHE, TTL_MRMS
from ..geo import distance_bearing, haversine_km

BUCKET = "unidata-nexrad-level2"

VCP_MEANINGS = {
    12: "severe-weather precipitation mode (fast low-level updates)",
    212: "severe-weather precipitation mode (fast low-level updates)",
    112: "precipitation mode with extended velocity range",
    215: "general precipitation mode",
    121: "legacy precipitation mode",
    31: "clear-air mode (long pulse)",
    32: "clear-air mode (short pulse)",
    35: "clear-air mode",
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


@lru_cache(maxsize=1)
def load_stations() -> list[dict[str, Any]]:
    """Parse the bundled fixed-width HOMR station table (WSR-88D only)."""
    text = (
        resources.files("shearline")
        .joinpath("data/nexrad-stations.txt")
        .read_text(encoding="utf-8", errors="replace")
    )
    lines = text.splitlines()
    stations = []
    for line in lines[2:]:
        if len(line) < 140:
            continue
        stntype = line[140:190].strip()
        if "NEXRAD" not in stntype:
            continue
        name = line[20:50].strip()
        # Skip ROC test/redundant radars (e.g. KCRI "ROC FAA REDUNDANT RDA");
        # they sit next to real sites and upload non-operational volumes.
        if "RDA" in name or "ROC " in name:
            continue
        try:
            stations.append(
                {
                    "id": line[9:13].strip(),
                    "name": line[20:50].strip().title(),
                    "state": line[72:74].strip(),
                    "lat": float(line[106:115]),
                    "lon": float(line[116:126]),
                    "elev_ft": float(line[127:133]),
                }
            )
        except ValueError:
            continue
    return stations


def nearest_sites(lat: float, lon: float, n: int = 3) -> list[dict[str, Any]]:
    sites = sorted(
        load_stations(), key=lambda s: haversine_km(lat, lon, s["lat"], s["lon"])
    )[:n]
    return [
        {**s, **distance_bearing(lat, lon, s["lat"], s["lon"])} for s in sites
    ]


def _latest_volume_sync(site_id: str) -> tuple[str, bytes] | None:
    s3 = _s3()
    now = datetime.now(UTC)
    for day in (now, now - timedelta(days=1)):
        prefix = f"{day.strftime('%Y/%m/%d')}/{site_id}/"
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        while resp.get("IsTruncated"):
            resp = s3.list_objects_v2(
                Bucket=BUCKET, Prefix=prefix, ContinuationToken=resp["NextContinuationToken"]
            )
            keys.extend(o["Key"] for o in resp.get("Contents", []))
        volumes = [k for k in keys if k.endswith("_V06")]
        if volumes:
            key = max(volumes)
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            return key, body
    return None


def _parse_volume_sync(volume: bytes, site_elev_ft: float) -> dict[str, Any]:
    """Extract VCP, max reflectivity, and a coarse echo top with MetPy."""
    import io

    import numpy as np
    from metpy.io import Level2File

    f = Level2File(io.BytesIO(volume))
    vcp = None
    if getattr(f, "vcp_info", None) is not None:
        vcp = getattr(f.vcp_info, "num", None) or getattr(f.vcp_info, "pattern_number", None)

    max_dbz = None
    max_meta: dict[str, Any] = {}
    echo_top_km: float | None = None
    echo_top_meta: dict[str, Any] = {}
    effective_earth_km = 6371.0 * 4.0 / 3.0

    for sweep in f.sweeps:
        for ray in sweep:
            hdr = ray[0]
            blocks = ray[4] if len(ray) > 4 else {}
            if b"REF" not in blocks:
                continue
            ref_hdr, ref_data = blocks[b"REF"][0], blocks[b"REF"][1]
            data = np.asarray(ref_data, dtype=float)
            if data.size == 0 or np.all(np.isnan(data)):
                continue
            gate_width = float(ref_hdr.gate_width)
            first_gate = float(ref_hdr.first_gate)
            if first_gate > 100:  # meters, not km
                first_gate /= 1000.0
                gate_width /= 1000.0
            ranges_km = first_gate + np.arange(data.size) * gate_width
            elev = float(getattr(hdr, "el_angle", 0.0))
            az = float(getattr(hdr, "az_angle", 0.0))

            finite = np.nan_to_num(data, nan=-999.0)
            i = int(np.argmax(finite))
            if finite[i] > -900 and (max_dbz is None or finite[i] > max_dbz):
                max_dbz = float(finite[i])
                max_meta = {
                    "range_km": round(float(ranges_km[i]), 1),
                    "azimuth_deg": round(az),
                    "elevation_deg": round(elev, 1),
                }

            echoes = finite >= 18.0
            if echoes.any():
                r = ranges_km[echoes]
                h = r * math.sin(math.radians(elev)) + (r**2) / (2 * effective_earth_km)
                j = int(np.argmax(h))
                top = float(h[j])
                if echo_top_km is None or top > echo_top_km:
                    echo_top_km = top
                    echo_top_meta = {"range_km": round(float(r[j]), 1), "azimuth_deg": round(az)}

    result: dict[str, Any] = {
        "vcp": int(vcp) if vcp else None,
        "vcp_meaning": VCP_MEANINGS.get(int(vcp), "unrecognized pattern") if vcp else None,
        "sweep_count": len(f.sweeps),
        "max_reflectivity": (
            {"dbz": round(max_dbz, 1), **max_meta} if max_dbz is not None else None
        ),
        "echo_top_estimate": (
            {
                "km_agl": round(echo_top_km, 1),
                "kft_agl": round(echo_top_km * 3.28084, 1),
                **echo_top_meta,
                "threshold_dbz": 18,
                "method": "coarse 4/3-earth beam-height estimate from highest sweep with >=18 dBZ",
            }
            if echo_top_km is not None
            else None
        ),
    }
    return result


async def radar_snapshot(lat: float, lon: float) -> dict[str, Any]:
    sites = nearest_sites(lat, lon, n=3)
    for site in sites:
        found = await CACHE.get_or_fetch(
            f"nexrad-latest:{site['id']}",
            TTL_MRMS,
            lambda s=site: asyncio.to_thread(_latest_volume_sync, s["id"]),
        )
        if found is None:
            continue  # site silent today — try the next nearest
        key, volume = found
        try:
            parsed = await asyncio.to_thread(_parse_volume_sync, volume, site["elev_ft"])
        except Exception:
            continue  # malformed volume — try the next nearest site
        ts = re.search(r"(\d{8})_(\d{6})_V06$", key)
        start_utc = None
        if ts:
            start_utc = (
                f"{ts.group(1)[:4]}-{ts.group(1)[4:6]}-{ts.group(1)[6:]}T"
                f"{ts.group(2)[:2]}:{ts.group(2)[2:4]}:{ts.group(2)[4:]}Z"
            )
        return {
            "site": {
                "id": site["id"],
                "name": site["name"],
                "state": site["state"],
                "distance_km": site["distance_km"],
                "bearing_deg": site["bearing_deg"],
                "direction": site["direction"],
            },
            "volume": {"s3_key": key, "scan_start_utc": start_utc},
            **parsed,
        }
    raise RuntimeError(
        f"No recent Level 2 volumes found for the {len(sites)} nearest WSR-88D sites "
        f"({', '.join(s['id'] for s in sites)})."
    )


def interpret(data: dict[str, Any]) -> str:
    site = data["site"]
    vcp = data.get("vcp")
    maxref = data.get("max_reflectivity")
    top = data.get("echo_top_estimate")
    s = [
        f"Nearest radar is {site['id']} ({site['name']}, {site['state']}), "
        f"{site['distance_km']} km {site['direction']} of the point, "
        f"scan started {data['volume']['scan_start_utc']}."
    ]
    if vcp:
        s.append(f"It is running VCP {vcp} — {data['vcp_meaning']}.")
    if maxref is None:
        s.append("No reflectivity echoes of consequence in the volume.")
    else:
        s.append(
            f"Strongest echo in the volume: {maxref['dbz']} dBZ at {maxref['range_km']} km "
            f"range, azimuth {maxref['azimuth_deg']} deg (elevation {maxref['elevation_deg']} deg)."
        )
        if top:
            s.append(
                f"Coarse 18-dBZ echo top estimate: {top['km_agl']} km "
                f"({top['kft_agl']} kft) AGL at {top.get('range_km', '?')} km range, "
                f"azimuth {top.get('azimuth_deg', '?')} deg — note this can be a distant "
                "storm elsewhere in the volume, not necessarily near the queried point."
            )
        if vcp in (31, 32, 35):
            s.append(
                "Note the radar is in clear-air mode, so operators are not yet treating "
                "nearby echoes as convective."
            )
    return " ".join(s)
