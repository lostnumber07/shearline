---
name: event-day-lightning-watch
description: Run a lightning-safety watch for an outdoor venue during an event window, polling SHEARLINE's lightning and threat-brief tools and applying the 30-30 / 10-mile rules to issue suspend / shelter / resume calls. Use for stadiums, festivals, golf, drone ops, construction, or any outdoor-safety officer role.
---

# Event-day lightning watch

Goal: keep people safe at a fixed outdoor venue by polling lightning proximity and
issuing clear **continue / suspend / shelter / resume** calls using standard
lightning-safety thresholds.

> Reminder: this supports an on-site safety officer; it does not replace one. The
> venue's emergency plan and official warnings take precedence.

## Inputs you need
- **Venue** — `lat, lon` (CONUS).
- **Event window** — start/end local time.
- **Poll interval** — every 5–10 minutes during the window (lightning evolves fast).

## Standard thresholds (apply these consistently)
- **≤ 10 km** from the venue → **SHELTER NOW** (strikes essentially overhead; 30-30 rule).
- **≤ 16 km (~10 mi)** → **SUSPEND** outdoor activity, move toward shelter.
- **> 16 km but present / approaching** → **HEIGHTENED WATCH**, ready to suspend.
- **Resume** only **30 minutes after the last strike** within 16 km.

## Procedure

1. **Pre-event baseline.** `get_threat_brief(lat, lon)`
   - Note the overall `threat_level`, the `attention_window`, and any
     `lightning_summary`. If the brief is already `elevated`+ for storms/lightning,
     brief the safety officer before doors open.

2. **Poll during the event.** Every 5–10 min:
   `get_lightning(lat, lon, radius_km=16, minutes=15)`
   - Read `data.flash_count`, `data.flashes_per_min`, and
     `data.nearest_strike.distance_km` / `time_utc`.
   - Apply the thresholds above to set the current call.
   - Track the **last strike time within 16 km** to drive the 30-minute resume clock.

3. **Escalation check.** If lightning appears or the flash rate climbs, also run
   `get_threat_brief(lat, lon)` to see whether severe weather (not just lightning) is
   developing — hail/wind/tornado may warrant evacuation beyond a lightning hold.

4. **Resume.** When `get_lightning` shows no strikes within 16 km for a continuous
   30 minutes, clear the suspension.

## Output template (per poll)

```
[<time_utc>] Lightning watch — <venue>
Nearest strike: <dist> km <dir> at <time_utc>  (flashes <n>, <rate>/min within 16 km)
CALL: <CONTINUE | HEIGHTENED WATCH | SUSPEND | SHELTER NOW | CLEARED>
Resume eligible at: <last-strike-time + 30 min, or "n/a">
Notes: <e.g. "rate increasing, cell approaching from SW">
```
