# FireWatch — build roadmap

Implementation plan for **PS 26162**. Each stage is independently runnable and has a
pass/fail acceptance test. Design rules live in `CLAUDE.md`; where this file and
`CLAUDE.md` disagree, `CLAUDE.md` wins.

Revised 2026-09-29 after a design review — see `CLAUDE.md` → *Changed decisions*.

---

## Ground rules

**Real data first.** The FIRMS archive for India is public (yearly CSVs, no key), so every
stage from Stage 2 on is built and tested against real detections. The synthetic
generator shrinks to a test fixture (`MOCK_MODE=1`) for fast, offline, deterministic
tests. Anomaly detection is tested by injecting synthetic spikes into *real* source
histories.

**Every stage ends with something runnable.** No stage is "write the classes for X".
Each produces a command you can execute and a number you can check.

**Two runners, same targets.** `make <target>` and `.\make.ps1 <target>` (Windows,
PowerShell 5.1 compatible).

**A stage passes only with the database tests running.** Skipped DB tests are not a pass.

**Cut from the bottom.** Stages 0–6 and 8–9 carry the graded deliverables. Stage 7 is
differentiation and goes first. Stage 10's headline metric is not optional.

---

## Status

| Stage | State |
|---|---|
| 0 | **Done 2026-09-29.** Acceptance passed on a real database: portable PostgreSQL 16.15 + PostGIS 3.6 (`scripts/local_postgres.ps1`); 86 passed, 1 skipped (hypertable test, TimescaleDB not in the portable build). Docker Desktop still to be installed for the demo |
| 1.5 | Done 2026-09-29 — `reports/stage1_5_spike.md` (one year) |
| 1.5b | Done and approved 2026-09-29 — `reports/stage1_5b_multiyear.md`. Gate and three-class set approved |
| 1 | Done 2026-09-29 — `firewatch/ingest/fixture.py`, `make fixture`; 24 fixture tests pass |
| 2 | **Done 2026-09-29.** Real archive loaded: 12.55M detections (2012–2024), 4.56M cloud cell-days, 47,970 OSM features, 916 GIHS objects; `check_ingest.py` passes |
| 3 | **Done 2026-09-29.** 557 sources; every floor passes (`reports/stage3_registry.md`). Gate recalibrated to ≥ 3 years |
| 4–10 | Not started |

---

## Repository layout

```
firewatch/
├── docker-compose.yml          # postgis (+ timescaledb), one command to start
├── Makefile, make.ps1          # same targets; make.ps1 for Windows
├── requirements.txt            # pinned; Python 3.13
├── .env.example
├── sql/
│   ├── 001_schema.sql          # TimescaleDB optional
│   ├── 002_indexes.sql
│   └── 003_critical_assets.sql
├── firewatch/
│   ├── config.py               # env-driven; MOCK_MODE (fixture) flag lives here
│   ├── db.py                   # connection pool, upsert helpers
│   ├── grid.py                 # EPSG:7755 metres, 375 m cells, cloud-grid cells: one implementation
│   ├── ingest/
│   │   ├── normalize.py        # column normalisation, SP/NRT dedupe
│   │   ├── fixture.py          # synthetic test fixture + spike injector
│   │   ├── firms_archive.py    # public yearly CSVs
│   │   ├── firms_api.py        # 2025+ and live NRT (needs FIRMS_MAP_KEY)
│   │   ├── vnf.py              # optional enrichment
│   │   ├── osm.py              # Geofabrik + pyosmium
│   │   ├── landcover.py        # WorldCover sampling
│   │   ├── fsi.py
│   │   ├── gihs.py             # evaluation reference
│   │   └── observability.py    # daily cloud amount via NASA POWER
│   ├── registry/
│   │   ├── cells.py            # 375 m grid + recurrence gate
│   │   ├── cluster.py
│   │   ├── fingerprint.py
│   │   └── baseline.py
│   ├── models/
│   │   ├── labels.py           # weak labels + LABEL_INPUTS
│   │   └── source_clf.py       # Model 1 — the only learned model
│   ├── inference/
│   │   ├── router.py
│   │   ├── road_a.py           # rules + physics; emits the reason text
│   │   ├── anomaly.py          # Road C, two tiers
│   │   ├── promotion.py        # provisional sources that never silence alerts
│   │   └── events.py
│   ├── risk/
│   │   ├── assets.py
│   │   └── score.py
│   └── api/
│       ├── main.py
│       └── tiles.py
├── web/                        # MapLibre frontend + offline basemap
├── scripts/                    # thin CLI wrappers: migrate.py, spike_cluster.py, ...
├── reports/
└── tests/
```

