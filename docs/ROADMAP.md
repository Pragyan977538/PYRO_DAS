# FireWatch — build roadmap

Implementation plan for **PS 26162**, divided into ten stages.
Each stage is independently runnable and has a pass/fail acceptance test.

---

## Ground rules

**Mock mode is the default.** Stages 1–9 are built and tested against a synthetic
generator that produces FIRMS/VNF-shaped data. Nothing is blocked waiting for API keys.
When credentials arrive, one config flag switches every stage to live data — the code
does not change.

**Every stage ends with something runnable.** No stage is "write the classes for X".
Each produces a command you can execute and a number you can check.

**Cut from the bottom.** Stages 0–6 and 8 are the graded deliverables. Stage 7 and 9 are
differentiation. If time runs out, Stage 7 goes first.

---

## Repository layout

```
firewatch/
├── docker-compose.yml          # postgis + api, one command to start
├── requirements.txt
├── .env.example
├── Makefile                    # make db, make backfill, make registry, make train...
├── sql/
│   ├── 001_schema.sql
│   ├── 002_indexes.sql
│   └── 003_critical_assets.sql
├── firewatch/
│   ├── config.py               # env-driven, MOCK_MODE flag lives here
│   ├── db.py                   # connection pool, upsert helpers
│   ├── ingest/
│   │   ├── mock.py             # synthetic FIRMS/VNF generator
│   │   ├── firms.py
│   │   ├── vnf.py
│   │   ├── osm.py
│   │   ├── fsi.py
│   │   └── observability.py
│   ├── registry/
│   │   ├── cluster.py
│   │   ├── fingerprint.py
│   │   └── baseline.py
│   ├── models/
│   │   ├── labels.py           # weak supervision joins
│   │   ├── source_clf.py       # Model 1
│   │   └── road_a_clf.py       # Model 2
│   ├── inference/
│   │   ├── router.py
│   │   ├── anomaly.py
│   │   └── events.py
│   ├── risk/
│   │   ├── assets.py
│   │   └── score.py
│   └── api/
│       ├── main.py
│       └── tiles.py
├── web/                        # MapLibre frontend
├── scripts/                    # thin CLI wrappers
└── tests/
```

---

## Stage 0 — Skeleton and database

**Build:** repo structure, `docker-compose.yml` (PostGIS + TimescaleDB), `requirements.txt`,
`.env.example`, `config.py`, `db.py`, the full SQL schema from the blueprint, `Makefile`.

**Produces:** `sql/001_schema.sql`, `sql/002_indexes.sql`, `firewatch/config.py`,
`firewatch/db.py`, `docker-compose.yml`, `Makefile`

**Accept when:**
```bash
make db && make migrate
psql -c "\dt"        # detections, sources, events, observability, critical_assets present
psql -c "SELECT PostGIS_Version();"
```

**Blocked by:** nothing. **Effort:** half a day.

---

## Stage 1 — Mock data generator

**Build:** a synthetic generator that emits FIRMS-shaped CSV and VNF-shaped CSV for a
configurable date range, with the six source classes, realistic within-class spread,
55% missing temperature, monsoon cloud gaps, and a handful of injected fire events at
known coordinates and dates.

This is not throwaway code. It is the **demo dataset** and the **test fixture** for every
later stage, and it is what lets you rehearse the presentation without live data.

**Produces:** `firewatch/ingest/mock.py`, `scripts/generate_mock.py`

**Accept when:**
```bash
python scripts/generate_mock.py --years 6 --out data/mock/
# writes firms_*.csv and vnf_*.csv with the correct column names
# ground_truth.csv lists every source and every injected event
```

**Blocked by:** Stage 0. **Effort:** one day.

---

## Stage 2 — Ingestion

**Build:** FIRMS client (backfill loop + 3-hourly live pull, rate-limited, resumable),
MODIS/VIIRS column normalisation, VNF loader with the spatial-temporal join, OSM loader
via the Geofabrik + osmium path, FSI loader, observability writer.

**Produces:** everything under `firewatch/ingest/`, `scripts/backfill.py`

**Accept when:**
```bash
make backfill              # MOCK_MODE=1 by default
psql -c "SELECT count(*), min(acq_datetime), max(acq_datetime) FROM detections;"
psql -c "SELECT count(*) FROM detections WHERE vnf_temp_k IS NOT NULL;"   # ~45%
psql -c "SELECT count(*) FROM observability;"                            # non-zero
```
Column normalisation verified: no row has both `bright_ti4` and `brightness` populated.

**Blocked by:** Stages 0, 1. **Effort:** two days.

---

## Stage 3 — Registry

**Build:** state-partitioned DBSCAN with `algorithm="ball_tree"`, NaN-safe fingerprint
computation, conditional baselines keyed by `sensor | daynight | season` with the
30-sample fallback hierarchy.

**Produces:** `firewatch/registry/`, `scripts/build_registry.py`

**Accept when:**
```bash
make registry
psql -c "SELECT count(*) FROM sources;"
psql -c "SELECT cls, count(*) FROM sources GROUP BY cls;"
python scripts/check_registry.py    # purity against mock ground truth > 0.90
```
Every source has a non-empty `baselines` JSONB. Persistence values fall in (0, 1] and use
the observed-nights denominator.

