# SHEARLINE Architecture

This document explains how SHEARLINE is put together: the layers, the contract
every tool obeys, how a request flows through the system, and the upstream
quirks each source module exists to absorb. It is the map you want before
changing code or adding a tool.

For *what* the tools do and *why* these tools, see the [README](README.md). For
*how they're built*, read on.

---

## Design goals

1. **Analyst-grade, not forecast-grade.** Ship only what requires radar
   meteorology to expose correctly (warnings' IBW tags, derived environments,
   MRMS, LSRs). Skip the basic forecast API that a dozen other MCPs already wrap.
2. **No API keys, ever.** Every upstream is public and anonymous.
3. **Uniform, self-describing output.** Every tool returns the same envelope:
   numeric `data` with units, plain-language `interpretation`, a `degraded`
   list, and a fixed safety `disclaimer`.
4. **Degrade, don't crash.** One upstream failing yields partial data plus a
   `degraded` marker — never a bare exception, and never a false "all-clear."
5. **CONUS-only and explicit about it.** Out-of-bounds points are rejected with
   an actionable error.
6. **Empirically grounded.** Every upstream path, field name, unit, and sentinel
   was verified against live endpoints at build time, not recalled from memory.
   Those findings are encoded as module docstrings and the offline `fixtures/`.

---

## Layered structure

```
                         ┌──────────────────────────────────────────────┐
   MCP client (agent) ───▶              server.py  (tool layer)          │
   stdio / HTTP         │ 10 @mcp.tool() coroutines · CONUS bounds check  │
                         │  envelope assembly · degraded aggregation       │
                         └───────┬───────────────────────────┬───────────┘
                                 │                            │
                  ┌──────────────▼─────────────┐   ┌──────────▼───────────┐
                  │   derive/  (meteorology)    │   │  payload builders     │
                  │  environment.py · threat.py │   │  (in server.py)       │
                  │  MetPy math · threat logic   │   │  shape + interpret    │
                  └──────────────┬──────────────┘   └──────────┬───────────┘
                                 │                              │
                  ┌──────────────▼──────────────────────────────▼──────────┐
                  │                  sources/  (per-upstream)                │
                  │  nws · spc · rap · mrms · iem · nexrad                   │
                  │  fetch + parse + normalize one upstream each            │
                  └──────────────┬──────────────────────────────┬──────────┘
                                 │                              │
                  ┌──────────────▼─────────┐      ┌─────────────▼──────────┐
                  │  fetch.py  (async HTTP) │      │  boto3 (anonymous S3)   │
                  │  shared client · retry  │      │  MRMS · NEXRAD buckets  │
                  └──────────────┬──────────┘      └─────────────┬──────────┘
                                 │                              │
                  ┌──────────────▼──────────────────────────────▼──────────┐
                  │                   cache.py  (TTL cache)                   │
                  │  async-safe · per-key locks · eviction on access         │
                  └──────────────────────────────────────────────────────────┘

      cross-cutting helpers:  bounds.py (CONUS gate) · geo.py (haversine/bearing)
                              envelope.py (response contract) · __init__.py (version)
```

The dependency rule is strictly downward: `server` → `derive` → `sources` →
`fetch`/`boto3` → `cache`. Nothing lower reaches up. `bounds`, `geo`, and
`envelope` are leaf utilities anyone may use.

### Module responsibilities

