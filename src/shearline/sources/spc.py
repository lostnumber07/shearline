"""SPC convective outlook GeoJSON layers, point-in-polygon risk lookup.

Empirically verified 2026-06-10:
- Layer roster (https://www.spc.noaa.gov/products/outlook/{name}.lyr.geojson):
  day1/day2: cat, torn, wind, hail + cigtorn/cigwind/cighail;
  day3: cat, prob (total severe), cigprob.
- The old sig* layers still return HTTP 200 but are FROZEN since 2026-03 —
  SPC replaced them with cig* (Conditional Intensity Groups). Guard against
  stale layers by comparing each layer's VALID to the categorical layer's.
- Categorical DN map: 2=TSTM 3=MRGL 4=SLGT 5=ENH 6=MDT 8=HIGH (7 is skipped).
- Probability contours: LABEL is a fraction string ('0.05'); CIG features in
  torn/wind/hail layers share DN=2 with the 2% contour — filter by LABEL.
- .lyr layers are nested ("wedding cake"): a point in ENH is also inside
  SLGT/MRGL/TSTM polygons — always take the MAX containing category.
"""

import re
from typing import Any

from shapely.geometry import Point, shape

from ..cache import TTL_OUTLOOK
from ..fetch import get_json

BASE = "https://www.spc.noaa.gov/products/outlook"

CAT_BY_DN = {2: "TSTM", 3: "MRGL", 4: "SLGT", 5: "ENH", 6: "MDT", 8: "HIGH"}

CAT_DESCRIPTIONS = {
    None: "No thunderstorms forecast",
    "TSTM": "General thunderstorms — lightning, brief heavy rain; severe not expected",
    "MRGL": "Marginal risk (1/5) — isolated severe storms possible, limited in intensity",
    "SLGT": "Slight risk (2/5) — scattered severe storms possible",
    "ENH": "Enhanced risk (3/5) — numerous severe storms possible, more persistent/widespread",
    "MDT": "Moderate risk (4/5) — widespread severe storms likely, some intense",
    "HIGH": "High risk (5/5) — severe weather outbreak expected",
}

_PROB_RE = re.compile(r"^0\.\d+$")


def _cig_rank(label: str) -> int:
    try:
        return int(label[3:])
    except ValueError:
        return 0

LAYERS_BY_DAY: dict[int, dict[str, str]] = {
    1: {
        "categorical": "day1otlk_cat",
        "tornado": "day1otlk_torn",
        "wind": "day1otlk_wind",
        "hail": "day1otlk_hail",
        "tornado_cig": "day1otlk_cigtorn",
        "wind_cig": "day1otlk_cigwind",
        "hail_cig": "day1otlk_cighail",
    },
    2: {
        "categorical": "day2otlk_cat",
        "tornado": "day2otlk_torn",
        "wind": "day2otlk_wind",
        "hail": "day2otlk_hail",
        "tornado_cig": "day2otlk_cigtorn",
        "wind_cig": "day2otlk_cigwind",
        "hail_cig": "day2otlk_cighail",
    },
    3: {
        "categorical": "day3otlk_cat",
        "total_severe": "day3otlk_prob",
        "total_severe_cig": "day3otlk_cigprob",
    },
}


async def fetch_layer(name: str) -> dict:
    return await get_json(f"{BASE}/{name}.lyr.geojson", ttl=TTL_OUTLOOK)


def _containing_features(layer: dict, lat: float, lon: float) -> list[dict]:
    pt = Point(lon, lat)
    out = []
    for feat in layer.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        if geom.get("type") == "GeometryCollection" and not geom.get("geometries"):
            continue  # empty-layer placeholder feature (DN=0)
        props = feat.get("properties", {})
        if props.get("DN") == 0:
            continue
        if shape(geom).intersects(pt):
            out.append(feat)
    return out


def categorical_at_point(layer: dict, lat: float, lon: float) -> dict[str, Any]:
    best_dn = None
    for feat in _containing_features(layer, lat, lon):
        dn = feat["properties"].get("DN")
        if isinstance(dn, int) and (best_dn is None or dn > best_dn):
            best_dn = dn
    label = CAT_BY_DN.get(best_dn) if best_dn is not None else None
    return {
        "dn": best_dn,
        "label": label,
        "description": CAT_DESCRIPTIONS.get(label, CAT_DESCRIPTIONS[None]),
    }


def probability_at_point(layer: dict, lat: float, lon: float) -> dict[str, Any]:
    """Max probability contour containing the point, plus conditional-intensity
    group if a CIG feature also contains it. Legacy 'SIGN' treated as
    significant for archived layers."""
    best_pct: int | None = None
    cig: str | None = None
    sign = False
    for feat in _containing_features(layer, lat, lon):
        props = feat["properties"]
        label = str(props.get("LABEL") or "")
        if _PROB_RE.match(label):
            pct = int(round(float(label) * 100))
            if best_pct is None or pct > best_pct:
                best_pct = pct
        elif label.startswith("CIG"):
            if cig is None or _cig_rank(label) > _cig_rank(cig):
                cig = label
        elif label == "SIGN":
            sign = True
    return {"probability_pct": best_pct, "conditional_intensity": cig, "significant": sign}


def layer_valid(layer: dict) -> str | None:
    for feat in layer.get("features", []):
        valid = feat.get("properties", {}).get("VALID")
        if valid:
            return str(valid)
    return None


def layer_times(layer: dict) -> dict[str, Any]:
    for feat in layer.get("features", []):
        props = feat.get("properties", {})
        if props.get("VALID"):
            return {
                "valid_utc": str(props["VALID"]),
                "expire_utc": str(props["EXPIRE"]) if props.get("EXPIRE") else None,
                "issue_utc": str(props["ISSUE"]) if props.get("ISSUE") else None,
            }
    return {"valid_utc": None, "expire_utc": None, "issue_utc": None}


def is_stale(layer: dict, reference_valid: str | None) -> bool:
    """A hazard layer whose VALID doesn't match the same-day categorical
    layer's VALID is a frozen relic (e.g. retired sig* files) — discard it."""
    if reference_valid is None:
        return False
    valid = layer_valid(layer)
    return valid is not None and valid != reference_valid