---

## Stage 0 — Skeleton and database

**Build:** repo structure, `docker-compose.yml` (PostGIS + TimescaleDB), `requirements.txt`,
`.env.example`, `config.py`, `db.py`, the SQL schema, `Makefile`.

**Fixes (2026-09-29):** Python 3.13 venv; `make.ps1`; `scripts/migrate.py`; TimescaleDB
optional; `instrument`, `firms_type`, `version` and `product` columns; SP/NRT dedupe
(`firewatch/ingest/normalize.py`); the upsert test rewritten against the `detections`
natural key so it can actually run; credentials checked only when they are used.

**Accept when:**
```bash
make install && make db && make migrate && make test     # or: .\make.ps1 <target>
```
All tests pass **with the DB tests running** — none skipped. Then in `make psql`: `\dt`
shows `detections, sources, events, observability, critical_assets, osm_industrial,
forest_boundary, fsi_alerts`, and `SELECT PostGIS_Version();` answers.

**Blocked by:** Docker Desktop (a manual install). **Effort:** half a day, done.

---

## Stage 1 — Test fixture and spike injector

**Build:** a small synthetic generator that emits FIRMS-shaped rows — both MODIS and VIIRS
column names, SP and NRT versions, missing temperatures — for unit tests. Biomass fires are
spatially diffuse (fields scattered across a region, forest fires spreading across
pixels), **never point sources**. Plus `inject_spikes(history, ...)`, which adds
excursions of known size and time to a real source's history and returns the ground
truth.

**Produces:** `firewatch/ingest/fixture.py`, `firewatch/grid.py`,
`scripts/generate_fixture.py` (`make fixture` → `data/mock`), `tests/test_fixture.py`

**Accept when:** `make test` normalises fixture rows from both sensors through
`normalize.py`, and injected spikes round-trip to their ground truth.

**Blocked by:** Stage 0. **Effort:** half a day.

**Result (2026-09-29):** done; 24 tests. The fixture is 2 years with ~33k
detections, generated in a few seconds and deterministic per seed. It reproduces
what real data taught us, and the tests pin each property:
- 32% of detections at night
- VNF temperatures on 9% of detections
- raw DBSCAN chains the paddy belt into one blob wider than 10 km
- the recurrence gate drops the belt, keeps the refinery inside it, and refuses
  both the five-month blowout and the new flare

The scripted events are Road A and Road C test cases: a furnace fire on two passes,
a flare blast on one, a warehouse fire outside a fence, a Baghjan-dated blowout, and
a new flare. It also writes mock OSM facilities and land cover for Stage 2's mock
mode.

A fixture test caught a projection bug that affected every stage: the per-point
equirectangular formula sheared distances by ~300 m per 500 m north–south. It is
now EPSG:7755 in `firewatch/grid.py`, and the spikes were rerun (see CLAUDE.md,
changed decision 17).

---

## Stage 1.5 — Real-data spike (one year, no database)

