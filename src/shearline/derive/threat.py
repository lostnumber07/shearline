"""Composite threat brief: synthesize warnings, outlook, environment, MRMS,
and storm reports into one ranked assessment with stated logic.

The level cascade is deliberately explicit and rule-based — every triggered
rule appends a plain-language reason to threat_logic so an agent (or human)
can audit exactly why the level is what it is.
"""

from datetime import UTC, datetime
from typing import Any

LEVELS = ["none", "marginal", "elevated", "significant", "extreme"]


def _latest_instant_utc(timestamps: list[str]) -> str | None:
    """Max of mixed-offset ISO timestamps by actual instant, emitted as UTC.

    NWS `expires` strings carry WFO-local offsets; a lexicographic max across
    time zones picks the wrong instant."""
    parsed = []
    for ts in timestamps:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is not None:
                parsed.append(dt)
        except (ValueError, TypeError):
            continue
    if not parsed:
        return timestamps[-1] if timestamps else None
    return max(parsed).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

_HAZARD_WORDS = [(80, "extreme"), (50, "high"), (25, "moderate"), (10, "low"), (0, "none")]


def _word(score: float) -> str:
    for threshold, word in _HAZARD_WORDS:
        if score >= threshold:
            return word
    return "none"


def _data(env: dict | None) -> dict:
    return (env or {}).get("data") or {}