| Module | Lines | Responsibility |
| --- | ---: | --- |
| `server.py` | ~540 | The MCP tools, the CONUS gate on every entry, the per-tool **payload builders** (which call sources + derive and assemble the envelope), and the `get_threat_brief` fan-out. The only file that imports `FastMCP`. |
| `derive/environment.py` | ~328 | RAP profile → severe-weather parameters via **MetPy** (CAPE/CIN family, LCL, shear, SRH, Bunkers motion, effective inflow layer, SCP, fixed- and effective-layer STP), plus the analyst-voice interpretation that names the parameter-space regime. |
| `derive/threat.py` | ~480 | Pure synthesis: combines the six source payloads (warnings, outlook, environment, MRMS, reports, lightning) into a rule-based threat level with quoted logic, ranked hazards, nearest storm signature, lightning summary, and attention window. No I/O — trivially testable. |
| `derive/trend.py` | ~110 | Pure: reduces a series of forecast-hour environments to the discriminating quantities and describes their trajectory (intensifying / stabilizing / steady). No I/O. |
| `sources/nws.py` | ~174 | api.weather.gov alerts: warning polygons, IBW tag parsing, storm-motion decode, point-in-polygon, severe-event whitelist. |
| `sources/spc.py` | ~158 | SPC outlook GeoJSON layers: categorical + probability point lookup, the `cig*` significant-severe migration, stale-relic guarding. |
| `sources/rap.py` | ~192 | NOMADS RAP grib-filter download + cfgrib decode to a single-point vertical profile. |
| `sources/mrms.py` | ~312 | MRMS products from S3: latest-file discovery, gzip+grib decode, radius sampling with the product-specific units and sentinels. |
| `sources/iem.py` | ~166 | IEM Local Storm Reports: point+radius query, type/magnitude/unit normalization, counts. |
| `sources/nexrad.py` | ~289 | NEXRAD Level 2 metadata: nearest-site lookup from a bundled station table, latest-volume fetch, MetPy volume parse (VCP, max dBZ, echo top). |
| `sources/lightning.py` | ~230 | GOES GLM lightning from S3: window granule discovery (s-tag start time), per-granule flash sampling within radius (netCDF via h5netcdf), tiered outdoor-safety output. |
| `fetch.py` | ~61 | One shared `httpx.AsyncClient`, polite User-Agent, cached JSON/bytes GETs with a single transient retry. |
| `cache.py` | ~71 | Async-safe TTL cache with per-key locks (no thundering herd) and eviction-on-access. |
| `bounds.py` | ~24 | CONUS bounding box; raises `OutOfBoundsError` with an actionable message. |
| `geo.py` | ~44 | Haversine distance, initial bearing, 16-point compass. |
| `envelope.py` | ~38 | The `{schema_version, data, interpretation, degraded, disclaimer}` builder and the exact disclaimer string. |
| `observability.py` | ~110 | HTTP-only structured per-tool JSON logging via the `@observe` wrapper (tool, coarse lat/lon, latency, degraded, cache hit/miss). Passthrough until `enable()`d, so stdio is unaffected. |
| `ratelimit.py` | ~110 | Pure-ASGI per-client token-bucket rate limiter wrapping the HTTP app; `429` + `Retry-After` on exceed. |

---

## The response envelope

Every tool returns this shape (assembled by `envelope.py`):

```jsonc
{
  "schema_version": "1.0",                  // semver of the I/O contract
  "data":           { /* numeric/structured fields, units in the key names */ },
  "interpretation": "2-5 plain-language analyst sentences",
  "degraded":       ["source-id", ...],   // empty when all upstreams healthy
  "disclaimer":     "Informational only. Not a substitute for official NWS warnings."
}
```

- `data` keys carry their units (`max_mesh_mm`, `srh_0_1km_m2s2`,
  `bulk_shear_0_6km_kt`) so a consumer never has to guess.
- `interpretation` is generated by the same module that produced the data, so it
  always reflects the actual numbers — including the degraded case, where it must
  say "status unknown," never "all clear."
- Tools are annotated `-> dict[str, Any]` so the MCP SDK emits a structured
  `outputSchema` and `structuredContent`, not just text.

---

## Stability contract

The envelope is a **public contract**: integrators may depend on the field names.
`schema_version` (in `envelope.py`, currently `1.0`) tracks that contract under
semantic versioning:

- **MAJOR** bump — a *breaking* change: renaming/removing/retyping a `data` field,
  removing a tool, or renaming/removing/retyping a tool parameter.
- **MINOR** bump — a *backward-compatible addition*: a new tool, a new optional
  parameter, or a new `data` field that doesn't disturb existing ones.

Two guards keep the contract honest:

1. **`tests/test_schema_lock.py`** snapshots every tool's MCP `inputSchema`
   (parameter names/types/defaults/required) and `outputSchema`, plus
   `schema_version`, into `tests/tools_schema_snapshot.json`. Any change to the
   *call* surface fails the suite (offline, in CI) with a unified diff. Making the
   change deliberately means bumping `SCHEMA_VERSION` and regenerating:
   `UPDATE_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_lock.py` — so the
   version bump and the contract change land together in one reviewable diff.
