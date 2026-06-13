---
name: hail-claim-verification
description: Verify whether damaging hail actually occurred at a given location on a given date, using SHEARLINE's storm-report and radar tools. Use for insurance claim triage, roofing/auto damage disputes, or any "did it really hail here on this day" question.
---

# Hail-claim verification

Goal: produce a defensible **corroborated / partially corroborated / not corroborated**
verdict for a hail claim at a specific location and date, with the evidence and
its distance/time from the claimed loss.

> Reminder: SHEARLINE data is informational and preliminary (NWS/spotter reports),
> not a legal adjudication or the final NCEI Storm Events record. State that in the
> output.

## Inputs you need
- **Location** — a street address or place. Geocode it to `lat, lon` (decimal degrees,
  CONUS only; longitude is negative in the US).
- **Date of the alleged hail** — a single UTC calendar day, `YYYY-MM-DD`. If the
  claimant gives a local date/time near midnight, check the adjacent UTC day too.
- **Claimed hail size** (optional, inches) — to compare against reports.

## Procedure

1. **Pull ground-truth reports for that day.**
   `get_historical_storm_reports(lat, lon, date, radius_km=40)`
   - Read `data.counts.hail` and the `data.reports` entries with `category: "hail"`.
   - For each hail report note `magnitude` (inches), `time_utc`, and `distance_km` /
     `direction` from the claim location.

2. **If the event was within the last ~24 hours, add radar-derived hail.**
   `get_mrms_severe(lat, lon, radius_km=40)`
   - `data.hail_mesh.max_mesh_in` is the radar Maximum Estimated Size of Hail in the
     last hour, with the distance/bearing of the maximum. (MRMS only retains ~24–48 h,
     so this step applies to recent claims only; for older dates rely on step 1.)

3. **Widen if nothing close.** If no hail report is within ~15 km, re-run step 1 with
   `radius_km=80` to see whether a hail swath passed nearby (hail is highly localized;
   a report 30 km away is weak support).

4. **Form the verdict.**
   - **Corroborated** — a hail report (or MESH ≥ 1") within ~15 km and within a few
     hours of the claimed time, of comparable or larger size.
   - **Partially corroborated** — hail reported in the broader area (15–40 km) or of
     smaller size than claimed.
   - **Not corroborated** — no hail reports within 40 km on that UTC day (note that
     pre-2005 dates aren't covered, and an empty old-date result is not proof of a
     quiet day).

## Output template

```
Hail-claim verification — <address> (<lat>, <lon>), <date> UTC
Verdict: <corroborated | partially corroborated | not corroborated>
Evidence:
  - <n> hail report(s) within 40 km; largest <size>" at <time_utc>, <dist> km <dir>
  - [if recent] MRMS MESH max <x>" (<dist> km <dir>) in the last hour
Nearest report to the loss: <size>" at <time_utc>, <dist> km <dir>
Caveats: preliminary NWS/spotter + radar data; not the final NCEI record;
         hail is localized so distance matters.
```