**Build:** pull one year of VIIRS for India, normalise it with the production code, and run:
(A) the original plan — raw DBSCAN, `eps=500` m, `min_samples=5`, `ball_tree`;
(A′) the same with `kd_tree`;
(B) `min_samples=1` on one year, a conservative stand-in for ten years of accumulation;
(C) 375 m cells + recurrence gate + DBSCAN with `sample_weight`.
Measure peak memory per run in a fresh process. Show Jamnagar, the Punjab paddy belt and
Jharia. Compare against FIRMS `type=2` and GIHS.

**Produces:** `scripts/spike_cluster.py`, `reports/stage1_5_spike.md`,
`reports/stage1_5/*.png`

**Accept when:** the report answers, with numbers: (1) does raw clustering chain the paddy
belt? (2) does `ball_tree` reduce memory? It also proposes starting gate thresholds for
Stage 3.

**Blocked by:** Stage 0 fixes only — it runs in pandas. **Effort:** one day.
**Stage 3 does not start until the user has reviewed this.**

**Result (2026-09-29):** both problems are real.
- **Problem 1.** On one year the paddy belt fragments into thousands of field
  clusters, with 60% of its detections inside "sources". On three years (Stage 1.5b)
  it merges into **one 369 km cluster** holding 97% of them.
- **Problem 2.** `ball_tree` ≈ `kd_tree` ≈ 1.32 GB, and memory grows faster than the
  data.
- The gated method matches 99% of FIRMS-static detections.
- Proposed gate: ≥ 4 months on ≥ 10 days within a year, in ≥ 2 years (moved to ≥ 3
  months in Stage 1.5b).

Full numbers are in `reports/stage1_5_spike.md`.

---

## Stage 1.5b — the gate on three years, the demo refineries, the label census

**Build:** rerun the gate sweep on 2021–2023 (months × days × years = 36 settings);
check Reliance Jamnagar, Nayara Vadinar and HMEL Bathinda at their published
coordinates; rerun raw DBSCAN on all three years; and count how many *registry
sources* each OSM label group can label.

**Produces:** `scripts/spike_gate_multiyear.py`, `scripts/spike_labels.py`,
`reports/stage1_5b_multiyear.md`, `reports/stage1_5b/*`

**Result (2026-09-29):**
- **The gate is ≥ 3 months, ≥ 10 days, in ≥ 2 years.** The one-year proposal said
  4 months; a pre-set rule moved it after the three-year sweep (F1 81.6% vs 80.4%).
  - 477 sources, with 0.01% of the paddy belt misrouted.
  - GIHS recall 78%, with 86% of sources on a GIHS site.
  - All three refineries found, with 84%, 90% and 97% of their detections covered.
- **Raw DBSCAN** merges the belt into one 369 km cluster.
- **Label census:** mining 163, heavy industry 215, oil and gas 27, kiln 2. That
  supports the three-class Model 1 (Stage 4).
- All numbers are from reruns with EPSG:7755 distances.

**Stage 3 does not start until the user has reviewed this.**

---

## Stage 2 — Ingestion

**Build:**
- Archive loader for the public yearly CSVs (all years, all sensors) through
  `normalize.py` into `detections`, SP taking precedence over NRT.
- FIRMS API client for 2025 onward and the 3-hourly live NRT pull: rate-limited,
  resumable, needs `FIRMS_MAP_KEY`. Sensor-agnostic, because S-NPP ends 1 Nov 2026.
- VNF loader with the spatio-temporal join — **optional**; skips cleanly without an EOG
  licence.
- OSM via Geofabrik + pyosmium, using the tags in the `CLAUDE.md` label table →
  `osm_industrial`.
- WorldCover sampling at points (cloud-optimised GeoTIFFs, windowed reads).
- GIHS loader (evaluation reference only).
- FSI loader: **deferred.** The FSI portal's point search is an interactive form, and no
  bulk export has been found or verified. Forest labels come from WorldCover tree cover
  until a teammate obtains FSI CSV/KML exports; the `fsi_alerts` table is ready.
