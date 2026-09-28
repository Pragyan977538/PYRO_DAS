# FireWatch

Satellite-based detection and classification of industrial fires and persistent thermal
sources over India. Built for Smart India Hackathon 2026, problem statement **26162**
(NTRO, Disaster Management, Software).

## What this system does

NASA FIRMS tells you a pixel is hot. It does not tell you *what* is hot — a refinery
flare, a stubble fire, a forest fire and a chemical plant explosion all arrive as the same
red dot. This system learns each location's thermal fingerprint from a decade of history
and uses it to answer the question FIRMS cannot: **is this normal for this place?**

**Graded deliverables (the only things that score):**

1. Classification and segregation of industrial fires from forest fires and other natural fires
2. GIS-based solution for data storage and visualisation as a map overlay

Everything else — event assembly, risk scoring — is differentiation and gets cut first
if time runs short.

## Architecture in one pass

Two clocks.

**Offline, rebuilt weekly.** Ten years of FIRMS detections are clustered into physical
sources. Each source gets a *fingerprint* (temperature, persistence, seasonality, FRP
distribution) and a *baseline* (median, MAD, p99 — keyed by sensor × day/night × season).
An XGBoost model classifies each source: flare, furnace, kiln, mining, forest, cropland.
The output is the **registry**.

**Online, every three hours.** Each new detection asks one question: *is there a known
source within 500 m?*

- **No → Road A.** No baseline exists, so classify from context and physics.
- **Yes, inside its envelope → Road B.** Infrastructure operating normally. Map layer only, no alert.
- **Yes, breaching both thresholds → Road C.** Anomaly at a running plant. Alert.

Road A detections that repeat for ~20 nights are promoted into the registry. The registry
grows.

## Non-negotiable design decisions

These were derived the hard way. **Do not "simplify" any of them without asking.**

### Model 1 must never see location features

Training labels come from OSM proximity (weak supervision). If `dist_industrial` or
`landcover` is also an input feature, the model learns `near factory → industrial`, scores
~100%, and has learned nothing. This is label leakage.

`scripts/train.py` must **assert** that no location feature appears in `FEATS` and fail
loudly if one does.

### Baselines are per source, never per source type

A large furnace and a small furnace differ by an order of magnitude in normal FRP. Pooling
them means a real fire at the large one may sit below the pooled p99 while the small one
breaches it during normal operation. **A site is compared only against itself.**

### Median and MAD, never mean and standard deviation

FRP is heavily right-skewed. One past explosion permanently inflates σ and the detector
stops firing at that site forever.

```python
z = 0.6745 * (frp - med) / max(mad, 1e-6)
is_anomaly = (z > 3.5) and (frp > p99 * 1.5)
```

Both conditions required — they fail differently. Plus a **two-consecutive-pass**
confirmation, which drops the false-positive rate by roughly an order of magnitude.

### Class B is the complement of Class C, not a separate test

Class B is simply "did not trigger the anomaly test". It is **not** "FRP below median" —
half of all normal readings sit above the median by definition.

### DBSCAN must use `algorithm="ball_tree"` and run in metres

The default `algorithm="auto"` exhausted memory at ~330k points. Cluster state by state
and merge; sources never span state boundaries at 500 m.

Project to metres before clustering — one degree of longitude is 111 km at the equator and
85 km at Kashmir's latitude, so `eps` in degrees means different distances across India.

```python
x = lon * 111320 * np.cos(np.radians(lat))
y = lat * 110540
DBSCAN(eps=500, min_samples=5, algorithm="ball_tree", n_jobs=2)
```

`eps=500` because VIIRS pixels are 375 m. `cluster == -1` is the noise label — those are
Road A candidates, free.

### All temperature statistics must be NaN-safe

VNF is night-only and Planck-fitting fails on cooler sources, so **~55% of detections have
no temperature**. Plain `np.median` returns NaN for the whole cluster. Use `np.nanmedian`,
`np.nanquantile` everywhere.

Do **not** impute. XGBoost handles missing values natively by learning a default split
direction. A daytime detection genuinely has no temperature measurement; filling in a
median invents data.

`temp_cov` — the fraction of a cluster's detections that *had* a temperature — is itself a
useful feature. Keep it.

### Persistence divides by nights observed, not calendar nights

Annual observability is ~64%, dropping to ~29% during monsoon. Calendar-night persistence
understates every source by a third. This is what the `observability` table is for, and it
is why a row is written on **every** pass whether or not anything burned.

### MODIS and VIIRS use different column names

MODIS emits `brightness` / `bright_t31`. VIIRS emits `bright_ti4` / `bright_ti5`.
Normalise to `bt4` / `bt5` **at ingestion**, or half the joins silently drop rows.

### Use Geofabrik for OSM, not Overpass

Overpass was unreachable during feasibility testing (503 on the main endpoint, timeouts on
two mirrors). Download the India extract and filter locally with `osmium`. Reproducible,
faster, and it cannot be down during a demo.

### The critical asset register is static and manual

Nuclear plants dump waste heat into cooling water at 30–40 °C — far below satellite
detection. They are thermally invisible until something burns. Same for ammunition depots
and LPG bottling plants. If criticality is derived from thermal history, the
highest-consequence assets score zero. Seed the table from PESO, CEA and MoPNG listings.

## Mock mode

`MOCK_MODE=1` (the default) runs the entire pipeline against a synthetic generator that
emits FIRMS- and VNF-shaped data with realistic class overlap, missing temperatures,
monsoon cloud gaps and injected fire events at known coordinates.

Nothing is blocked waiting on API keys. When credentials arrive, flip the flag — the code
does not change.

The mock dataset is also the demo dataset and the test fixture. Treat it as production
code, not a throwaway.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Database | PostgreSQL 16 + PostGIS 3.4 + TimescaleDB |
| ML | scikit-learn (DBSCAN), XGBoost |
| Backend | FastAPI |
| Frontend | React-free vanilla + MapLibre GL JS |
| Deploy | Docker Compose |

**No deep learning.** ~15 tabular features and a few thousand rows is exactly where
gradient boosting wins, and feature importances are needed to justify alerts to a
government agency.

## Conventions

- Config is environment-driven via `firewatch/config.py`. No hardcoded paths, no
  hardcoded keys, ever.
- All geometry stored in EPSG:4326. Reproject to metres (EPSG:7755 or the equirectangular
  approximation above) for distance work.
- Every stage exposes a `make` target and a check script. A stage is not done until its
  acceptance command in `docs/ROADMAP.md` passes.
- Type hints on public functions. Docstrings explain *why*, not *what*.
- Never commit `.env`, data files, or trained model binaries.

## Where things are

- `docs/ROADMAP.md` — the ten build stages with acceptance criteria. **Read the relevant
  stage before starting work.**
- `docs/plan.md` — verified feasibility results, measured numbers, working code snippets
- `docs/PS26162_Blueprint.md` — full technical reference including data source URLs and
  field lists
- `reference/verify_pipeline.py` — the synthetic benchmark that produced the measured
  numbers. Runnable.

## Measured numbers

From `reference/verify_pipeline.py` on a realistic synthetic benchmark:

- Source classifier: **94.5%** accuracy (upper bound — synthetic data is drawn from the
  distributions the model learns; target the published **77%** benchmark on real data)
- Anomaly test: **0.78%** false positives before two-pass confirmation, **96.6%**
  detection of 6–12× excursions
- Cluster purity **0.944** with 18% co-located sources
- Observability **64%** annual, **29%** monsoon