2. **`scripts/canary.py`** asserts the presence and type of each tool's key `data`
   fields live, so an output-field rename surfaces there too.

The MCP `outputSchema` is generic (`object`/`additionalProperties`) because tools
return `dict[str, Any]`; the *field-level* output contract is therefore guarded by
the canary's field checks and each tool's offline tests rather than by the
declared schema alone.

---

## Request lifecycle

### A single tool (e.g. `get_mrms_severe`)

1. `server.get_mrms_severe` runs `check_conus(lat, lon)` — out-of-bounds raises
   immediately, before any network call.
2. It calls the payload builder `_mrms_payload`, which fans the five MRMS
   products out concurrently with `asyncio.gather(..., return_exceptions=True)`.
3. Each `mrms.sample_product` goes through `cache.CACHE.get_or_fetch`: on a miss
   it lists the S3 prefix and downloads the newest gzip'd grib (in a worker
   thread — see Concurrency), decodes it, and samples the max within radius.
4. Any product that raised becomes a `degraded` entry; the rest are shaped by
   `mrms.shape_results` and interpreted by `mrms.interpret`.
5. `envelope(...)` wraps it. Done.

### The composite (`get_threat_brief`)

```
get_threat_brief(lat, lon)
   check_conus
   asyncio.gather(            ← all six run concurrently
       _warnings_payload(40km),
       _outlook_payload(day 1),
       _environment_payload(),
       _mrms_payload(40km),
       _reports_payload(80km, 6h),
       _lightning_payload(40km, 15min),
       return_exceptions=True)
   │
   ├─ collect degraded from each sub-payload + any that threw
   └─ threat.build_threat_brief(...)   ← pure synthesis, no I/O
          rule cascade → level + quoted logic
          hazard scoring → ranked hazards
          nearest qualifying storm signature
          attention window (UTC-correct expiry)
   envelope(data, interpretation, degraded)
```

The brief reuses the *same* payload builders as the individual tools, so its
sub-results are identical to what those tools would return standalone, and the
TTL cache means calling the brief and then a single tool re-hits nothing.
(`get_radar_snapshot` is intentionally **not** part of the brief — it downloads a
multi-MB Level 2 volume and is a separate on-demand tool.)

---

## Caching & TTLs

All upstream reads pass through `cache.TTLCache` (a module-level singleton,
`cache.CACHE`). Per the freshness of each product:

| Constant | TTL | Applies to |
| --- | ---: | --- |
| `TTL_ALERTS` | 60 s | NWS active alerts |
| `TTL_MRMS` | 120 s | MRMS products (≈2-min upstream cadence) and NEXRAD volumes |
| `TTL_LSR` | 300 s | IEM storm reports |
| `TTL_OUTLOOK` | 1800 s | SPC outlook layers |
| `TTL_RAP` | 1800 s | RAP profile subsets |
| `TTL_HISTORICAL` | 21600 s | past storm-report windows (effectively immutable) |

Properties that matter:

- **Per-key locks** collapse concurrent callers of the same key into exactly one
  upstream fetch (no thundering herd) — important because `get_threat_brief`
  and a follow-up single tool can race on the same key.
- **Eviction on access and on insert** keeps a long-running `--http` server from
  pinning expired multi-MB payloads (NEXRAD volumes, RAP subsets, and the
  per-2-minute-rotating MRMS sample keys).

---

## Graceful degradation

The invariant: **a tool never raises to the client, and a feed outage never
reads as an all-clear.** Three layers enforce it:

1. **Per-source try/except** in each payload builder appends a `degraded`
   id rather than propagating. The threat brief additionally catches a whole
   sub-payload dying and records it.
2. **Degraded-aware interpretation.** `_interpret_warnings` and
   `_interpret_outlook` receive the `degraded` list. If the alert feeds are
   unreachable they return "WARNING STATUS UNKNOWN … do not treat as a confirmed
   all-clear," not the quiet-day text. This is a safety property, tested
   explicitly.
