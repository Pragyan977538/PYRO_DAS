# FireWatch — build roadmap

Implementation plan for **PS 26162**. Each stage is independently runnable and has a
pass/fail acceptance test. Design rules live in [DESIGN.md](DESIGN.md); where this file
and DESIGN.md disagree, DESIGN.md wins.

Revised 2026-09-29 after a design review — see DESIGN.md → *Decision log*.

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
| 4 | **Done 2026-09-29.** Model 1 block-CV balanced accuracy 61.2% (location-only reference 40.8%); oil and gas usable; biomass class refused (`reports/stage4_model1.md`) |
| 5 | **Done 2026-09-29, one target missed.** 2024 replayed: 1.20M detections routed and explained. Road C confirmed recall 88.2% (target > 90%) at 0.040% false positives; Baghjan never Road B (`reports/stage5_inference.md`) |
| 6 | **Done 2026-09-29.** 2024 assembled into 461,007 events (115 new-source incidents, 28 anomalies); Baghjan is exactly one event (`reports/stage6_events.md`) |
| 7 | **Done 2026-09-29.** 461,008 events scored with decompositions; remote big fires score ≤ 17.8, big fires at critical assets median 79.2 (`reports/stage7_risk.md`) |
| 8 | **Done 2026-09-29.** FastAPI: sources, detections, events (with risk breakdown), timeseries, observability, vector tiles; acceptance calls pass on the live server |
| 9 | **Done 2026-09-29, one gap.** The map works offline and every acceptance behaviour was checked in a browser; `docker compose up` is written but unverified (no Docker on the build machine) |
| 10 | **Done 2026-09-29; the headline misses.** Per-fire recall 1 of 18 verified accidents (chance 0.12); FIRMS saw only 6 of them. Every metric, the reasons for each miss and the limitations: `reports/validation.md`. Demo path scripted and checked |

---

## Repository layout