def build_threat_brief(
    lat: float,
    lon: float,
    warnings_env: dict | None,
    outlook_env: dict | None,
    environment_env: dict | None,
    mrms_env: dict | None,
    reports_env: dict | None,
    lightning_env: dict | None = None,
) -> tuple[dict[str, Any], str]:
    w = _data(warnings_env)
    o = _data(outlook_env)
    e = _data(environment_env)
    m = _data(mrms_env)
    r = _data(reports_env)
    li = _data(lightning_env)

    rules: list[tuple[str, str]] = []  # (level, reason)
    scores = {"tornado": 0.0, "hail": 0.0, "damaging_wind": 0.0, "flash_flood": 0.0}

    warnings = w.get("warnings") or []
    watches = w.get("watches_at_point") or []
    refl_raw = (m.get("composite_reflectivity") or {}).get("max_dbz")
    refl = -99 if refl_raw is None else refl_raw
    mesh_in = (m.get("hail_mesh") or {}).get("max_mesh_in") or 0
    rot_ll = (m.get("rotation_lowlevel") or {}).get("max_azimuthal_shear_s1") or 0
    rot_ml = (m.get("rotation_midlevel") or {}).get("max_azimuthal_shear_s1") or 0
    vil = (m.get("vil") or {}).get("max_vil_kg_m2") or 0
    counts = r.get("counts") or {}
    report_total = sum(v or 0 for v in counts.values())
    flash_count = li.get("flash_count") or 0
    nearest_strike_km = (li.get("nearest_strike") or {}).get("distance_km")
    storms_active = bool(warnings) or refl >= 50 or report_total > 0 or flash_count > 0

    # ---- Warnings ----
    for warn in warnings:
        event = warn.get("event") or ""
        inside = warn.get("point_inside")
        ibw = warn.get("ibw_tags") or {}
        dist = warn.get("distance_km")
        where = "at the point" if inside else f"{dist} km {warn.get('direction') or ''} of the point"

        if event == "Tornado Warning":
            scores["tornado"] += 60 if inside else 35
            damage = (ibw.get("tornado_damage_threat") or "").upper()
            detection = (ibw.get("tornado_detection") or "").upper()
            if inside and (damage in ("CONSIDERABLE", "CATASTROPHIC") or detection == "OBSERVED"):
                scores["tornado"] += 30
                rules.append(
                    (
                        "extreme",
                        f"Tornado Warning {where} with "
                        + (
                            f"'{damage.title()}' damage threat"
                            if damage
                            else "an observed tornado"
                        )
                        + " — treat as an immediate life-safety situation.",
                    )
                )
            elif inside and ((counts.get("tornado") or 0) > 0 or rot_ll >= 0.01):
                scores["tornado"] += 20
                corroboration = (
                    "confirmed tornado reports nearby"
                    if counts.get("tornado")
                    else "an intense low-level rotation track nearby"
                )
                rules.append(
                    (
                        "extreme",
                        f"Tornado Warning in effect {where}, corroborated by {corroboration} — "
                        "treat as an immediate life-safety situation.",
                    )
                )
            elif inside:
                rules.append(("significant", f"Tornado Warning in effect {where}."))
            else:
                rules.append(("elevated", f"Tornado Warning {where}."))
        elif event == "Severe Thunderstorm Warning":
            hail = ibw.get("max_hail_size_in") or 0
            gust = ibw.get("max_wind_gust_mph") or 0
            scores["hail"] += (45 if hail >= 1.75 else 30 if hail >= 1 else 10) * (
                1.0 if inside else 0.6
            )
            scores["damaging_wind"] += (50 if gust >= 80 else 30 if gust >= 60 else 10) * (
                1.0 if inside else 0.6
            )
            if (ibw.get("tornado_detection") or "").upper() == "POSSIBLE":
                scores["tornado"] += 15
            damage = (ibw.get("thunderstorm_damage_threat") or "").upper()
            if inside and damage in ("CONSIDERABLE", "DESTRUCTIVE"):
                rules.append(
                    (
                        "significant",
                        f"Severe Thunderstorm Warning {where} tagged '{damage.title()}' "
                        f"(hail to {hail}\", gusts to {gust:.0f} mph).",
                    )
                )
            elif inside:
                rules.append(("elevated", f"Severe Thunderstorm Warning in effect {where}."))
            else:
                rules.append(("elevated", f"Severe Thunderstorm Warning {where}."))
        elif event == "Flash Flood Warning":
            scores["flash_flood"] += 45 if inside else 20
            if inside:
                rules.append(("elevated", "Flash Flood Warning in effect at the point."))

    for watch in watches:
        if "Tornado" in (watch.get("event") or ""):
            scores["tornado"] += 15
            rules.append(("elevated", "The point is inside a Tornado Watch."))
        elif "Severe" in (watch.get("event") or ""):
            scores["hail"] += 8
            scores["damaging_wind"] += 8
            rules.append(("marginal", "The point is inside a Severe Thunderstorm Watch."))

    # ---- Outlook ----
    cat = (o.get("categorical") or {}).get("label")
    probs = o.get("probabilities") or {}
    if cat == "HIGH":
        rules.append(("significant", "SPC High risk (5/5) — severe weather outbreak expected."))
    elif cat == "MDT":
        rules.append(
            (
                "significant" if storms_active else "elevated",
                "SPC Moderate risk (4/5)"
                + (" with storms already active in the area." if storms_active else "."),
            )
        )
    elif cat == "ENH":
        rules.append(("elevated", "SPC Enhanced risk (3/5) for the area."))
    elif cat == "SLGT":
        rules.append(
            (
                "elevated" if storms_active else "marginal",
                "SPC Slight risk (2/5)"
                + (" with storms already active nearby." if storms_active else "."),
            )
        )
    elif cat == "MRGL":
        rules.append(("marginal", "SPC Marginal risk (1/5) for the area."))

    torn_prob = (probs.get("tornado") or {}).get("probability_pct") or 0
    scores["tornado"] += torn_prob * 2
    scores["hail"] += (probs.get("hail") or {}).get("probability_pct") or 0
    scores["damaging_wind"] += (probs.get("wind") or {}).get("probability_pct") or 0
    if torn_prob >= 10:
        rules.append(
            (
                "significant" if storms_active and torn_prob >= 15 else "elevated",
                f"SPC tornado probability of {torn_prob}% within 25 miles.",
            )
        )
    for hz in ("tornado", "hail", "wind"):
        p = probs.get(hz) or {}
        if p.get("significant") or p.get("conditional_intensity"):
            key = "damaging_wind" if hz == "wind" else hz
            scores[key] += 10

    # ---- Environment ----
    comp = e.get("composites") or {}
    kin = e.get("kinematics") or {}
    th = e.get("thermodynamics") or {}
    stp = comp.get("stp_effective")
    stp = comp.get("stp_fixed_layer") if stp is None else stp
    stp = stp or 0
    scp = comp.get("scp") or 0
    if stp >= 3:
        scores["tornado"] += 30
        rules.append(
            (
                "significant" if storms_active else "elevated",
                f"Significant-tornado parameter of {stp}"
                + (
                    " with storms ongoing — environment strongly supports tornadic supercells."
                    if storms_active
                    else " — a volatile environment if storms can initiate."
                ),
            )
        )
    elif stp >= 1:
        scores["tornado"] += 15
        rules.append(
            (
                "elevated" if storms_active else "marginal",
                f"Significant-tornado parameter of {stp} — tornado-supportive environment"
                + ("." if storms_active else ", conditional on storms developing."),
            )
        )
    if scp >= 4 and storms_active:
        scores["hail"] += 10
        rules.append(("elevated", f"Supercell composite of {scp} with active storms."))
    mucape = th.get("mucape_jkg") or 0
    shear6 = kin.get("bulk_shear_0_6km_kt") or 0
    if mucape >= 2000 and shear6 >= 40:
        scores["hail"] += 15
        scores["damaging_wind"] += 10
        if not any(level in ("significant", "extreme") for level, _ in rules):
            rules.append(
                (
                    "elevated" if storms_active else "marginal",
                    f"MUCAPE ~{mucape} J/kg with {shear6} kt deep shear supports organized "
                    "severe storms" + ("." if storms_active else " if initiation occurs."),
                )
            )

    # ---- MRMS ----
    if mesh_in >= 2:
        scores["hail"] += 45
        rules.append(("significant", f"MRMS MESH of {mesh_in}\" hail within radius in the last hour."))
    elif mesh_in >= 1:
        scores["hail"] += 25
        rules.append(("elevated", f"MRMS MESH of {mesh_in}\" hail within radius in the last hour."))
    rot_best = max(rot_ll, rot_ml)
    if rot_ll >= 0.01 or rot_ml >= 0.012:
        scores["tornado"] += 25
        rules.append(
            (
                "significant",
                f"Intense rotation track (azimuthal shear {rot_best} /s) nearby in the last hour.",
            )
        )
    elif rot_ll >= 0.006 or rot_ml >= 0.008:
        scores["tornado"] += 12
        rules.append(
            ("elevated", f"Strong rotation track (azimuthal shear {rot_best} /s) nearby in the last hour.")
        )
    if refl >= 60:
        rules.append(("elevated", f"A {refl}-dBZ core is within the radius — hail-capable convection."))
    if vil >= 45:
        scores["hail"] += 10

    # ---- Storm reports ----
    if counts.get("tornado"):
        scores["tornado"] += 35
        rules.append(
            ("significant", f"{counts['tornado']} tornado report(s) near the point in the report window.")
        )
    if counts.get("hail"):
        scores["hail"] += 15
    if counts.get("wind"):
        scores["damaging_wind"] += 12
    if counts.get("flood"):
        scores["flash_flood"] += 10
    if (counts.get("hail") or 0) + (counts.get("wind") or 0) >= 3:
        rules.append(
            ("elevated", "Multiple recent severe hail/wind reports in the area — storms are producing.")
        )

    # ---- Lightning (outdoor-safety hazard) ----
    if flash_count > 0 and nearest_strike_km is not None:
        if nearest_strike_km <= 16:
            rules.append(
                (
                    "elevated",
                    f"Lightning within {nearest_strike_km} km of the point ({flash_count} flash(es) "
                    "in the last 15 min) — an immediate outdoor-safety hazard regardless of "
                    "severe-storm potential.",
                )
            )
        else:
            rules.append(
                (
                    "marginal",
                    f"Lightning in the area ({nearest_strike_km} km from the point) but not yet "
                    "within strike range.",
                )
            )

    # ---- Final level ----
    if rules:
        level = LEVELS[max(LEVELS.index(lvl) for lvl, _ in rules)]
    else:
        level = "none"
    # Order reasons by severity, keep the strongest few.
    logic = [
        reason
        for _, reason in sorted(rules, key=lambda x: -LEVELS.index(x[0]))
    ][:6]
    if not logic:
        if cat == "TSTM":
            logic = [
                "General (non-severe) thunderstorms are in the outlook, but there are no "
                "active warnings or watches, no severe risk area, no storm signatures on "
                "radar-derived products, and no recent local storm reports."
            ]
        else:
            logic = [
                "No active warnings or watches, no SPC outlook risk area, no storm signatures "
                "on radar-derived products, and no recent local storm reports."
            ]

    hazards_ranked = sorted(
        (
            {"hazard": hz, "level": _word(score), "score": round(score)}
            for hz, score in scores.items()
        ),
        key=lambda h: -h["score"],
    )

    # ---- Attention window ----
    if warnings:
        expiries = [warn.get("expires_utc") for warn in warnings if warn.get("expires_utc")]
        attention = {
            "window": "now",
            "until_utc": _latest_instant_utc(expiries),
            "reasoning": "Active warnings — the threat is immediate until they expire or are replaced.",
        }
    elif refl >= 50:
        attention = {
            "window": "next 1-2 hours",
            "until_utc": None,
            "reasoning": "Strong storms are within the radius; threat evolves with storm motion.",
        }
    elif cat in ("SLGT", "ENH", "MDT", "HIGH"):
        attention = {
            "window": "through the outlook period",
            "until_utc": (o.get("expire_utc") or None),
            "reasoning": "Outlook risk with no storms yet near the point — watch for initiation, "
            "typically peaking with afternoon heating into evening.",
        }
    else:
        attention = {
            "window": "none",
            "until_utc": None,
            "reasoning": "No defined attention window; re-check at the next outlook issuance.",
        }

    # ---- Nearest storm signature ----
    candidates = []
    for label, block, fmt in (
        ("composite reflectivity", m.get("composite_reflectivity"), lambda b: f"{b.get('max_dbz')} dBZ"),
        ("60-min MESH", m.get("hail_mesh"), lambda b: f"{b.get('max_mesh_in')}\" hail"),
        ("low-level rotation", m.get("rotation_lowlevel"), lambda b: f"{b.get('max_azimuthal_shear_s1')} /s"),
        ("mid-level rotation", m.get("rotation_midlevel"), lambda b: f"{b.get('max_azimuthal_shear_s1')} /s"),
    ):
        if not block or "distance_km" not in (block.get("max_location") or {}):
            continue
        value = (
            block.get("max_dbz")
            or block.get("max_mesh_in")
            or block.get("max_azimuthal_shear_s1")
        )
        significant_value = (
            (label == "composite reflectivity" and (block.get("max_dbz") or 0) >= 50)
            or (label == "60-min MESH" and (block.get("max_mesh_in") or 0) >= 0.5)
            or ("rotation" in label and (block.get("max_azimuthal_shear_s1") or 0) >= 0.004)
        )
        if value and significant_value:
            candidates.append(
                {
                    "signature": label,
                    "value": fmt(block),
                    **block["max_location"],
                    "valid_utc": block.get("valid_utc"),
                }
            )
    nearest_signature = (
        min(candidates, key=lambda c: c.get("distance_km", 1e9)) if candidates else None
    )

    env_summary = {
        "mlcape_jkg": th.get("mlcape_jkg"),
        "bulk_shear_0_6km_kt": kin.get("bulk_shear_0_6km_kt"),
        "srh_0_1km_m2s2": kin.get("srh_0_1km_m2s2"),
        "stp_effective": comp.get("stp_effective"),
        "scp": comp.get("scp"),
    } if e else None

    lightning_summary = {
        "flash_count": flash_count,
        "flashes_per_min": li.get("flashes_per_min"),
        "nearest_strike_km": nearest_strike_km,
    } if li else None

    data = {
        "point": {"lat": lat, "lon": lon},
        "threat_level": level,
        "threat_logic": logic,
        "hazards_ranked": hazards_ranked,
        "lightning_summary": lightning_summary,
        "warnings_summary": {
            "active_at_point": sum(1 for x in warnings if x.get("point_inside")),
            "active_within_radius": len(warnings),
            "watches_at_point": [x.get("event") for x in watches],
        },
        "outlook_summary": {
            "categorical": cat,
            "tornado_pct": (probs.get("tornado") or {}).get("probability_pct"),
            "hail_pct": (probs.get("hail") or {}).get("probability_pct"),
            "wind_pct": (probs.get("wind") or {}).get("probability_pct"),
            "total_severe_pct": (probs.get("total_severe") or {}).get("probability_pct"),
        } if o else None,
        "environment_summary": env_summary,
        "nearest_storm_signature": nearest_signature,
        "attention_window": attention,
    }

    interp = _interpret(
        level, logic, hazards_ranked, env_summary, nearest_signature, attention, lightning_summary
    )
    return data, interp


