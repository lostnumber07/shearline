"""Severe-weather environment parameters from a RAP point profile, via MetPy.

All thermodynamic/kinematic derivations use MetPy functions (invariant: no
hand-rolled thermodynamics). The effective inflow layer is located with
repeated MetPy parcel_profile/cape_cin evaluations per the SPC definition
(CAPE >= 100 J/kg and CIN >= -250 J/kg, contiguous from the lowest qualifying
level). Effective-layer STP includes the MLCIN term per SPC's formulation —
the term arithmetic is plain algebra over MetPy-derived quantities (MetPy's
significant_tornado implements only the fixed-layer variant, which we also
report).
"""

import math
from typing import Any

MS_TO_KT = 1.943844


def _round(value: float | None, ndigits: int = 0) -> float | int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    r = round(value, ndigits)
    return int(r) if ndigits == 0 else r


def compute_environment(profile: dict[str, Any]) -> dict[str, Any]:
    """Synchronous and CPU-bound — call via asyncio.to_thread."""
    import numpy as np
    from metpy.calc import (
        bulk_shear,
        bunkers_storm_motion,
        cape_cin,
        dewpoint_from_relative_humidity,
        el,
        lcl,
        mixed_layer_cape_cin,
        mixed_parcel,
        most_unstable_cape_cin,
        parcel_profile,
        significant_tornado,
        storm_relative_helicity,
        supercell_composite,
        surface_based_cape_cin,
        wind_direction,
        wind_speed,
    )
    from metpy.units import units

    p_iso = np.asarray(profile["pressure_hpa"], dtype=float)
    sp_hpa = profile["surface_pressure_pa"] / 100.0
    above = p_iso < (sp_hpa - 1.0)  # keep only levels above the surface

    t_iso = np.asarray(profile["temp_k"], dtype=float)[above] * units.kelvin
    rh_iso = np.clip(np.asarray(profile["rh_pct"], dtype=float)[above], 0.5, 100.0) * units.percent
    td_iso = dewpoint_from_relative_humidity(t_iso, rh_iso)

    p = np.concatenate(([sp_hpa], p_iso[above])) * units.hPa
    temp = np.concatenate(
        ([profile["t2m_k"]], t_iso.to("kelvin").magnitude)
    ) * units.kelvin
    dewp = np.concatenate(
        ([profile["d2m_k"]], td_iso.to("kelvin").magnitude)
    ) * units.kelvin
    u = np.concatenate(
        ([profile["u10_ms"]], np.asarray(profile["u_ms"], dtype=float)[above])
    ) * units("m/s")
    v = np.concatenate(
        ([profile["v10_ms"]], np.asarray(profile["v_ms"], dtype=float)[above])
    ) * units("m/s")
    z_agl = np.concatenate(
        ([0.0], np.asarray(profile["height_gpm"], dtype=float)[above] - profile["elevation_m"])
    ) * units.meter

    def height_at_pressure(target_hpa: float) -> float:
        # p decreases with index; np.interp needs ascending x
        return float(
            np.interp(target_hpa, p.magnitude[::-1], z_agl.magnitude[::-1])
        )

    sbcape, sbcin = surface_based_cape_cin(p, temp, dewp)
    mlcape, mlcin = mixed_layer_cape_cin(p, temp, dewp)
    mucape, mucin = most_unstable_cape_cin(p, temp, dewp)

    right_mover, _left, _mean = bunkers_storm_motion(p, u, v, z_agl)
    rm_u, rm_v = right_mover[0], right_mover[1]
    rm_speed_kt = float(wind_speed(rm_u, rm_v).to("m/s").magnitude) * MS_TO_KT
    rm_from_deg = float(wind_direction(rm_u, rm_v).magnitude)

    srh1 = storm_relative_helicity(
        z_agl, u, v, depth=1000 * units.meter, storm_u=rm_u, storm_v=rm_v
    )[2]
    srh3 = storm_relative_helicity(
        z_agl, u, v, depth=3000 * units.meter, storm_u=rm_u, storm_v=rm_v
    )[2]

    shear6_u, shear6_v = bulk_shear(p, u, v, height=z_agl, depth=6000 * units.meter)
    shear6 = wind_speed(shear6_u, shear6_v)
    shear1_u, shear1_v = bulk_shear(p, u, v, height=z_agl, depth=1000 * units.meter)
    shear1 = wind_speed(shear1_u, shear1_v)

    lcl_p, _ = lcl(p[0], temp[0], dewp[0])
    lcl_m_agl = height_at_pressure(float(lcl_p.to("hPa").magnitude))
    ml_p, ml_t, ml_td = mixed_parcel(p, temp, dewp)
    ml_lcl_p, _ = lcl(ml_p, ml_t, ml_td)
    ml_lcl_m_agl = height_at_pressure(float(ml_lcl_p.to("hPa").magnitude))

    # --- Effective inflow layer (SPC definition, MetPy parcel math) ---
    eff_base_i: int | None = None
    eff_top_i: int | None = None
    n = len(p)
    for i in range(n):
        if p.magnitude[i] < 500.0:
            break
        try:
            prof = parcel_profile(p[i:], temp[i], dewp[i])
            pcape, pcin = cape_cin(p[i:], temp[i:], dewp[i:], prof)
        except Exception:
            continue
        qualifies = (
            pcape.magnitude >= 100.0 and pcin.magnitude >= -250.0
        )
        if qualifies and eff_base_i is None:
            eff_base_i = i
            eff_top_i = i
        elif qualifies and eff_base_i is not None:
            eff_top_i = i
        elif eff_base_i is not None:
            break

    eff_base_m = eff_top_m = None
    eff_srh = ebwd_kt = None
    scp_val = 0.0
    if eff_base_i is not None and eff_top_i is not None and eff_top_i > eff_base_i:
        eff_base_m = float(z_agl.magnitude[eff_base_i])
        eff_top_m = float(z_agl.magnitude[eff_top_i])
        eff_srh = float(
            storm_relative_helicity(
                z_agl,
                u,
                v,
                depth=(eff_top_m - eff_base_m) * units.meter,
                bottom=eff_base_m * units.meter,
                storm_u=rm_u,
                storm_v=rm_v,
            )[2].magnitude
        )
        # Effective bulk wind difference: inflow base to 50% of MU-parcel EL.
        try:
            mu_prof = parcel_profile(p, temp[0], dewp[0])
            el_p, _ = el(p, temp, dewp, mu_prof)
            el_m_agl = height_at_pressure(float(el_p.to("hPa").magnitude))
        except Exception:
            el_m_agl = None
        if el_m_agl and el_m_agl / 2.0 > eff_base_m:
            eb_u, eb_v = bulk_shear(
                p,
                u,
                v,
                height=z_agl,
                bottom=eff_base_m * units.meter,
                depth=(el_m_agl / 2.0 - eff_base_m) * units.meter,
            )
            ebwd_ms = float(wind_speed(eb_u, eb_v).to("m/s").magnitude)
            ebwd_kt = ebwd_ms * MS_TO_KT
            scp_val = float(
                supercell_composite(
                    mucape, eff_srh * units("m^2/s^2"), ebwd_ms * units("m/s")
                ).magnitude[0]
            )
            scp_val = max(scp_val, 0.0) + 0.0  # floor for right-movers; kill -0.0

    stp_fixed = float(
        significant_tornado(sbcape, lcl_m_agl * units.meter, srh1, shear6).magnitude[0]
    )

    # Effective-layer STP with CIN term (SPC formulation), assembled from the
    # MetPy-derived components above.
    stp_eff = None
    if eff_srh is not None and ebwd_kt is not None:
        ebwd_ms = ebwd_kt / MS_TO_KT
        cape_term = float(mlcape.magnitude) / 1500.0
        lcl_term = min(max((2000.0 - ml_lcl_m_agl) / 1000.0, 0.0), 1.0)
        srh_term = eff_srh / 150.0
        shear_term = 0.0 if ebwd_ms < 12.5 else min(ebwd_ms, 30.0) / 20.0
        cin_term = min(max((200.0 + float(mlcin.magnitude)) / 150.0, 0.0), 1.0)
        stp_eff = max(cape_term * lcl_term * srh_term * shear_term * cin_term, 0.0) + 0.0

    data = {
        "model": "RAP 13-km analysis (f00)",
        "cycle_utc": profile.get("cycle_utc"),
        "valid_utc": profile.get("valid_utc"),
        "grid_point": profile.get("grid_point"),
        "thermodynamics": {
            "sbcape_jkg": _round(float(sbcape.magnitude)),
            "sbcin_jkg": _round(float(sbcin.magnitude)),
            "mlcape_jkg": _round(float(mlcape.magnitude)),
            "mlcin_jkg": _round(float(mlcin.magnitude)),
            "mucape_jkg": _round(float(mucape.magnitude)),
            "mucin_jkg": _round(float(mucin.magnitude)),
            "lcl_m_agl": _round(lcl_m_agl),
            "ml_lcl_m_agl": _round(ml_lcl_m_agl),
        },
        "kinematics": {
            "bulk_shear_0_1km_kt": _round(float(shear1.to("m/s").magnitude) * MS_TO_KT),
            "bulk_shear_0_6km_kt": _round(float(shear6.to("m/s").magnitude) * MS_TO_KT),
            "srh_0_1km_m2s2": _round(float(srh1.magnitude)),
            "srh_0_3km_m2s2": _round(float(srh3.magnitude)),
            "effective_inflow_base_m_agl": _round(eff_base_m),
            "effective_inflow_top_m_agl": _round(eff_top_m),
            "effective_srh_m2s2": _round(eff_srh),
            "effective_bulk_shear_kt": _round(ebwd_kt),
            "bunkers_right_mover": {
                "from_deg": _round(rm_from_deg),
                "speed_kt": _round(rm_speed_kt),
            },
        },
        "composites": {
            "scp": _round(scp_val, 1),
            "stp_effective": _round(stp_eff, 1),
            "stp_fixed_layer": _round(stp_fixed, 1),
        },
        "model_reported": {
            k: (_round(v, 1) if isinstance(v, (int, float)) else v)
            for k, v in profile.get("model_reported", {}).items()
        },
    }
    return data