```
firewatch/
├── docker-compose.yml          # PostgreSQL + PostGIS (+ TimescaleDB) and the API
├── Makefile, make.ps1          # the same targets; make.ps1 for Windows
├── requirements.txt            # pinned; Python 3.13
├── .env.example
├── sql/                        # applied in order by scripts/migrate.py
│   ├── 001_schema.sql          # core tables; TimescaleDB optional
│   ├── 002_indexes.sql
│   ├── 003_ingest.sql          # OSM, GIHS, observability
│   ├── 004_registry.sql        # sources, cells, fingerprints, baselines
│   ├── 005_labels.sql          # power plants, weak labels
│   ├── 006_inference.sql       # roads, reasons, alerts, passes, runs
│   ├── 007_events.sql
│   ├── 008_risk.sql            # critical assets, wind
│   └── 009_api.sql
├── firewatch/
│   ├── config.py               # environment-driven settings; MOCK_MODE lives here
│   ├── db.py                   # connection pool, upsert helpers
│   ├── grid.py                 # EPSG:7755 metres, 375 m cells: the one implementation
│   ├── ingest/
│   │   ├── normalize.py        # column normalisation, SP/NRT dedupe
│   │   ├── fixture.py          # synthetic test fixture + spike injector
│   │   ├── firms_archive.py    # public yearly CSVs
│   │   ├── firms_api.py        # 2025+ and live NRT (needs FIRMS_MAP_KEY)
│   │   ├── load.py             # bulk loading into PostgreSQL
│   │   ├── vnf.py              # optional temperature enrichment
│   │   ├── osm.py              # Geofabrik + pyosmium
│   │   ├── gppd.py             # WRI Global Power Plant Database
│   │   ├── landcover.py        # WorldCover sampling and the 300 m mosaic
│   │   ├── gihs.py             # evaluation reference
│   │   ├── observability.py    # daily cloud amount via NASA POWER
│   │   ├── wind.py             # daily wind components via NASA POWER
│   │   └── basemap.py          # Natural Earth layers for the offline map
│   ├── registry/
│   │   ├── cells.py            # 375 m grid + recurrence gate
│   │   ├── cluster.py
│   │   ├── fingerprint.py
│   │   ├── baseline.py         # per-pass baselines by instrument, day/night, season
│   │   ├── build.py            # the weekly rebuild; stable source ids
│   │   └── evaluate.py         # GIHS, FIRMS type=2, demo sites
│   ├── models/
│   │   ├── labels.py           # weak labels + LABEL_INPUTS
│   │   └── source_clf.py       # Model 1, the only learned model
│   ├── inference/
│   │   ├── router.py
│   │   ├── road_a.py           # rules + physics; emits the reason text
│   │   ├── anomaly.py          # Road C, two tiers
│   │   ├── promotion.py        # provisional sources that never silence alerts
│   │   ├── engine.py           # replay and live runs, day by day
│   │   └── events.py
│   ├── risk/
│   │   ├── assets.py           # the generated critical-asset register
│   │   ├── population.py       # WorldPop disks and downwind sectors
│   │   └── score.py
│   └── api/
│       ├── main.py
│       └── tiles.py            # vector tiles from ST_AsMVT
├── web/                        # MapLibre frontend; offline
├── scripts/                    # one CLI per pipeline step, plus check scripts
├── reference/                  # verified industrial accidents; synthetic benchmark
├── reports/                    # results of each stage
├── docs/                       # DESIGN.md, this roadmap, images
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
now EPSG:7755 in `firewatch/grid.py`, and the spikes were rerun (see DESIGN.md,
decision 17).

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
**Stage 3 does not start until this has been reviewed.**

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

**Stage 3 does not start until this has been reviewed.**

---

## Stage 2 — Ingestion

**Build:**
- Archive loader for the public yearly CSVs (all years, all sensors) through
  `normalize.py` into `detections`, SP taking precedence over NRT.
- FIRMS API client for 2025 onward and the 3-hourly live NRT pull: rate-limited,
  resumable, needs `FIRMS_MAP_KEY`. Sensor-agnostic, because S-NPP ends 1 Nov 2026.
- VNF loader with the spatio-temporal join — **optional**; skips cleanly without an EOG
  licence.
- OSM via Geofabrik + pyosmium, using the tags in the DESIGN.md label table →
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

**Blocked by:** Stage 2, **and a review of Stage 1.5b**. **Effort:** two days.

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
  had only 79.5% of its sources on GIHS (`reports/stage3/sweep.md`; DESIGN.md
  decision 19).
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

**Build:** label joins per the class table in DESIGN.md, with `LABEL_INPUTS` declared
per label. XGBoost with GroupKFold by spatial block (~2°, so no state polygons are
needed); confusion matrix, per-class precision/recall, feature importances; model
persistence; the leakage guard.

**Produces:** `firewatch/models/labels.py`, `firewatch/models/source_clf.py`,
`firewatch/ingest/gppd.py` (WRI power plants), `sql/005_labels.sql` (`power_plants`,
`source_labels`), `scripts/build_labels.py`, `scripts/train.py`,
`reports/model1_metrics.json`, `tests/test_models.py`

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

**Fixed 2026-09-29, after labelling and before any training ran:**
- **Label order** is the census's, most specific first:
  oil_gas > steel_cement > thermal_power (OSM or WRI GPPD combustion plants) > mining >
  kiln (no label) > industrial_other, within 1 km of any source cell.
- **The biomass class needs a second condition:** fewer than half its candidates may
  sit on a GIHS-confirmed industrial site. GIHS decides only whether the class exists
  and never labels a source. It failed this condition: see Result.
- **Features exclude absolute brightness temperatures** (`bt4_*`, `bt5_*`). They carry
  the background's climate, which is a location proxy. The I4 − I5 contrast carries the
  fire, so `bt45_*` stays.
- **XGBoost hyperparameters are fixed, not tuned:** depth 3, 300 trees, learning rate
  0.05, subsample 0.8, balanced class weights. No setting is chosen on cross-validation
  scores.
- **Evaluation** is 5-fold GroupKFold on 2° blocks.
- **Oil and gas is "usable"** only if its block-CV recall and precision are both
  ≥ 50%. Otherwise the map shows the nearest OSM oil and gas facility as a lookup.
- **Two references are reported, and neither is ever a feature:**
  - the majority class
  - a location-only model on lat/lon, which shows how much of the score geography
    alone would give

**Result (2026-09-29):** accepted. `make labels && make train` runs, and the leakage
guard passes and is tested to fail on any label input. The metrics are in
`reports/model1_metrics.json`; the write-up is `reports/stage4_model1.md`.
- **Labels** for the 557 sources:
  - heavy industry 267 (industrial_other 183, thermal_power 77, steel_cement 7)
  - mining 165
  - oil and gas 34
  - unlabelled 91 (2 of them kiln-first)
- **Biomass class: not added.** There were 42 candidates, but 81% sat on
  GIHS-confirmed industry (DESIGN.md decision 20).
- **Block CV:** accuracy **65.2%**, balanced accuracy **61.2%**, macro-F1 0.62.
  - The location-only reference reaches 40.8% balanced.
  - Shuffled labels average 33.3% balanced: none of 100 shuffles comes near
    (p ≈ 0.01).
- **Oil and gas** reaches recall 58.8% and precision 64.5%, so it is usable and stays
  a class.
- **Contested labels:** 119 sources have evidence for two classes. The model is right
  on 52% of them, against 70% of uncontested sources.
- **The four VNF features** are dropped because they are all-missing without the
  licence. Adding VNF is the obvious next gain.

---

## Stage 5 — Router and inference

**Build:** the spatial-lookup router (against source footprints); Road A rules plus
physics, emitting a reason string; Road C with two tiers — a provisional alert on one
extreme pass, a confirmed alert on two consecutive breaching passes — with thresholds
calibrated on spikes injected into real histories; promotion to provisional sources that
keep alerting.

**Produces:** `firewatch/inference/router.py`, `road_a.py`, `anomaly.py`, `promotion.py`,
`engine.py`; `firewatch/ingest/landcover.py` India mosaic; `sql/006_inference.sql`;
`scripts/run_inference.py`, `check_injected.py`, `check_baghjan.py`,
`report_inference.py`; `reference/verified_events.csv`; `tests/test_inference.py`

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

**Fixed 2026-09-29, before any inference ran:**

**Passes.** A pass is one sensor's detections at one source within 20 minutes. Its
statistic is the **hottest pixel**, and baselines (Stage 3) are rebuilt on the same unit,
pass maxima keyed `instrument|daynight|season` with n ≥ 30 passes. A pixel-level baseline
would flag every big multi-pixel site on most passes, because the most extreme of k
pixels is not one pixel. A pass total would drown a one-pixel fire at a many-pixel site.

**Road C:**
- **Breach and confirmed tier** are fixed by the design (DESIGN.md §3.4): breach is `z > 3.5` and
  `max > 1.5 × p99`. Confirmed means breaching on two consecutive passes at the source:
  the previous detection pass, from any sensor, within 24 h.
- **Extreme tier** (provisional) starts at `z > 7` and `max > 3 × p99`.
- **Calibration on 2023**, with baselines from 2022 and earlier: keep (7, 3) if provisional
  false positives are < 0.01% of passes. Otherwise take the grid setting
  (z ∈ {7, 10, 15, 20} × p99 ∈ {3, 4, 5, 6}) that meets it with the best single-pass
  recall on injected spikes, lower thresholds winning ties.
- **Every target is then reported on 2024**, with baselines from 2023 and earlier.

**Injected spikes:**
- Up to 3 events per registry source with ≥ 40 passes in the test year.
- Each event is 6–12× the source's baseline-period median, over 2 consecutive real passes
  (`fixture.inject_spikes`).
- Recall (confirmed) means the event's second pass raises a confirmed alert.
- False-positive rates are measured on the same year without injection. Real fires in it
  count against us.

**Road A** is rules applied in order, first match wins, and each rule writes its reason:
1. A mapped facility (OSM, or a WRI combustion plant) within 375 m: its class.
2. Bare ground within 2 km of a mapped mine: mining.
3. Water or open sea: offshore (platform flare or vessel).
4. Land cover:
   - tree cover or mangrove: forest
   - cropland: agricultural
   - shrub, grass or wetland: other vegetation
   - built-up or bare: unclassified, with the reason saying why

Land cover comes from a ~300 m India mosaic of WorldCover's overviews.

**Promotion:**
- Road A detections from the last 60 days, in 375 m cells.
- Cells with ≥ 3 distinct days are clustered at 500 m.
- A cluster ≤ 2 km wide with ≥ 20 distinct days becomes a **provisional source**.
- Its detections are Road C (`new_source`) from then on, never Road B.
- It is retired after 90 quiet days, and only an analyst clears `provisional`.

**Baghjan** is located from the satellite record, not the article. Wikipedia's point
(27.604 N, 95.405 E) is a persistent flare, registry source 549. The fire's 557 VIIRS
detections between 9 June and 15 November 2020 sit 2.7 km away, at 27.596 N, 95.379 E
(σ ≈ 300 m). Both points are in `reference/verified_events.csv`.

**Replays are honest in time:** baselines are as of the window's start, and promotion
happens in simulated time. `make inference` replays the latest archive year. The
registry itself saw that year (the gate needs ≥ 3 years, so few sources depend on it),
and the report says so.

**Result (2026-09-29):** `make inference` replays 2024, writing 1,204,402 detections in
about 10 minutes.
- **Roads:** A 86.3%, B 12.8%, C 0.86%. **Every Road A detection has a reason.**
- **Road C against injected spikes, 2024** (baselines ≤ 2023, nothing tuned on it):
  - confirmed recall **88.2%**: the > 90% target is **missed**
  - confirmed false positives **0.040%**
  - provisional false positives **0.000%**
- **Why recall falls short.** The breach test sees 92% of spikes on the first pass.
  About half the misses fall to the non-negotiable `> 1.5 × p99` condition at
  heavy-tailed sources, so that is reported, not tuned.
- **Baghjan:** 645 fire detections, **0 on Road B**. Promoted 21 days after it caught
  fire, then Road C.
- **Four rules changed after the first real results.** Each is principled rather than
  fitted, and each is in DESIGN.md decisions 22–25:
  - **"Consecutive" has no clock.** The 24 h window above cost 35 points of recall
    and bought nothing.
  - **Own-instrument baselines only.** This halved the confirmed false positives.
  - **Promotion counts observable days, not calendar days.** Baghjan went from 66
    days to 21.
  - **The extreme tier is at 6× p99**, by the calibration rule. It now catches ~7% of
    one-pass blasts; 3× would catch 62% at ~40 false alerts a year. That is a user
    decision (`ANOMALY_EXTREME`).
- **Two Road A rules were tightened after the first replay's map:**
  - generic industry needs the fire on it, or on built-up land beside it
  - water pixels are not offshore
- **Tests:** 29 in `tests/test_inference.py`.

---

## Stage 6 — Event assembly

**Build:** spatial clustering within a pass, temporal linking across passes, event
lifecycle (active → dormant → closed) with the 72-hour re-ignition window. A provisional
source's long-running incident stays one open event.

**Produces:** `firewatch/inference/events.py`, `sql/007_events.sql`,
`scripts/build_events.py`, `scripts/check_dedup.py`, `tests/test_events.py`

**Accept when:**
```bash
make events
psql -c "SELECT count(*) FROM events;"
python scripts/check_dedup.py   # one real fire in the verified set => exactly one event row
```

**Blocked by:** Stage 5. **Effort:** one day.

**Fixed 2026-09-29, before any events were built:**
- **Incidents only.** Road A fires and Road C alerts become events. Road B is a plant
  operating normally, so it gets none.
- **Anomaly at a registry source:** one event per source. An alert within 72 h of the
  event's last alert extends it; a later one opens a new event.
- **Provisional source:** one event for its whole life, whatever the gaps, until it is
  retired (90 quiet days) or an analyst confirms it. The event includes the Road A
  detections that caused the promotion: those within 500 m of its cells, since its
  first fire. A blowout is one incident from its first detection, not from the day it
  was promoted.
- **Road A fires:**
  - A UTC day's detections are clustered at 750 m (two VIIRS pixels).
  - A cluster joins an open event if it lies within 750 m of any of that event's
    detections from the last 72 h, and the joined event stays ≤ 10 km wide. Otherwise
    it starts a new event.
  - Day clusters wider than 10 km are split on a 10 km grid. Without the cap, 750 m
    links over consecutive days would chain the paddy belt into one "event", the
    registry's landscape problem again.
- **Lifecycle, as of the run's end:**
  - active: ≤ 24 h since the last detection
  - dormant: 24–72 h. A new detection re-ignites the same event.
  - closed: > 72 h
  - Provisional-source events stay open until the source is retired.
- **`check_dedup.py`** replays each verified event's window. It passes only if the fire's
  detections (within the event's radius, between its published dates) all land in
  exactly one event.

**Result (2026-09-29):** accepted. Full write-up: `reports/stage6_events.md`.
- `make events` assembles the 2024 replay into **461,007 events** in about 70 s:
  460,864 fires, 115 new-source incidents and 28 anomalies.
- All 1,049,887 Road A/C detections are linked to an event, and no Road B detection is.
- **`check_dedup.py` passes:** Baghjan is exactly one `new_source` event, from its first
  detection on 9 June 2020, holding all 645 of the fire's detections.
- **The first attempt failed with three events,** and that led to a fix. Linking only
  looked 72 h back, which contradicted "one event for its whole life". Fires near a live
  provisional incident now link to it at any time.

---

## Stage 7 — Risk scoring

**Build:** the critical asset register seed (~200 entries), H/E/V computation, the
multiplicative score, and the decomposition output.

**Produces:** `firewatch/risk/` (`assets`, `population`, `score`), `firewatch/ingest/wind.py`,
`sql/008_risk.sql`, `scripts/score_risk.py`, `scripts/check_risk.py`, `tests/test_risk.py`

**Accept when:**
```bash
make risk
curl localhost:8000/api/events/1 | jq .risk_breakdown
```
Every scored event returns hazard, exposure and vulnerability sub-scores with the raw
values behind each. A large fire far from population and assets scores low — verify this
case explicitly; it is the argument for multiplication.

**Blocked by:** Stage 6. **Effort:** one day. **First to cut.**

**Result (2026-09-29):** accepted, except that the `curl` needs the Stage 8 API. The
same decomposition is checked in the database by `scripts/check_risk.py`. Full write-up:
`reports/stage7_risk.md`.
- **All 461,008 events** of 2024 are scored in about 3 minutes, each with hazard,
  exposure and vulnerability sub-scores and their raw values.
- **Asset register:** generated from OSM and WRI power plants rather than hand-compiled
  for the prototype. It has 48,107 entries, including 9 nuclear plants (DESIGN.md
  decisions 26–28).
- **A large fire far from people and assets scores low:** 482 such 2024 fires score at
  most 17.8. The largest, a 2,559 MW forest fire, scores 16.5; additive would give 42.4.
  Big fires at critical assets have a median of 79.2.
- **A test caught the downwind term pointing upwind** (FFT convolution flips the
  kernel). It was fixed and everything rescored.

---

## Stage 8 — API

**Build:** FastAPI endpoints for detections, sources, events, timeseries and
observability; `ST_AsMVT` vector tiles for anything above ~10k points. Responses carry
the provisional flag, the alert tier and the reason text.

**Produces:** `firewatch/api/` (`main`, `tiles`), `sql/009_api.sql`, `tests/test_api.py`

**Accept when:**
```bash
make api
curl "localhost:8000/api/sources?bbox=68,6,98,37&class=flare" | jq '.features | length'
curl "localhost:8000/api/tiles/detections/6/40/28.mvt" -o /dev/null -w "%{http_code}"
```
`/docs` renders the OpenAPI page.

**Blocked by:** Stage 3 minimum (sources endpoint), Stage 6 for events.
**Effort:** one day.

**Result (2026-09-29):** accepted on the live server (`make api`).
- **Acceptance calls:** `class=flare` returns 34 sources (legacy class names map to the
  approved classes). The `6/40/28.mvt` tile returns 200. `/docs` renders. Stage 7's
  `curl /api/events/{id} | jq .risk_breakdown` returns the full decomposition.
- **Endpoints:** sources (and detail, and time series), detections (and the detail
  panel), events (and detail with risk breakdown and timeline), observability, and
  vector tiles for detections and events. Every feature carries its alert tier, the
  provisional flag and the reason.
- **Tiles.** Detections are binned below zoom 8 and read time-first. A zoom-5 tile
  fell from 5.8 s to 0.38 s; most calls take tens of milliseconds.
- **The map** (`web/`) is served from `/` by the same process.
- **Tests:** 13 in `tests/test_api.py`, on a fixture database.

---

## Stage 9 — Frontend

**Build:** MapLibre map; one toggleable layer per class; a click-through detail panel
showing class, confidence, temperature, FRP against baseline, nearest named facility, alert
tier and **the reason for the classification** (Road A's rule path, or the baseline breach
for Road C); FRP time-series chart; time slider; observability overlay. **Offline
basemap:** a PMTiles extract of India served locally, because venue internet at the
finale is unreliable.

**Produces:** `web/` (vendored MapLibre 4.7.1), `firewatch/ingest/basemap.py`,
`scripts/build_basemap.py`, `Dockerfile`, the `api` service in `docker-compose.yml`,
`tests/test_web.py`

**Accept when:** every class layer toggles independently; clicking any point opens the
detail panel with a populated reason field; the time slider redraws; the app runs from
`docker compose up` with no manual steps **and with the network disconnected**.

**Blocked by:** Stage 8. **Effort:** two days.
**This is deliverable (ii). It must exist even if it is plain.**

**Result (2026-09-29):** the map runs at http://localhost:8000 (`make basemap`, then
`make api`), checked in a browser.
- **Every class layer toggles independently:** five fire categories, alerts, three
  source classes, provisional sites, incidents and cloud cover.
- **Clicking a detection opens the detail panel** with a populated reason. It shows
  class and confidence, temperature (or why there is none), FRP against the site's
  own baseline, the nearest named facility, the alert tier and the incident with its
  risk. Below zoom 7 detections are binned, and clicking a bin zooms in.
- **The time slider redraws** tiles, incidents, counts and the risk watch list. Play
  animates it.
- **Source panels** chart the FRP history against the site's median and p99. **Event
  panels** decompose the risk.
- **Offline.** The basemap is Natural Earth (India point of view) in 1.7 MB of local
  GeoJSON, and MapLibre is vendored. Every request the page makes is to localhost,
  and `tests/test_web.py` fails if an external URL appears.
- **Docker is not verified.** `docker compose up` (the new `api` service and its
  `Dockerfile`) is written but **untested: Docker is not installed on the build
  machine**. Run it once on the demo laptop before the finale.

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

**Rules fixed before any event was scored** (`scripts/validate_events.py`):
- An event's location comes from a publication or a map (Wikipedia coordinates, an OSM
  facility outline, a Nominatim village or estate point) — **never from the satellite
  record**, or a hit is guaranteed.
- Match radius: an OSM outline's equivalent radius + 500 m; 2 km around a published
  plant point; 3 km around a village, estate or oilfield point.
- Window: the published burning period, or 24 h from the published start; the start date
  and the next (IST) when no time of day is published. The replay is dry and starts 60
  days earlier.
- **Flagged** = any Road C alert, or a Road A detection classified industrial, within the
  radius and window. Otherwise: routine (Road B), misclassified (Road A, other class), or
  no signature. A chance rate from the 60 lead days is reported next to each hit.

**Result (2026-09-29):** accepted; the headline number is reported, not met. Full
report: `reports/validation.md`.
- **The verified set:** 20 accidents, 2013–2024, each with a source URL. 18 are located;
  Dahej 2020 and Atchutapuram 2024 still need a person to pin the plant.
- **Per-fire recall: 1 of 18 (5.6%)**, against 0.12 expected by chance. The one hit is
  Baghjan: promoted 21 days into the fire, then alerting on every pass. Its alerts carry
  Road A's class, "forest", because no well is mapped within 375 m of the pad.
- **FIRMS saw 6 of 18.** 12 accidents left no detection: fires of a few hours between
  passes, monsoon cloud, heat inside a building (Aether, Surat: three passes during a
  7-hour fire, nothing within 15 km).
- **Of the six seen:** three were Road B at running plants, below the plants' own
  baselines (Haldia, Bhilai, Tata Steel); two were unmapped factories misrouted by land
  cover (Harda as cropland, Dombivli as built-up).
- **Mean time to the first FIRMS detection:** 6.0 h (n = 5). No alerted event has a
  published time of day, so there is no mean time-to-alert.
- **Everything else holds:**
  - Model 1: 65.2% (balanced 61.2%) against the 77% reference that used temperature.
  - Road B false alerts: 0.040% confirmed, 0.000% provisional.
  - Road A discards nothing; 0.98% is left unclassified, each with a reason.
  - Registry: 83.4% of active GIHS objects found; 98.6% of FIRMS `type=2` detections
    routed to a source.
- **The demo:** `make demo` / `.\make.ps1 demo` runs six map stops in about three
  minutes. `--check` / `-Check` verifies every stop against the API. Both wrappers were
  run end to end, starting and stopping their own server.

---

## External dependencies

Work outside the code, tracked alongside the stages.

| Item | Needed for | Status |
|---|---|---|
| Verified industrial accident set (20–40 events, Baghjan 2020 included) | Stage 10 headline | 20 compiled, 18 located; Dahej 2020 and Atchutapuram 2024 still need the plant pinned |
| VIIRS Nightfire academic licence | Temperature features | Pending; the pipeline runs without it |
| Docker Desktop (WSL2 or Hyper-V, administrator rights) | `docker compose` deployment | Not yet verified; a portable PostgreSQL + PostGIS is used |
| FIRMS `MAP_KEY` | 2025 onward and the live NRT feed | Free; not yet configured |
| Official critical-asset lists (PESO, CEA, MoPNG) | Risk: vulnerability | A generated register is in use; manual rows can be layered on |

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

## Effort estimate

The estimate made at the start of the build.

| Stages | Content | Effort |
|---|---|---|
| 0 fixes, 1, 1.5 | Environment, test fixture, real-data spike | ~2 days |
| 2–4 | Ingestion → registry → Model 1 | ~5.5 days |
| 5–6 | Inference and events | ~3 days |
| 7 | Risk scoring | ~1 day |
| 8–9 | API and frontend | ~3 days |
| 10 | Validation | ~1 day |
| | **Total** | **~15.5 working days** |

The plan assumed a team of six working four tracks in parallel: data and registry,
labels and model, API and frontend, and the external dependencies above.