- Observability: NASA POWER daily cloud amount (CERES SYN1deg, 1°) → `observability`.
  (Open-Meteo's ERA5 was the first plan; its free quota can't cover the archive.)

**Produces:** everything under `firewatch/ingest/`, `sql/003_ingest.sql`,
`scripts/backfill.py`, `scripts/check_ingest.py` (the checks below, as one script),
`tests/test_ingest.py` (on a throwaway `firewatch_test` database)

**Accept when:**
```bash
make backfill                                   # runs check_ingest.py at the end
psql -c "SELECT instrument, product, count(*), min(acq_datetime), max(acq_datetime)
         FROM detections GROUP BY 1, 2;"
psql -c "SELECT count(*) FROM detections n WHERE n.product = 'NRT' AND EXISTS (
           SELECT 1 FROM detections s WHERE s.product = 'SP' AND s.sensor = n.sensor
             AND s.acq_datetime::date = n.acq_datetime::date);"       # must be 0
psql -c "SELECT daynight, count(*) FROM detections GROUP BY 1;"        # record the split
psql -c "SELECT count(*) FROM observability;"                          # non-zero
```
If VNF is loaded, record its real coverage. It will be far below the old mock's 45%.

**Blocked by:** Stages 0 and 1. **Effort:** two days.

**Result (2026-09-29):** accepted on the real archive; `check_ingest.py` passes.
- **Detections:** 12,554,303.
  - VIIRS SP 11,485,898: S-NPP 2012-01-20 to 2024, NOAA-20 2018 to 2024.
  - MODIS SP 1,068,405: 2012 to 2024.
  - No NRT yet: the API needs `FIRMS_MAP_KEY`.
- **NRT rows on SP-covered days:** 0.
- **Day/night:** 9,158,962 day and 3,395,341 night; **27.0% at night** (29.0% in 2023
  alone).
- **VNF:** not loaded (no licence), so 0% coverage. The pipeline runs without it.
- **Observability:** 4,559,040 NASA POWER cell-days (1°, 2012–2024, 156 tile-years).
  POWER refuses boxes under 2° a side, so the 36–37° N strip is fetched as 36–38° N.
- **OSM** (Geofabrik 2026-09-28): 47,970 labelled features.
  - industrial_other 29,721
  - mining 12,004
  - kiln 5,696
  - thermal_power 376
  - oil_gas 134
  - steel_cement 39
- **GIHS:** 916 India objects, 812 confirmed.
- **Database timezone:** UTC.

---

## Stage 3 — Registry

**Build:**
- 375 m cells.
- The recurrence gate: within a year, ≥ 3 distinct months on ≥ 10 distinct days, in
  at least 2 years (Stage 1.5b), recalibrated here against GIHS on the full archive.
  The recalibration moved it to **≥ 3 years** (see Result).
- DBSCAN on gated cells (`eps=500` m, `min_samples=5` by weight, `sample_weight`).
- A 20 km footprint cap, as a backstop only: the widest gated source in 2023 was
  5.8 km.
- NaN-safe fingerprints.
- Baselines keyed `instrument | daynight | season`, with the n ≥ 30 fallback
  (→ `instrument | daynight` → source-wide).

Porting the Stage 1.5 spike code (`cell_table`, `grid_cluster`) is the starting point.

**Recalibration rule, fixed 2026-09-29 before the full-archive sweep ran.**
- Score years ∈ {2, 3, 4} × months ∈ {3, 4} × days ∈ {10, 20} on all of 2012–2024
  (`scripts/build_registry.py --sweep`).
- Keep the approved gate (≥ 3 months, ≥ 10 days, ≥ 2 years) if it meets every floor
  below.
- Otherwise take the setting that meets every floor with the best F1 of GIHS recall
  and on-GIHS share.
- If no setting meets them all, keep the approved gate and report which floor fails.