def interpret_environment(data: dict[str, Any]) -> str:
    """2-5 analyst sentences discriminating the parameter-space regime."""
    th = data["thermodynamics"]
    kin = data["kinematics"]
    comp = data["composites"]
    mlcape = th["mlcape_jkg"] or 0
    mucape = th["mucape_jkg"] or 0
    shear6 = kin["bulk_shear_0_6km_kt"] or 0
    srh1 = kin["srh_0_1km_m2s2"] or 0
    lcl_m = th["lcl_m_agl"] or 0
    scp = comp["scp"] or 0
    stp = comp["stp_effective"] if comp["stp_effective"] is not None else comp["stp_fixed_layer"]
    stp = stp or 0

    sentences: list[str] = []

    if mucape < 100 and mlcape < 100:
        sentences.append(
            "The airmass is stable: effectively no CAPE for surface-based or "
            "elevated parcels, so deep convection is not supported regardless of shear."
        )
        if shear6 >= 40:
            sentences.append(
                f"Deep-layer shear is strong ({shear6} kt 0-6 km), but with no buoyancy "
                "it has nothing to organize — this matters only if instability develops."
            )
        sentences.append(
            f"Composite indices reflect the quiet setup (SCP {scp}, STP {stp})."
        )
        return " ".join(sentences)

    # Regime discrimination
    if mlcape >= 2000 and shear6 < 25:
        sentences.append(
            f"This is a high-CAPE/low-shear pulse regime: MLCAPE around {mlcape} J/kg "
            f"with only {shear6} kt of 0-6 km shear favors short-lived pulse storms — "
            "brief hail/microburst wind threats rather than organized, persistent severe."
        )
    elif mlcape < 1000 and shear6 >= 40:
        sentences.append(
            f"This is a low-CAPE/high-shear profile (MLCAPE ~{mlcape} J/kg, 0-6 km shear "
            f"{shear6} kt), the classic cool-season/high-shear setup: storms can organize "
            "into lines or low-topped supercells despite modest buoyancy, and any tornado "
            "threat hinges on low-level SRH rather than instability."
        )
    elif mlcape >= 1000 and shear6 >= 35:
        sentences.append(
            f"CAPE and shear overlap in classic supercell parameter space: MLCAPE "
            f"~{mlcape} J/kg beneath {shear6} kt of deep-layer shear supports organized, "
            "persistent rotating storms."
        )
    else:
        sentences.append(
            f"A mixed/marginal environment: MLCAPE ~{mlcape} J/kg with {shear6} kt of "
            "0-6 km shear — enough for storms with some organization, but short of "
            "classic supercell parameter space."
        )

    if srh1 >= 150 and lcl_m and lcl_m < 1200:
        sentences.append(
            f"Low-level rotation ingredients are notable: 0-1 km SRH of {srh1} m2/s2 "
            f"with LCLs near {lcl_m} m AGL — low cloud bases plus strong low-level "
            "hodograph curvature are the tornado-favorable combination."
        )
    elif lcl_m and lcl_m > 1800 and mlcape >= 1000:
        sentences.append(
            f"Cloud bases are high (LCL ~{lcl_m} m AGL), which favors hail and damaging "
            "downburst winds over tornadoes."
        )

    if scp >= 4 or stp >= 1:
        sentences.append(
            f"Composite indices underline the threat: SCP {scp} (supercell composite "
            f">~1 supports supercells) and STP {stp} (effective significant-tornado "
            "parameter >~1 is the climatological significant-tornado threshold)."
        )
    else:
        sentences.append(
            f"Composite indices stay modest (SCP {scp}, STP {stp}), so organized "
            "significant-severe potential is limited at this point."
        )
    return " ".join(sentences)
