"""Forecast-environment trend: reduce a series of RAP forecast-hour environments
to the discriminating quantities and describe their TRAJECTORY.

Pure functions over already-computed `compute_environment` outputs — no I/O,
so trivially testable.
"""

from typing import Any


def summarize(env_data: dict[str, Any], forecast_hour: int) -> dict[str, Any]:
    """Pull the discriminating quantities out of one compute_environment output."""
    th = env_data.get("thermodynamics") or {}
    kin = env_data.get("kinematics") or {}
    comp = env_data.get("composites") or {}
    stp = comp.get("stp_effective")
    if stp is None:
        stp = comp.get("stp_fixed_layer")
    return {
        "forecast_hour": forecast_hour,
        "valid_utc": env_data.get("valid_utc"),
        "mlcape_jkg": th.get("mlcape_jkg"),
        "bulk_shear_0_6km_kt": kin.get("bulk_shear_0_6km_kt"),
        "srh_0_1km_m2s2": kin.get("srh_0_1km_m2s2"),
        "scp": comp.get("scp"),
        "stp": stp,
    }


def _num(x: Any) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def interpret_trend(series: list[dict[str, Any]]) -> str:
    """2-4 analyst sentences describing how the environment evolves over the series."""
    if not series:
        return "No forecast environment series could be computed for this point."
    first, last = series[0], series[-1]
    span_h = last["forecast_hour"] - first["forecast_hour"]
    if span_h <= 0 or len(series) == 1:
        return (
            f"Single-time environment only (f{first['forecast_hour']:02d}): "
            f"MLCAPE {first.get('mlcape_jkg')} J/kg, 0-6 km shear "
            f"{first.get('bulk_shear_0_6km_kt')} kt, STP {first.get('stp')}."
        )

    stp0, stp1 = _num(first.get("stp")), _num(last.get("stp"))
    cape0, cape1 = _num(first.get("mlcape_jkg")), _num(last.get("mlcape_jkg"))
    shear0, shear1 = _num(first.get("bulk_shear_0_6km_kt")), _num(last.get("bulk_shear_0_6km_kt"))
    dstp, dcape, dshear = stp1 - stp0, cape1 - cape0, shear1 - shear0

    sentences: list[str] = []

    # Lead on the significant-tornado parameter trajectory when it's meaningful.
    if dstp >= 1.0 and stp1 >= 1.0:
        sentences.append(
            f"Significant-tornado parameter rising {stp0:g} → {stp1:g} over the next "
            f"{span_h} h — the environment is becoming more tornado-favorable."
        )
    elif dstp <= -1.0 and stp0 >= 1.0:
        sentences.append(
            f"Significant-tornado parameter falling {stp0:g} → {stp1:g} over the next "
            f"{span_h} h — the tornado-favorable window is closing."
        )
    elif stp1 >= 1.0 or stp0 >= 1.0:
        sentences.append(
            f"Significant-tornado parameter holds near {stp1:g} (from {stp0:g}) over the "
            f"next {span_h} h — a persistent, tornado-supportive environment."
        )

    # Then characterize the instability/shear evolution.
    if dcape >= 500:
        sentences.append(
            f"Destabilizing: MLCAPE building {cape0:.0f} → {cape1:.0f} J/kg"
            + (f" while 0-6 km shear strengthens to {shear1:.0f} kt." if dshear >= 5 else ".")
        )
    elif dcape <= -500:
        sentences.append(
            f"Stabilizing: MLCAPE falling {cape0:.0f} → {cape1:.0f} J/kg over the next "
            f"{span_h} h — the severe window is weakening."
        )
    else:
        sentences.append(
            f"Instability holds roughly steady (MLCAPE {cape0:.0f} → {cape1:.0f} J/kg); "
            f"0-6 km shear {shear0:.0f} → {shear1:.0f} kt."
        )

    if not sentences or all("tornado" not in s for s in sentences):
        # No notable STP signal — make the overall call explicit.
        if cape1 < 100:
            sentences.append("The airmass stays stable through the series; deep convection is not supported.")
    return " ".join(sentences) or (
        f"Environment roughly steady over the next {span_h} h."
    )
