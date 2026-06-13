"""Live upstream canary — catches schema drift before it reaches users.

The offline test suite (tests/) mocks every upstream, so it can't notice when
NOAA renames a field, changes a sentinel, or moves a bucket — both of which have
already happened to this project. This script does the opposite: it hits every
real upstream once, for a fixed always-valid CONUS point, and asserts the SHAPE
of a healthy response (envelope keys, expected data fields, correct types) —
NOT specific values, and NOT "severe weather present" (most days are quiet).

Run:  uv run python scripts/canary.py
CI:   .github/workflows/canary.yml (daily cron + manual dispatch)

Exit codes:
  0  every source returned a healthy, correctly-shaped response
  1  at least one source drifted (missing/typed-wrong field) or stayed
     unreachable after a retry (a moved bucket / renamed file shows up here)

Transient network blips get one retry; a persistent failure is reported as
either DRIFT (envelope present, fields wrong) or UNREACHABLE (source degraded),
with a diff, so a human can tell a NOAA outage from a real breaking change.
"""

import asyncio
import sys
from typing import Any

from shearline import server
from shearline.envelope import DISCLAIMER

# A fixed point with year-round radar/model/MRMS coverage. Oklahoma City — chosen
# because it is always inside CONUS, always has a nearby WSR-88D (KTLX) and RAP
# grid coverage, but is quiet on most days, so the canary must assert SHAPE only.
OKC_LAT, OKC_LON = 35.47, -97.52

# Type aliases for readability in the spec.
NUM = (int, float)

# ---------------------------------------------------------------------------
# EXPECTED SCHEMA SPEC — the contract the canary enforces.
#
# For each tool: the call args, and the dotted `data.*` paths that MUST be
# present with the given type on a HEALTHY (non-degraded) run, regardless of
# whether any weather is happening. Containers (lists/dicts) are checked for
# presence + type, never for contents, so a quiet day passes. Every tool also
# implicitly requires the four envelope keys (checked generically below).
#
# `tolerate_degraded`: sources that legitimately may be empty/absent at a quiet
# point are still required NOT to degrade here — a moved bucket or renamed file
# manifests as persistent degradation, which is exactly the drift we want to
# catch. So all are False; the retry guards against a one-off network blip.
# ---------------------------------------------------------------------------
SPEC: dict[str, dict[str, Any]] = {
    "get_active_warnings": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            "data.point": dict,
            "data.radius_km": NUM,
            "data.point_inside_warning": bool,
            "data.warnings": list,
            "data.watches_at_point": list,
            "data.counts": dict,
        },
    },
    "get_spc_outlook": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON, "day": 1},
        "fields": {
            "data.day": int,
            "data.categorical": dict,
            "data.categorical.dn": (int, type(None)),
            "data.categorical.label": (str, type(None)),
            "data.probabilities": dict,
        },
    },
    "get_point_environment": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            "data.thermodynamics": dict,
            "data.thermodynamics.mlcape_jkg": (int, float, type(None)),
            "data.thermodynamics.sbcape_jkg": (int, float, type(None)),
            "data.kinematics": dict,
            "data.kinematics.bulk_shear_0_6km_kt": (int, float, type(None)),
            "data.kinematics.srh_0_1km_m2s2": (int, float, type(None)),
            "data.composites": dict,
            "data.composites.scp": (int, float, type(None)),
            "data.composites.stp_fixed_layer": (int, float, type(None)),
        },
    },
    "get_mrms_severe": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            # one dict per product; present only if the S3 fetch+decode worked,
            # so a missing key here means a moved prefix / broken decode.
            "data.hail_mesh": dict,
            "data.hail_mesh.max_mesh_mm": (int, float, type(None)),
            "data.rotation_midlevel": dict,
            "data.rotation_midlevel.max_azimuthal_shear_s1": (int, float, type(None)),
            "data.rotation_lowlevel": dict,
            "data.vil": dict,
            "data.composite_reflectivity": dict,
            "data.composite_reflectivity.max_dbz": (int, float, type(None)),
        },
    },
    "get_storm_reports": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            "data.counts": dict,
            "data.reports": list,
            "data.hours": NUM,
        },
    },
    "get_radar_snapshot": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            "data.site": dict,
            "data.site.id": str,
            "data.volume": dict,
            "data.sweep_count": (int, type(None)),
        },
    },
    "get_threat_brief": {
        "args": {"lat": OKC_LAT, "lon": OKC_LON},
        "fields": {
            "data.threat_level": str,
            "data.threat_logic": list,
            "data.hazards_ranked": list,
            "data.attention_window": dict,
        },
    },
}