**Assignment radius.** A VIIRS detection belongs to a source when it lies within
500 m of one of the source's cells: the router's question. A MODIS detection gets
1 km, because MODIS pixels are 1 km at nadir and larger off-nadir. MODIS feeds only
the MODIS baselines; the gate, the clusters and the fingerprints are VIIRS-only.

**Produces:** `firewatch/registry/` (`cells`, `cluster`, `fingerprint`, `baseline`,
`evaluate`, `build`), `sql/004_registry.sql` (`source_cells`, `registry_runs`),
`scripts/build_registry.py` (`--sweep` for recalibration), `scripts/check_registry.py`,
`tests/test_registry.py`, `reports/stage3/`

**Accept when:**
```bash
make registry
psql -c "SELECT count(*) FROM sources;"
python scripts/check_registry.py
```
`check_registry.py` asserts. The floors come from three years in Stage 1.5b; raise them
if the full archive allows:
- **GIHS recall ≥ 75%** of confirmed India objects active in 2021, within 1 km
- **≥ 85% of sources on a GIHS site** (a lower bound on precision)
- **Punjab paddy belt ≤ 1%** of detections within 500 m of a source, outside a 3 km
  zone around the HMEL refinery
- **FIRMS `type=2` recall ≥ 98%** of static-flagged detections within 500 m of a source
  (evaluation only)
- **Reliance Jamnagar, Nayara Vadinar and HMEL Bathinda** each have a source within 3 km
  of their published coordinates
- no source footprint wider than the cap
- persistence in [0, 1], computed nights over nights (and days over days), and
  positive at night or by day for every source. The first wording was (0, 1] at
  night; the full archive has one day-shift plant that is never seen at night (see
  Result)
- every source has a non-empty `baselines` JSONB

It also reports how many co-located sources merge.

**Blocked by:** Stage 2, **and the user's review of Stage 1.5b**. **Effort:** two days.

**Result (2026-09-29):** accepted; `check_registry.py` passes. Full report:
`reports/stage3_registry.md`.
- **557 sources** from 2,745 cells. The build takes 99 s, and every source has a
  baseline.
- The widest source is 8.0 km; nothing was capped.
- GIHS recall is **83.4%**, and **86.2%** of sources sit on a GIHS site.
- **0.014%** of the paddy belt lands on a source; FIRMS `type=2` coverage is
  **99.3%**.
- Reliance is found at 0.72 km, Nayara at 0.25 km and HMEL at 2.1 km.
- **The gate moved to ≥ 3 years,** by the rule above: on 13 years the two-year gate
  had only 79.5% of its sources on GIHS (`reports/stage3/sweep.md`; CLAUDE.md
  changed decision 19).
- 185 sources each merge two or more GIHS objects (469 objects in all): GIHS draws a
  plant as several objects.
- One source is day-only: Bajaj Auto, Waluj, on a confirmed GIHS site, which is why
  the persistence check was reworded.
- Tests: 134 passed, 1 skipped by design (the hypertable test; TimescaleDB is not in
  the portable build).

---

## Stage 4 — Weak labels and Model 1

**Classes (approved 2026-09-29, from the Stage 1.5b label census):** the census counted
the registry sources each group can label.
- **Mining and coal fires:** 163.
- **Heavy industry:** 215 — thermal power, steel, cement, smelters and other works,
  merged because captive power plants make them inseparable by label.
- **Oil and gas:** 27 — thin, so its recall is reported separately. If that recall is
  unusable, the map names the nearest OSM oil and gas facility instead; that is a
  display lookup, not a class.
- **Kiln:** dropped.
- **Recurrent biomass:** a fourth class only if WorldCover finds at least ~30 registry
  sources on cropland or forest with no industrial label.

**Build:** label joins per the class table in `CLAUDE.md`, with `LABEL_INPUTS` declared
per label. XGBoost with GroupKFold by spatial block (~2°, so no state polygons are
needed); confusion matrix, per-class precision/recall, feature importances; model
persistence; the leakage guard.

