"""Standard response envelope every SHEARLINE tool returns (invariants 3-5).

Shape:
    schema_version  semver of the tool I/O contract (see ARCHITECTURE "Stability
                    contract"); field renames/removals bump the major version
    data            numeric/structured payload, units stated in field names
    interpretation  2-5 plain-language analyst sentences
    degraded        list of upstream sources that failed (empty when healthy)
    disclaimer      fixed safety line, always present on hazard tools
"""

from typing import Any

DISCLAIMER = "Informational only. Not a substitute for official NWS warnings."

# Bump per the stability contract: MAJOR for a breaking field rename/removal/retype,
# MINOR for backward-compatible additions.
SCHEMA_VERSION = "1.0"


def envelope(
    data: dict[str, Any],
    interpretation: str,
    *,
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "data": data,
        "interpretation": interpretation,
        "degraded": degraded or [],
        "disclaimer": DISCLAIMER,
    }