def _interpret(
    level: str,
    logic: list[str],
    hazards: list[dict],
    env: dict | None,
    signature: dict | None,
    attention: dict,
    lightning: dict | None = None,
) -> str:
    s = [f"Overall threat level: {level.upper()}. {logic[0]}"]
    active_hazards = [h for h in hazards if h["level"] != "none"]
    if active_hazards:
        ranked = ", ".join(f"{h['hazard'].replace('_', ' ')} ({h['level']})" for h in active_hazards)
        s.append(f"Hazards in order of concern: {ranked}.")
    else:
        s.append("No individual hazard rises above background levels.")
    if lightning and (lightning.get("flash_count") or 0) > 0:
        near = lightning.get("nearest_strike_km")
        s.append(
            f"Lightning: {lightning['flash_count']} flash(es) nearby"
            + (f", nearest {near} km from the point." if near is not None else ".")
        )
    if env and env.get("mlcape_jkg") is not None:
        s.append(
            f"Environment snapshot: MLCAPE {env['mlcape_jkg']} J/kg, 0-6 km shear "
            f"{env['bulk_shear_0_6km_kt']} kt, 0-1 km SRH {env['srh_0_1km_m2s2']} m2/s2, "
            f"effective STP {env['stp_effective']}, SCP {env['scp']}."
        )
    if signature:
        s.append(
            f"Nearest storm signature: {signature['signature']} of {signature['value']}, "
            f"{signature.get('distance_km', '?')} km {signature.get('direction', '')} of the point."
        )
    s.append(f"Attention window: {attention['window']} — {attention['reasoning']}")
    return " ".join(s)