**Produces:** `firewatch/models/labels.py`, `firewatch/models/source_clf.py`,
`scripts/train.py`, `reports/model1_metrics.json`

**Accept when:**
```bash
make labels && make train
cat reports/model1_metrics.json
```
The report holds the confusion matrix, block-grouped accuracy, feature importances and
the label count per class and source. **The script fails loudly if
`FEATS ∩ LABEL_INPUTS` is non-empty.** If a class has too few honest labels (kiln is the
likely one), drop or merge it and say so in the report.

**Blocked by:** Stage 3. **Effort:** one and a half days.
**This stage decides the classifier's quality. Do not compress it.**

---

## Stage 5 — Router and inference

**Build:** the spatial-lookup router (against source footprints); Road A rules plus
physics, emitting a reason string; Road C with two tiers — a provisional alert on one
extreme pass, a confirmed alert on two consecutive breaching passes — with thresholds
calibrated on spikes injected into real histories; promotion to provisional sources that
keep alerting.

**Produces:** `firewatch/inference/router.py`, `road_a.py`, `anomaly.py`, `promotion.py`,
`scripts/run_inference.py`

**Accept when:**
```bash
make inference
psql -c "SELECT road, pred_class, count(*) FROM detections
         WHERE road IS NOT NULL GROUP BY 1, 2 ORDER BY 1;"
python scripts/check_injected.py   # spikes injected into real source histories
python scripts/check_baghjan.py    # regression test
```
Targets:
- injected-spike recall > 90% (confirmed tier)
- confirmed false positives < 0.1% of Road B passes
- provisional false positives < 0.01% of passes
- every Road A detection carries a non-empty reason
- **Baghjan** (Tinsukia, Assam; burning June–November 2020, coordinates from the verified
  event set) is never routed to Road B while it burns

**Blocked by:** Stage 4. **Effort:** two days.

---

## Stage 6 — Event assembly

**Build:** spatial clustering within a pass, temporal linking across passes, event
lifecycle (active → dormant → closed) with the 72-hour re-ignition window. A provisional
source's long-running incident stays one open event.

**Produces:** `firewatch/inference/events.py`

**Accept when:**
```bash
make events
psql -c "SELECT count(*) FROM events;"
python scripts/check_dedup.py   # one real fire in the verified set => exactly one event row
```

**Blocked by:** Stage 5. **Effort:** one day.

---

## Stage 7 — Risk scoring

**Build:** the critical asset register seed (~200 entries), H/E/V computation, the
multiplicative score, and the decomposition output.

**Produces:** `firewatch/risk/`, `sql/003_critical_assets.sql`

**Accept when:**
```bash
make risk
curl localhost:8000/api/events/1 | jq .risk_breakdown
```
Every scored event returns hazard, exposure and vulnerability sub-scores with the raw
values behind each. A large fire far from population and assets scores low — verify this
case explicitly; it is the argument for multiplication.

**Blocked by:** Stage 6. **Effort:** one day. **First to cut.**

---

## Stage 8 — API

**Build:** FastAPI endpoints for detections, sources, events, timeseries and
observability; `ST_AsMVT` vector tiles for anything above ~10k points. Responses carry
the provisional flag, the alert tier and the reason text.

**Produces:** `firewatch/api/`

**Accept when:**
```bash
make api
curl "localhost:8000/api/sources?bbox=68,6,98,37&class=flare" | jq '.features | length'
curl "localhost:8000/api/tiles/detections/6/40/28.mvt" -o /dev/null -w "%{http_code}"
```
`/docs` renders the OpenAPI page.

**Blocked by:** Stage 3 minimum (sources endpoint), Stage 6 for events.
**Effort:** one day.

---

## Stage 9 — Frontend