**Blocked by:** Stage 2. **Effort:** two days.
**Known issue to expect:** ~15% of co-located sources merge. Record the number.

---

## Stage 4 — Weak labels and Model 1

**Build:** the OSM/FSI/land-cover label joins, XGBoost training with GroupKFold by state,
confusion matrix + per-class precision/recall + feature importances, model persistence,
and the leakage guard (location features excluded, asserted in code).

**Produces:** `firewatch/models/labels.py`, `firewatch/models/source_clf.py`,
`scripts/train.py`, `reports/model1_metrics.json`

**Accept when:**
```bash
make labels && make train
cat reports/model1_metrics.json
```
Confusion matrix printed. Accuracy reported with a state-grouped split. **The script fails
loudly if any location feature appears in `FEATS`.** Feature importances written to the
report.

**Blocked by:** Stage 3. **Effort:** one and a half days.
**This is the stage that decides your score. Do not compress it.**

---

## Stage 5 — Router and inference

**Build:** the spatial-lookup router, Road C anomaly test with the two-consecutive-pass
rule, Road A classifier (rules first, XGBoost second), and the Road A → registry promotion
job.

**Produces:** `firewatch/inference/router.py`, `firewatch/inference/anomaly.py`,
`firewatch/models/road_a_clf.py`, `scripts/run_inference.py`

**Accept when:**
```bash
make inference
psql -c "SELECT road, pred_class, count(*) FROM detections
         WHERE road IS NOT NULL GROUP BY 1,2 ORDER BY 1;"
python scripts/check_injected.py   # recovers the mock's injected fire events
```
Target: injected-event recall > 90%, Road B false-positive rate < 0.1% **after**
two-pass confirmation.

**Blocked by:** Stage 4. **Effort:** two days.

---

## Stage 6 — Event assembly

**Build:** spatial clustering within a pass, temporal linking across passes, event
lifecycle (active → dormant → closed) with the 72-hour re-ignition window.

**Produces:** `firewatch/inference/events.py`

**Accept when:**
```bash
make events
psql -c "SELECT count(*) FROM events;"
python scripts/check_dedup.py   # one injected fire => exactly one event row
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
case explicitly, it is the argument for multiplication.

**Blocked by:** Stage 6. **Effort:** one day. **First to cut.**

---

## Stage 8 — API

**Build:** FastAPI endpoints for detections, sources, events, timeseries and
observability; `ST_AsMVT` vector tiles for anything above ~10k points.

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

**Build:** MapLibre map, one toggleable layer per class, click-through detail panel
showing class, confidence, temperature, FRP against baseline, nearest named facility and
**the reason for the classification**, FRP time-series chart, time slider, observability
overlay.

**Produces:** `web/`

**Accept when:** all six class layers toggle independently; clicking any point opens the
detail panel with a populated reason field; the time slider redraws; the app runs from
`docker-compose up` with no manual steps.

**Blocked by:** Stage 8. **Effort:** two days.
**This is deliverable (ii). It must exist even if it is plain.**

---

## Stage 10 — Validation and demo

**Build:** held-out evaluation split by state, full metrics report, the hand-verified
industrial-fire check set, a scripted demo path, and the limitations table.

**Produces:** `reports/validation.md`, `scripts/demo.sh`

**Accept when:** `reports/validation.md` contains the confusion matrix, per-class
precision/recall, mean time-to-detect, Road B false-positive rate, Road A discard rate,
and the comparison against the 77% published benchmark. `scripts/demo.sh` runs the
three-minute demo path end to end without intervention.

**Blocked by:** Stage 9. **Effort:** one day.

---

## Dependency graph

```
0 ─→ 1 ─→ 2 ─→ 3 ─→ 4 ─→ 5 ─→ 6 ─→ 7
                    │         │     │
                    └─→ 8 ←───┴─────┘
                        │
                        └─→ 9 ─→ 10
```

Stage 8 can start as soon as Stage 3 finishes — the sources endpoint doesn't need events.
That's the one place two people can work in parallel without blocking each other.

---

## Totals

| Stages | Content | Effort |
|---|---|---|
| 0–4 | Data → registry → trained classifier | ~7 days |
| 5–6 | Inference and events | ~3 days |
| 7 | Risk scoring | ~1 day |
| 8–9 | API and frontend | ~3 days |
| 10 | Validation | ~1 day |
| | **Total** | **~15 working days** |

Comfortable for a hackathon build period with a team. For one person under pressure,
Stages 0–5 plus 8–9 is the minimum viable submission, at roughly 10 days.

---

## What I need from you per stage

Nothing, for Stages 0–7 — mock mode covers it.

**Stage 2** switches to live data the moment you have: the FIRMS `MAP_KEY`, an EOG account,
and the Geofabrik India extract downloaded. Give me the key and I'll flip the flag.

**Stage 7** needs the critical asset register. I'll seed ~40 entries I can source
confidently (major refineries, LNG terminals, large thermal stations). Expanding to 200
from the PESO and CEA listings is a manual task for someone on the team.

---

Tell me a stage number and I'll build it.