THREAT_LEVELS = {"none", "marginal", "elevated", "significant", "extreme"}


def _dig(obj: Any, path: str) -> tuple[bool, Any]:
    """Walk a dotted path; return (found, value)."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _check_envelope(name: str, env: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(env, dict):
        return [f"{name}: response is {type(env).__name__}, expected dict envelope"]
    for key, typ in (("data", dict), ("interpretation", str), ("degraded", list)):
        if key not in env:
            problems.append(f"{name}: envelope missing '{key}'")
        elif not isinstance(env[key], typ):
            got = type(env[key]).__name__
            problems.append(f"{name}: envelope['{key}'] is {got}, expected {typ.__name__}")
    if env.get("disclaimer") != DISCLAIMER:
        problems.append(f"{name}: disclaimer mismatch (got {env.get('disclaimer')!r})")
    if isinstance(env.get("interpretation"), str) and not env["interpretation"].strip():
        problems.append(f"{name}: interpretation is empty")
    return problems


def _check_fields(name: str, env: dict, fields: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for path, expected_type in fields.items():
        found, value = _dig(env, path)
        if not found:
            problems.append(f"{name}: MISSING field '{path}' (expected {_typename(expected_type)})")
        elif not isinstance(value, expected_type):
            problems.append(
                f"{name}: field '{path}' is {type(value).__name__}, expected {_typename(expected_type)}"
            )
    return problems


def _typename(t: Any) -> str:
    if isinstance(t, tuple):
        return " | ".join(x.__name__ for x in t)
    return t.__name__


async def _run_one(name: str) -> tuple[str, list[str]]:
    """Returns (status, problems). status in {ok, drift, unreachable, error}."""
    spec = SPEC[name]
    tool = getattr(server, name)
    try:
        env = await tool(**spec["args"])
    except Exception as exc:  # a tool raising at all is itself a contract break
        return "error", [f"{name}: tool raised {type(exc).__name__}: {exc}"]

    problems = _check_envelope(name, env)
    if problems:
        return "drift", problems

    degraded = env.get("degraded") or []
    if degraded:
        return "unreachable", [f"{name}: degraded sources {degraded}"]

    problems = _check_fields(name, env, spec["fields"])
    if name == "get_threat_brief":
        lvl = (env.get("data") or {}).get("threat_level")
        if lvl not in THREAT_LEVELS:
            problems.append(f"{name}: threat_level {lvl!r} not in {sorted(THREAT_LEVELS)}")
    return ("drift" if problems else "ok"), problems


async def main() -> int:
    names = list(SPEC)
    print(f"SHEARLINE canary — probing {len(names)} sources live at ({OKC_LAT}, {OKC_LON})\n")

    results = await asyncio.gather(*(_run_one(n) for n in names))
    statuses = dict(zip(names, results, strict=True))

    # One retry for anything that isn't a clean pass — rules out a transient blip.
    retry = [n for n, (st, _) in statuses.items() if st != "ok"]
    if retry:
        print(f"Retrying {len(retry)} source(s) after a short pause to rule out transient outage: {retry}\n")
        from shearline.cache import CACHE

        CACHE.clear()  # force fresh fetches on retry
        await asyncio.sleep(5)
        retried = await asyncio.gather(*(_run_one(n) for n in retry))
        for n, res in zip(retry, retried, strict=True):
            statuses[n] = res

    failures: list[str] = []
    for name in names:
        status, problems = statuses[name]
        symbol = {"ok": "PASS", "drift": "DRIFT", "unreachable": "UNREACHABLE", "error": "ERROR"}[status]
        print(f"[{symbol:>11}] {name}")
        for p in problems:
            print(f"              - {p}")
        if status != "ok":
            failures.extend(problems)

    print()
    if failures:
        print(f"CANARY FAILED — {len(failures)} problem(s) across "
              f"{sum(1 for n in names if statuses[n][0] != 'ok')} source(s).")
        print("DRIFT = upstream schema changed (breaking); UNREACHABLE = source down after retry "
              "(could be a NOAA outage or a moved bucket/renamed file — check the source module).")
        return 1
    print("CANARY PASSED — all sources returned healthy, correctly-shaped responses.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
