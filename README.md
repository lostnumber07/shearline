<!-- mcp-name: io.github.lostnumber07/shearline -->

# SHEARLINE

[![PyPI](https://img.shields.io/pypi/v/shearline)](https://pypi.org/project/shearline/)
[![Python](https://img.shields.io/pypi/pyversions/shearline)](https://pypi.org/project/shearline/)
[![CI](https://github.com/lostnumber07/shearline/actions/workflows/ci.yml/badge.svg)](https://github.com/lostnumber07/shearline/actions/workflows/ci.yml)
[![MCP Registry](https://img.shields.io/badge/MCP%20registry-io.github.lostnumber07%2Fshearline-blue)](https://registry.modelcontextprotocol.io/v0.1/servers?search=shearline)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**The severe-weather analyst your agent doesn't have.** SHEARLINE is a free, MIT-licensed MCP server that gives AI agents analyst-grade US severe-weather tools: live warning polygons with Impact-Based Warning tags, SPC convective outlooks, RAP-derived point environments (CAPE/shear/SRH/STP computed with MetPy), MRMS radar-derived hail and rotation products, ground-truth storm reports, and a composite threat brief that synthesizes all of it. A dozen weather MCPs already wrap the basic forecast API; SHEARLINE deliberately skips everything they do and ships only what requires radar meteorology to expose correctly.

> **Informational only. Not a substitute for official NWS warnings.** Every tool repeats this, because it matters: when weather threatens, follow official warnings from weather.gov and local authorities.

## Tools

| Tool | What it returns |
| --- | --- |
| `get_active_warnings(lat, lon, radius_km=40)` | Active tornado/severe-thunderstorm/flash-flood warning polygons with IBW tags (max hail size, max gust, tornado detection/damage threat), parsed storm motion, expirations, and whether the exact point is inside a polygon. Watches listed separately. |
| `get_spc_outlook(lat, lon, day=1)` | SPC categorical risk (TSTM→HIGH) at the point plus tornado/hail/wind probabilities and significant-severe flags, days 1–3, with interpretation calibrated to the category. |
| `get_point_environment(lat, lon)` | Latest RAP 13-km analysis profile computed with MetPy: MLCAPE/MUCAPE/CINs, LCL, 0–1/0–6 km shear, 0–1/0–3 km SRH, Bunkers motion, effective inflow layer, effective SRH/shear, SCP, and significant-tornado parameter — interpreted like an analyst (pulse vs. cool-season high-shear vs. classic supercell parameter space). |
| `get_environment_trend(lat, lon)` | The anticipatory view: a short RAP forecast series (f00/f01/f03/f06, one consistent cycle) of MLCAPE, 0–6 km shear, 0–1 km SRH, SCP and STP, with an interpretation of the **trajectory** (intensifying / stabilizing / steady) — for "is this getting worse" rather than "what is it now." |
| `get_mrms_severe(lat, lon, radius_km=40)` | MRMS maxima within radius: 60-min MESH (hail, inches and mm), low-level and mid-level rotation tracks (azimuthal shear), VIL, composite reflectivity — each with valid time and distance/bearing of the max. |
| `get_storm_reports(lat, lon, radius_km=80, hours=6)` | Normalized Local Storm Reports: type, magnitude with units, time, location, distance/bearing, remarks. |
| `get_lightning(lat, lon, radius_km=40, minutes=15)` | GOES-East GLM total-lightning activity in the recent window: flash count and rate, nearest strike (distance/bearing/time), and a tiered outdoor-safety interpretation (overhead / within-striking-distance / in-the-area). |
| `get_historical_storm_reports(lat, lon, date, radius_km=80)` | What hail/wind/tornado hit a point on a specific past date (`YYYY-MM-DD`, UTC) — normalized reports with magnitude+units and distance/bearing, for the insurance / ag / forensic use case. Coverage from ~2005; preliminary LSRs, not the final NCEI record. |
| `get_threat_brief(lat, lon)` | The showpiece: runs everything above concurrently and synthesizes a threat level (none/marginal/elevated/significant/extreme) **with stated logic**, hazards ranked, environment summary, nearest storm signature, and a recommended attention window. |
| `get_radar_snapshot(lat, lon)` | Nearest WSR-88D's latest Level 2 volume metadata: VCP (scan strategy), max reflectivity with range/azimuth, coarse echo-top estimate. |

Every tool returns structured JSON with `data` (numeric fields, units stated), `interpretation` (plain-language analyst sentences), `degraded` (which upstream sources failed, if any — partial data instead of errors), and the safety `disclaimer`.

## Example: threat brief during a real outbreak

Real output from 2026-06-10, point inside an active tornado warning in northern Missouri:

```json
{
  "threat_level": "extreme",
  "threat_logic": [
    "Tornado Warning in effect at the point, corroborated by confirmed tornado reports nearby — treat as an immediate life-safety situation.",
    "Severe Thunderstorm Warning at the point tagged 'Considerable' (hail to 1.75\", gusts to 60 mph).",
    "Significant-tornado parameter of 4.0 with storms ongoing — environment strongly supports tornadic supercells.",
    "MRMS MESH of 2.3\" hail within radius in the last hour.",
    "Intense rotation track (azimuthal shear 0.013 /s) nearby in the last hour.",
    "6 tornado report(s) near the point in the report window."
  ],
  "hazards_ranked": [
    {"hazard": "tornado", "level": "extreme"},
    {"hazard": "hail", "level": "extreme"},
    {"hazard": "damaging_wind", "level": "extreme"},
    {"hazard": "flash_flood", "level": "moderate"}
  ],
  "nearest_storm_signature": {
    "signature": "composite reflectivity", "value": "58.5 dBZ",
    "distance_km": 18.0, "direction": "ENE", "valid_utc": "2026-06-10T22:14Z"
  },
  "attention_window": {"window": "now", "until_utc": "2026-06-10T21:00:00-05:00"}
}
```

And the same tool for a quiet coastal Maine point reads as confidently quiet — not as an error: `"threat_level": "none"` with the environment numbers shown so the agent can see *why* it's quiet.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API keys — every data source is public and anonymous. `uvx` downloads and runs the published package in one step; nothing is installed permanently.

**Claude Code:**

```sh
claude mcp add shearline -- uvx shearline
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "shearline": {
      "command": "uvx",
      "args": ["shearline"]
    }
  }
}
```

**Streamable HTTP** (for remote/agent-platform use):

```sh
uvx shearline --http --port 8741
# serves at http://127.0.0.1:8741/mcp
```

To run the latest unreleased `main` instead of the PyPI release, swap `shearline` for `--from git+https://github.com/lostnumber07/shearline shearline`.

## Why these tools

A forecast API tells you it might rain. None of the questions that matter on a severe weather day — *is this storm rotating, how big is the hail, is the environment loaded for tornadoes, am I inside the polygon* — are answerable from a forecast endpoint. They require the warning's IBW tags, radar-derived products, and a real sounding:

- **Warnings with IBW tags, not just warning text.** A base-tier Severe Thunderstorm Warning and one tagged `DESTRUCTIVE` with 80 mph gusts are different planning problems. SHEARLINE parses the machine-readable tags (max hail size, max gust, tornado detection/damage threat) and the storm-motion vector, and does the point-in-polygon test for you.
- **The environment, computed honestly.** CAPE without shear is a pulse-storm day; shear without CAPE is wind-driven rain. SHEARLINE pulls the current RAP analysis profile and computes the discriminating quantities with MetPy — including the effective inflow layer, effective SRH/shear, SCP, and STP — because high-CAPE/low-shear, low-CAPE/high-shear, and classic supercell parameter spaces produce very different hazards, and the interpretation says which one you're in.
- **MRMS, because warnings lag storms.** MESH tells you what hail a storm has *already* produced; rotation tracks show where mesocyclones have tracked in the last hour — both on a ~2-minute cadence from the national radar mosaic, often ahead of the next warning update.
- **LSRs, because radar isn't ground truth.** Spotter reports confirm what's actually reaching the ground.
- **One brief that reasons across all of it.** The threat level is rule-based with the triggered rules quoted back, so an agent can audit the logic instead of trusting a vibe.

## Data sources (all public, no keys)

- Warnings: [api.weather.gov](https://www.weather.gov/documentation/services-web-api) (NWS)
- Outlooks: [Storm Prediction Center](https://www.spc.noaa.gov/) public GeoJSON
- Point environment: [NOMADS](https://nomads.ncep.noaa.gov/) RAP grib filter, derived with [MetPy](https://unidata.github.io/MetPy/)
- MRMS: [NOAA MRMS on AWS Open Data](https://registry.opendata.aws/noaa-mrms-pds/)
- Storm reports (real-time and historical): [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) LSR service
- NEXRAD Level 2: [Unidata on AWS Open Data](https://registry.opendata.aws/noaa-nexrad/)
- Lightning: [GOES GLM on AWS Open Data](https://registry.opendata.aws/noaa-goes/) (GOES-East GLM-L2-LCFA)

Coverage is **continental US only** — out-of-bounds coordinates are rejected with a clear error. Upstream fetches are cached (warnings 60 s, MRMS 120 s, LSRs 300 s, outlooks/RAP 30 min) and degrade gracefully: if one source is down, you get partial data plus a `degraded` field, never a bare exception.

## Recipes for non-meteorologists

You don't need to know what an STP is to use SHEARLINE. The [`.claude/skills/`](.claude/skills)
directory ships three end-to-end recipes that name the exact tool sequence for a
domain task — drop them into any agent that has SHEARLINE connected:

- **[hail-claim-verification](.claude/skills/hail-claim-verification/SKILL.md)** — did damaging hail occur at this address on this date? (insurance / forensic)
- **[chase-day-briefing](.claude/skills/chase-day-briefing/SKILL.md)** — outlook → environment → trend → warnings → radar, into a go/no-go with a target window (chase / EM)
- **[event-day-lightning-watch](.claude/skills/event-day-lightning-watch/SKILL.md)** — poll lightning proximity and issue suspend/shelter/resume calls by the 30-30 / 10-mile rules (venues / outdoor ops)

## Architecture

SHEARLINE is a thin, layered async server: per-source fetch/parse modules feed a meteorology derivation layer, which feeds a uniform tool layer. Every tool returns the same `{data, interpretation, degraded, disclaimer}` envelope, every upstream call is TTL-cached, and one failing source degrades to partial data instead of an exception. See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the module map, the request lifecycle of `get_threat_brief`, the concurrency model, and the upstream quirks each source module encodes.

## Development

```sh
git clone https://github.com/lostnumber07/shearline && cd shearline
uv sync
uv run pytest          # offline test suite against recorded fixtures
uv run ruff check .
uv run shearline       # stdio
uv run python scripts/smoke.py   # live smoke test, both transports
```

See [ARCHITECTURE.md](ARCHITECTURE.md#adding-a-tool) for how to add a tool or data source.

## License

MIT © Backshear LLC. Weather data is produced by NOAA/NWS and other public services; this project is not affiliated with or endorsed by NOAA.