**Build:** MapLibre map; one toggleable layer per class; a click-through detail panel
showing class, confidence, temperature, FRP against baseline, nearest named facility, alert
tier and **the reason for the classification** (Road A's rule path, or the baseline breach
for Road C); FRP time-series chart; time slider; observability overlay. **Offline
basemap:** a PMTiles extract of India served locally, because venue internet at the
finale is unreliable.

**Produces:** `web/`

**Accept when:** every class layer toggles independently; clicking any point opens the
detail panel with a populated reason field; the time slider redraws; the app runs from
`docker compose up` with no manual steps **and with the network disconnected**.

**Blocked by:** Stage 8. **Effort:** two days.
**This is deliverable (ii). It must exist even if it is plain.**

---

## Stage 10 — Validation and demo

**Build:** the held-out evaluation, the full metrics report, a scripted demo path and the
limitations table.

**Headline:** per-fire recall on the verified industrial fire set, with mean
time-to-detect. Also: Model 1 confusion matrix and per-class precision/recall (reference:
the published 77%); Road B false-positive rate; Road A discard rate; registry vs GIHS;
registry vs FIRMS `type=2`. The limitations table lists verified accidents that left no
FIRMS signature (fires between passes, heat confined inside vessels) — they count against
recall, and they are reported, not hidden.

**Produces:** `reports/validation.md`, `scripts/demo.sh`, `scripts/demo.ps1`

**Accept when:** `reports/validation.md` contains all of the above, and the demo script
runs the three-minute demo path end to end without intervention.

**Blocked by:** Stage 9, and the verified event set. **Effort:** one day.

---

## Parallel track — people, not code (start now)

- **Verified industrial fire set:** 20–40 news-verified events with date and location;
  Baghjan 2020 must be in it. Check each one in FIRMS. Some accidents leave no thermal
  signature; record those too, because they belong in the limitations table.
- **VNF academic licence:** signed agreement, approval time unknown. Nothing blocks on it.
- **Docker Desktop:** needs WSL2 or Hyper-V, virtualisation enabled in BIOS, admin rights.
- **FIRMS `MAP_KEY`:** only needed for 2025 onward and live NRT. Free and instant.
- **Critical asset register:** ~200 entries from PESO, CEA and MoPNG listings.

---

## Dependency graph

```
0 ─→ 1 ─→ 2 ─→ 3 ─→ 4 ─→ 5 ─→ 6 ─→ 7
│              ↑    │         │     │
└─→ 1.5 ─→ 1.5b    └─→ 8 ←───┴─────┘
         (review)          │
                           └─→ 9 ─→ 10
```

Stage 1.5 feeds Stage 3 (gate thresholds and the go-ahead). Stage 8 can start as soon as
Stage 3 finishes — the sources endpoint doesn't need events.

---

## Totals

| Stages | Content | Effort |
|---|---|---|
| 0 fixes, 1, 1.5 | Environment, test fixture, real-data spike | ~2 days |
| 2–4 | Ingestion → registry → Model 1 | ~5.5 days |
| 5–6 | Inference and events | ~3 days |
| 7 | Risk scoring | ~1 day |
| 8–9 | API and frontend | ~3 days |
| 10 | Validation | ~1 day |
| | **Total** | **~15.5 working days** |

The build window is October–November 2026; the 36-hour finale is in December. With a
team of six, run four tracks: data and registry, labels and model, API and frontend, and
the parallel track above.

---

## What I need from you per stage

- **Stage 0:** Docker Desktop installed, so the DB tests can run.
- **Stage 2:** the FIRMS `MAP_KEY` for 2025 onward (history needs none); EOG credentials
  once the VNF licence is approved (optional).
- **Stage 5:** Baghjan's coordinates and dates from the verified event set.
- **Stage 7:** the critical asset register. I'll seed ~40 entries I can source confidently
  (major refineries, LNG terminals, large thermal stations); expanding to 200 from the
  PESO and CEA listings is a manual task.
- **Stage 10:** the complete verified event set.

---

Tell me a stage number and I'll build it.
