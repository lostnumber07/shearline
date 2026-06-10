"""Standard response envelope every SHEARLINE tool returns (invariants 3-5).

Shape:
    data            numeric/structured payload, units stated in field names
    interpretation  2-5 plain-language analyst sentences
    degraded        list of upstream sources that failed (empty when healthy)
    disclaimer      fixed safety line, always present on hazard tools
"""

from typing import Any

DISCLAIMER = "Informational only. Not a substitute for official NWS warnings."


def envelope(
    data: dict[str, Any],
    interpretation: str,
    *,
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "data": data,
        "interpretation": interpretation,
        "degraded": degraded or [],
        "disclaimer": DISCLAIMER,
    }
