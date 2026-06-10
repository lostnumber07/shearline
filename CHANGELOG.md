# Changelog

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
