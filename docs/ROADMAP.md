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
| 0 | Fixes done 2026-09-29. DB acceptance **pending Docker Desktop** |
| 1.5 | Done 2026-09-29 — `reports/stage1_5_spike.md` (one year) |
| 1.5b | Done 2026-09-29 — `reports/stage1_5b_multiyear.md` (three years, refineries, label census). **Awaiting review before Stage 3** |
| 1, 2 | Not started. May proceed once Docker is up; they don't depend on the gate |
| 3–10 | Not started |

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
│   │   └── observability.py    # ERA5 cloud cover via Open-Meteo
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

**Produces:** `firewatch/ingest/fixture.py`, `tests/test_fixture.py`

**Accept when:** `make test` normalises fixture rows from both sensors through
`normalize.py`, and injected spikes round-trip to their ground truth.

**Blocked by:** Stage 0. **Effort:** half a day.

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
  clusters, with 57% of its detections inside "sources". On three years (Stage 1.5b)
  it merges into **one 400 km cluster** holding 97% of them.
- **Problem 2.** `ball_tree` = `kd_tree` = 1,258 MB, and memory grows faster than the
  data.
- The gated method matches 99% of FIRMS-static detections.
- Proposed gate: ≥ 4 months on ≥ 10 days within a year, in ≥ 2 years.

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
- **The gate is confirmed unchanged:** ≥ 4 months, ≥ 10 days, in ≥ 2 years.
  - 461 sources, with 0.30% of the paddy belt misrouted.
  - GIHS recall 75%, with 87% of sources on a GIHS site.
  - All three refineries found, with 81%, 84% and 92% of their detections covered.
- **Raw DBSCAN** merges the belt into one 400 km cluster.
- **Label census:** mining 153, heavy industry 214, oil and gas 28, kiln 2. That
  supports the proposed three-class Model 1 (Stage 4).

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
- FSI loader (weak forest labels). GIHS loader (evaluation reference only).
- Observability: ERA5 cloud cover at overpass times via Open-Meteo → `observability`.

**Produces:** everything under `firewatch/ingest/`, `scripts/backfill.py`

**Accept when:**
```bash
make backfill
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

---

## Stage 3 — Registry

**Build:**
- 375 m cells.
- The recurrence gate: within a year, ≥ 4 distinct months on ≥ 10 distinct days, in
  at least 2 years. It was confirmed on 2021–2023 (Stage 1.5b) and is recalibrated
  here against GIHS on the full archive.
- DBSCAN on gated cells (`eps=500` m, `min_samples=5` by weight, `sample_weight`).
- A 20 km footprint cap, as a backstop only: the widest gated source in 2023 was
  6.8 km.
- NaN-safe fingerprints.
- Baselines keyed `instrument | daynight | season`, with the n ≥ 30 fallback
  (→ `instrument | daynight` → source-wide).

Porting the Stage 1.5 spike code (`cell_table`, `grid_cluster`) is the starting point.

**Produces:** `firewatch/registry/`, `scripts/build_registry.py`,
`scripts/check_registry.py`

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
- persistence in (0, 1], computed nights over nights
- every source has a non-empty `baselines` JSONB

It also reports how many co-located sources merge.

**Blocked by:** Stage 2, **and the user's review of Stage 1.5b**. **Effort:** two days.

---

## Stage 4 — Weak labels and Model 1

**Classes (proposed in Stage 1.5b, pending the user's review):** the label census counted
the registry sources each group can label.
- **Mining and coal fires:** 153.
- **Heavy industry:** 214 — thermal power, steel, cement, smelters and other works,
  merged because captive power plants make them inseparable by label.
- **Oil and gas:** 28 — thin, so its recall is reported separately. If that recall is
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
