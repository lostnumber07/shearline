# Changelog

## v1.1.0 — 2026-06-13

Backward-compatible feature release — existing tools and fields are unchanged.

- **New tools:**
  - `get_historical_storm_reports(lat, lon, date, radius_km=80)` — what hail/wind/
    tornado hit a point on a specific past date (IEM LSRs, ~2005 onward).
  - `get_environment_trend(lat, lon)` — RAP forecast-hour series (f00/f01/f03/f06)
    of MLCAPE/shear/SRH/SCP/STP with a trajectory interpretation.
  - `get_lightning(lat, lon, radius_km=40, minutes=15)` — GOES-East GLM total
    lightning with a tiered outdoor-safety interpretation; also folded into
    `get_threat_brief`.
- **Envelope:** added `schema_version` ("1.0"); the tool I/O contract is now under
  a documented semver stability policy, guarded by a schema-lock test.
- **Hosting:** the `--http` transport now emits structured per-request JSON logs
  and applies a per-client token-bucket rate limit (both HTTP-only; stdio
  unchanged), plus an upstream-concurrency cap. Configurable via
  `SHEARLINE_RATE_RPM`/`_BURST`, `SHEARLINE_HTTP_LOG`, `SHEARLINE_LOG_LEVEL`,
  `SHEARLINE_UPSTREAM_CONCURRENCY`.
- **Reliability:** a daily live "canary" workflow (`scripts/canary.py`) checks
  every upstream's response shape and fails on schema drift (renamed fields,
  moved buckets) before it reaches users.
- **Recipes:** three `.claude/skills/` domain recipes (hail-claim verification,
  chase-day briefing, event-day lightning watch).
- New dependencies: `h5netcdf`, `h5py` (GOES GLM netCDF decode). Upstreams added:
  GOES GLM on AWS Open Data (operational GOES-East = `noaa-goes19`).

## v1.0.0 — 2026-06-10

Initial release.

- Seven tools: `get_active_warnings`, `get_spc_outlook`, `get_point_environment`,
  `get_mrms_severe`, `get_storm_reports`, `get_threat_brief`, `get_radar_snapshot`.
- stdio and streamable-HTTP transports (`shearline` / `shearline --http --port 8741`).
- No API keys: NWS api.weather.gov, SPC GeoJSON, NOMADS RAP grib filter,
  MRMS + NEXRAD Level 2 on AWS Open Data (anonymous), IEM Local Storm Reports.
- Every tool returns `data` + `interpretation` + `degraded` + safety disclaimer;
  CONUS-only with clear out-of-bounds errors; TTL-cached upstream fetches with
  graceful degradation.
- Environment derivations via MetPy: MLCAPE/MUCAPE/SBCAPE + CINs, LCL, bulk
  shear, SRH, Bunkers motion, effective inflow layer, effective SRH/shear,
  SCP, fixed-layer and effective-layer STP.
- Upstream contracts verified empirically at build time, including the 2026
  SPC `cig*` significant-severe layer migration (frozen `sig*` relics guarded),
  the IEM begints+endts requirement, and the NEXRAD bucket move to
  `unidata-nexrad-level2`.
- Offline test suite against recorded real-weather fixtures; live smoke test
  script for both transports.