3. **Stale-relic guarding** (SPC). SPC keeps serving HTTP 200 for retired layer
   files; every hazard layer's `VALID` is compared to the categorical layer's,
   and a mismatch is dropped as stale. If the categorical layer itself is
   unreachable, hazard layers are skipped (staleness can't be verified) rather
   than risk presenting a frozen relic as current.

---

## Concurrency model

The server is `async`, but two kinds of work block:

- **Network I/O** uses `httpx.AsyncClient` (a single shared client in `fetch.py`)
  and is naturally concurrent.
- **CPU/blocking work** — cfgrib/eccodes GRIB decode (RAP, MRMS), MetPy
  computation, MetPy Level 2 parsing, and **boto3 S3 calls** — is pushed off the
  event loop with `asyncio.to_thread`. The MRMS tool runs five product samples
  in parallel threads; the threat brief runs six sources in parallel.

Because boto3 **client creation** is not thread-safe, `mrms.py`, `nexrad.py`,
and `lightning.py` guard their lazy client init with a `threading.Lock` and use a
dedicated `boto3.Session`.

A per-event-loop **upstream-concurrency semaphore** (`fetch.py`,
`SHEARLINE_UPSTREAM_CONCURRENCY`, default 8) bounds the number of simultaneous
outbound HTTP fetches as politeness toward NWS/SPC/NOMADS/IEM — the cache
already collapses *duplicate* fetches, and this caps *distinct* ones across all
clients.

### HTTP transport hardening (Task 7)

The `--http` path (`server._run_http`) wraps FastMCP's `streamable_http_app()`
with the `ratelimit.RateLimitMiddleware` and enables `observability` (the
`@observe` per-tool logger). Both are **strictly HTTP-only**: stdio runs
`mcp.run()` with observability disabled and no middleware, so its stdout stays a
clean JSON-RPC stream. Cache hit/miss counts come from a `ContextVar` the
`@observe` wrapper scopes per call and `cache.get_or_fetch` increments.

---

## Upstream sources and the quirks they absorb

Each source module exists to turn a messy real-world feed into clean, normalized
data. The non-obvious behavior each one encodes (all verified live at build
time; recorded in `fixtures/`):

| Source | Endpoint | Quirks handled |
| --- | --- | --- |
| **NWS** | `api.weather.gov/alerts/active` | User-Agent is mandatory (403 HTML without). `parameters` values are always string-arrays. IBW keys are conditionally present (no key = base tier). `eventMotionDescription` is `DEG/KT` machine encoding with lat-first centroids. Watches are zone-based (null geometry). Only severe-convective events count as warnings — Winter Storm/Flood/Fire products are filtered out. |
| **SPC** | `spc.noaa.gov/products/outlook/*.lyr.geojson` | `sig*` significant layers were replaced by `cig*` (Conditional Intensity Groups) in 2026-03; old files still 200 but are frozen → stale-guarded. Categorical DN skips 7 (HIGH=8). Probability `LABEL` is a fraction string; a CIG feature shares DN=2 with the 2% contour → disambiguated by LABEL. Layers are nested "wedding cake" → take max containing category. |
| **RAP** | NOMADS `filter_rap.pl` | f00 analysis posts ~48 min after the cycle hour → walk back up to 4 h, rolling into the previous day's dir near 00Z. No dewpoint/specific-humidity aloft (RH only) → dewpoint derived. cfgrib silently drops conflicting hypercubes → each level group opened separately. Decoded lons are 0–360 E on a 2-D Lambert grid. Forecast hours fXX share the same filename/params/decode (only `f00→fXX` changes); range is cycle-dependent (f21 vs f51), and fXX post over ~10 min out of order, so the trend series anchors to the latest cycle for which **all** hours are present. |
| **MRMS** | `s3://noaa-mrms-pds` (anon) | cfgrib names every field `unknown` → product identity comes from the S3 key. Lats descending, lons 0–360. MESH/VIL on a 0.01° grid, rotation tracks on 0.005°. Sentinels differ by product (−3/−1 vs −999/−99 vs 0). Rotation values are in 0.001/s. Irregular-second filenames → latest = lexicographic max of the day folder. |
| **IEM** | `mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point` | Time filter is silently ignored unless **both** `begints` and `endts` are sent. Magnitude semantics vary by type (hail inches, wind mph, tornado null). Case-varying unit strings. The same endpoint serves arbitrary past single-day windows (historical tool) back to ~2005; there is **no** server-side row cap, so the historical path keeps the radius small and the window to one day. `product_id` can lag the event by days — always use `valid` for the event time. |
| **NEXRAD** | `s3://unidata-nexrad-level2` (anon) | The old `noaa-nexrad-level2` bucket 403s since 2025-09 → use `unidata-`. `_MDM` sidecar files must be filtered. Classify sites by the station table's STNTYPE, not ICAO (TJUA is a T-prefixed WSR-88D); skip ROC/NSSL test radars (KCRI, KOUN). Echo top is a coarse 4/3-earth beam-height estimate, range-capped to avoid distant-precip artifacts. |
| **GOES GLM** | `s3://noaa-goes19` (anon) | Operational GOES-East is **goes19**, not goes16/17 (do not hardcode the satellite from memory — the canary watches for it going empty). Keys carry an s-tag start time (year/day-of-year/HH:MM:SS/tenths); granules are ~20 s, keys sort chronologically. File is netCDF4 → `h5netcdf` engine (extra deps). flash lon is −180..180; GLM is **total** lightning (in-cloud + CG), not CG-only. |

---

## Safety invariants

These are non-negotiable and test-enforced where possible:

1. **CONUS gate on every tool** — `check_conus` runs before any network call.
2. **Disclaimer on every response** — the exact string lives once in
   `envelope.DISCLAIMER`.
3. **No false all-clear** — degraded alert/outlook feeds produce "status
   unknown," not quiet-day text.
4. **No keys, no persistence, no outbound writes** — SHEARLINE only reads public
   data.

---

## Testing strategy

- **Offline and deterministic.** The suite (≈141 tests) runs with no network:
  HTTP is mocked with `respx`, S3/GRIB paths use real recorded `fixtures/`
  (captured during an actual 2026-06-10 tornado outbreak), and `cache.CACHE` is
  cleared between tests.
- **`derive/threat.py` is pure**, so it's tested exhaustively with synthetic
  envelope dicts across the whole rule cascade (every level, every escalation).
- **`derive/environment.py`** is regression-tested against the Moore OK RAP
  fixture with generous tolerances, plus synthetic-profile tests for each
  interpretation regime branch.
- **CI** (`.github/workflows/ci.yml`) runs `ruff` + `pytest` on Ubuntu, which
  also confirms the `eccodeslib` GRIB binary decodes on Linux, not just macOS.
- **`scripts/smoke.py`** is a live end-to-end check over both transports
  (acceptance gates: cold start < 10 s, cached call < 500 ms).
- **`scripts/canary.py`** is a *live* upstream drift detector (the offline suite
  can't see NOAA changing a field or moving a bucket). It hits every source once
  for a fixed quiet CONUS point and asserts the **shape** of a healthy response
  — envelope keys, expected `data` fields, correct types — never specific values
  or "weather present." It distinguishes transient outage (one retry) from
  schema drift (fail hard with a diff). A moved bucket / renamed file surfaces as
  persistent degradation and also fails the run. `.github/workflows/canary.yml`
  runs it on a daily cron (+ manual dispatch) and opens a tracking issue on
  failure. The expected-schema spec lives inline at the top of the script.

---

## Adding a tool

1. Write a **payload builder** `_xxx_payload(...)` in `server.py` that calls into
   `sources/` and/or `derive/`, and returns `envelope(data, interpretation,
   degraded=...)`. Keep all I/O behind `cache.CACHE.get_or_fetch`; push any
   blocking decode/compute through `asyncio.to_thread`.
2. Add the thin `@mcp.tool()` coroutine that calls `check_conus`, clamps any
   radius/time args with `_clamp`, and delegates to the builder. Annotate it
   `-> dict[str, Any]`.
3. Write the `interpretation` next to the data so it always matches the numbers,
   including the degraded path.
4. Add offline tests: mock HTTP with `respx` or add a real fixture; clear the
   cache between tests.

## Adding a data source

1. Create `sources/<name>.py` that fetches and **normalizes** one upstream into
   plain Python (floats, units stated). Document every verified quirk — units,
   sentinels, grid orientation, latest-file logic — in the module docstring, the
   way the existing source modules do.
2. Route all reads through `fetch.py` (HTTP) or a lock-guarded anonymous boto3
   client (S3), with the appropriate `TTL_*`.
3. Record a real response into `fixtures/` and test against it offline.
4. Surface it through a payload builder + tool as above.
