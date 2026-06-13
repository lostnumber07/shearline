---
name: chase-day-briefing
description: Produce a storm-chaser / emergency-manager severe-weather briefing for a target location today, sequencing SHEARLINE's outlook, environment, trend, warning, and radar tools into a go/no-go call with a target window. Use for chase planning, outdoor-operations risk calls, or field-team safety briefings.
---

# Chase-day briefing

Goal: a concise field briefing for a target point today — **what's expected, is the
environment loaded and trending up, is anything already happening**, and a clear
go / hold / no-go with the window of greatest concern.

> Reminder: SHEARLINE is decision *support*. Official NWS watches/warnings drive
> life-safety decisions; say so in the briefing.

## Inputs you need
- **Target** — `lat, lon` (CONUS). For a region, pick the centroid or run the
  sequence for two or three candidate points.

## Procedure (run in this order; the later steps refine the earlier)

1. **The forecast frame.** `get_spc_outlook(lat, lon, day=1)`
   - Note `data.categorical.label` (MRGL→HIGH) and the tornado/hail/wind
     `probabilities`. If you're planning ahead, also `day=2`.

2. **The environment now.** `get_point_environment(lat, lon)`
   - Read the `interpretation` — it names the regime (pulse / cool-season high-shear /
     classic supercell). Note MLCAPE, 0–6 km shear, 0–1 km SRH, SCP, effective STP,
     and whether instability is **capped** (large negative MLCIN matters — storms may
     not fire until the cap erodes).

3. **The trajectory.** `get_environment_trend(lat, lon)`
   - This is the chase-critical step: is STP/CAPE **rising** into the afternoon, or
     stabilizing? Read the trend `interpretation` ("intensifying" vs "weakening").

4. **What's already up.** `get_active_warnings(lat, lon, radius_km=80)` and
   `get_mrms_severe(lat, lon, radius_km=80)`
   - Any active warnings, and any MESH / rotation-track signatures already in range
     (storms upstream that will move toward the target).

5. **Synthesize.** Optionally run `get_threat_brief(lat, lon)` for the composite level,
   then write the call.

## Output template

```
Chase briefing — (<lat>, <lon>), <date>
Outlook: SPC <category>; tornado <x>% / hail <y>% / wind <z>%
Environment: <regime in one phrase>; MLCAPE <..> J/kg, 0-6km shear <..> kt,
             0-1km SRH <..>, eff-STP <..>, SCP <..>; cap: <none/weak/strong>
Trend (f00→f06): <intensifying | steady | weakening> — <one line>
Already active: <warnings / MESH / rotation in range, or "nothing yet">
CALL: <GO | HOLD | NO-GO>
Target window: <e.g. 21–01Z as heating maximizes / cap erodes>
Watch for: <the single signal that would change the call, e.g.
            "rotation track appearing within 60 km to the SW">
Safety: follow live NWS warnings; this is planning support only.
```
