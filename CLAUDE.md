# FireWatch

Satellite-based detection and classification of industrial fires and persistent thermal
sources over India. Built for Smart India Hackathon 2026, problem statement **26162**
(NTRO, Disaster Management, Software).

## What this system does

NASA FIRMS tells you a pixel is hot. It does not tell you *what* is hot — a refinery
flare, a stubble fire, a forest fire and a chemical plant explosion all arrive as the same
red dot. (The FIRMS archive does carry a `type=2` "static land source" flag, but that only
says a site recurs: not what it is, not in near-real-time, and not whether today's reading
is abnormal.) This system learns each location's thermal fingerprint from a decade of
history and uses it to answer the question FIRMS cannot: **is this normal for this place?**

**Graded deliverables (the only things that score):**

1. Classification and segregation of industrial fires from forest fires and other natural fires
2. GIS-based solution for data storage and visualisation as a map overlay

Everything else — event assembly, risk scoring — is differentiation and gets cut first
if time runs short.

**Headline metric: per-fire recall on the hand-verified industrial fire set** — 20–40
news-verified events with date and location, Baghjan 2020 included. It is the only number
that is not circular. Per-source classification accuracy is reported too, but second.

## Architecture in one pass

Two clocks.

**Offline, rebuilt weekly.** The FIRMS archive is snapped to ~375 m cells. Cells that burn
across multiple years — not just in one season — are clustered into physical sources.
Each source gets a *fingerprint* (persistence, seasonality, FRP distribution, temperature
where VNF has it) and a *baseline* (median, MAD, p99 — keyed by instrument × day/night ×
season). One XGBoost model classifies each source: flare, furnace, kiln, mining, forest,
cropland. The output is the **registry**.

**Online, every three hours.** Each new detection asks one question: *is there a known
source within 500 m?*

- **No → Road A.** No baseline exists. Transparent rules plus physics (land cover,
  distance to industrial footprints, season, temperature where available) assign a class.
  The rule path that fired becomes the "reason" text on the map.
- **Yes, inside its envelope → Road B.** Infrastructure operating normally. Map layer
  only, no alert.
- **Yes, breaching its own baseline → Road C.** Anomaly at a running plant. Two tiers: an
  extreme breach on a single pass raises a **provisional** alert; a breach on two
  consecutive passes raises a **confirmed** alert.

Road A sites that keep burning become **provisional** sources in the registry, and keep
alerting until they look like stable infrastructure. The registry grows, but it never
silently absorbs an accident.

In the schema, roads are numbered A = 1, B = 2, C = 3.

## Non-negotiable design decisions

These were derived the hard way — several of them twice. **Do not "simplify" any of them
without asking.** What changed, and why, is recorded in
[Changed decisions](#changed-decisions) at the end of this file.

### No label may be built from a model input

Training labels come from weak supervision. If a label is derived from anything the model
can also see, the model learns the labelling rule, scores near 100%, and has learned
nothing. Concretely:

- **Model 1 never sees location features** — no `dist_industrial`, `landcover`, lat/lon or
  state. Its labels come from location (OSM tags, WorldCover, FSI).
- **FIRMS `type` is never a label or a feature.** FIRMS derives `type=2` from recurrence,
  which is what `persistence` measures. It is stored as `firms_type` and used only as an
  evaluation comparator.
- **Cropland labels come from WorldCover land cover only, never from season.** Season is a
  feature (`month_entropy`).
- **Road A is rules plus physics, not a trained model.** Its inputs (land cover, distance
  to industry) are exactly the layers every available label is built from, so a model
  trained there could only memorise the rule. Model 1 is the only learned model.

`scripts/train.py` must declare the inputs used to build each label (`LABEL_INPUTS`) and
**assert** that `FEATS ∩ LABEL_INPUTS` is empty, failing loudly if not.

### Weak-label sources, class by class

Counts are OSM objects in India (Geofabrik taginfo, 2026-09-29). Where a class has no
honest label source, say so — never invent labels to fill it.

| Class | Label sources | Caveat |
|---|---|---|
| flare | `industrial=refinery` (35), `industrial=oil` (40), `man_made=petroleum_well` (37), `man_made=flare` (22) | OSM has only 22 flare tags in all of India. Oil-and-gas facility polygons are the fallback, and they also hold process heaters, so flare labels are noisy. The EOG flare list needs the VNF licence |
| furnace | `power=plant` with `plant:source` = coal (293) / gas (69) / oil; WRI Global Power Plant Database (thermal) | Steel and cement are nearly unlabelled in OSM (`product=steel` 5, `product=cement` 3). Name matching or the GEM steel tracker would be needed; flag it if used |
| kiln | `man_made=kiln` (2,688), `industrial=brickyard` (2,841), `industrial=brickworks` (173) | Labels exist; **whether VIIRS detects brick kilns at 375 m is unverified.** If too few labelled kiln sources reach the registry, drop or merge the class |
| mining | `landuse=quarry` (10,487), `resource=coal` (1,622), `industrial=mine` (150) | Coal-seam fires (Jharia) are fires *in* mines, not mining activity. They are labelled mining, and the report says so |
| forest | WorldCover tree cover (class 10); FSI fire alerts | FSI alerts are FIRMS points inside forest boundaries with partial state feedback. Weak, not validated |
| cropland | WorldCover cropland (class 40) only | Never season-based |
| — | FIRMS `type=2`, GIHS | Evaluation only, never labels |

### Baselines are per source, never per source type

A large furnace and a small furnace differ by an order of magnitude in normal FRP. Pooling
them means a real fire at the large one may sit below the pooled p99 while the small one
breaches it during normal operation. **A site is compared only against itself.**

Keys are **instrument (VIIRS | MODIS) × day/night × season** — never the satellite.
Suomi NPP delivery ends on 1 Nov 2026, before the finale, and NOAA-21 has little history.
The three VIIRS units run the same 375 m algorithm, so their history pools. Each bucket
needs n ≥ 30; otherwise fall back to instrument × day/night, then to the source-wide
baseline.

### Median and MAD, never mean and standard deviation

FRP is heavily right-skewed. One past explosion permanently inflates σ and the detector
stops firing at that site forever.

```python
z = 0.6745 * (frp - med) / max(mad, 1e-6)
breach  = (z > 3.5) and (frp > p99 * 1.5)                      # both: they fail differently
extreme = (z > Z_EXTREME) and (frp > p99 * P99_EXTREME)        # starting values 7 and 3
```

- **Confirmed alert:** `breach` on two consecutive passes. That drops the false-positive
  rate by roughly an order of magnitude.
- **Provisional alert:** `extreme` on a single pass. Short blasts are the deadliest events,
  and they often burn out between passes; after S-NPP stops a site is seen in about two
  windows a day. Thresholds live in config and are calibrated in Stage 5 against spikes
  injected into real histories.

### Class B is the complement of Class C, not a separate test

Class B is simply "neither alert tier fired". It is **not** "FRP below median" — half of
all normal readings sit above the median by definition.

### The registry holds persistent sources, not landscapes

Raw DBSCAN over detections turns one-off crop and forest fires into "sources". Measured
on one real year (2023, `reports/stage1_5_spike.md`):
- It registered **33,963 sources across India**; the gated method finds 442.
- **57% of the Punjab paddy belt's detections** landed inside a source (72% in a
  ten-year stand-in). Those stubble fires would get baselines and skip Road A.
- The belt did *not* merge into one blob; it fragments into thousands of 2–10 km field
  clusters.
- Dense landscapes do chain: the Jharia coalfield became one 16 km blob.

Instead:

1. **Project to metres.** One degree of longitude is 111 km at the equator and 85 km at
   Kashmir's latitude, so `eps` in degrees means different distances across India.

   ```python
   x = lon * 111320 * np.cos(np.radians(lat))
   y = lat * 110540
   ```

2. **Snap to ~375 m cells** (the VIIRS pixel) and aggregate: detections, distinct days,
   nights, months and years.
3. **Gate on recurrence:** keep cells that burn across multiple years *and* across much of
   the year. Starting values from Stage 1.5: within a year, **≥ 4 distinct months on
   ≥ 10 distinct days, in at least 2 years**.
   - On one year this kept the paddy belt at 1.8% while covering 99.8% of FIRMS-static
     detections and 71% of active GIHS sites.
   - The two-year rule keeps a single long accident (Baghjan) out by construction.
   - Thresholds live in config; Stage 3 calibrates them against GIHS on the full
     archive.
4. **Cluster the surviving cells** with
   `DBSCAN(eps=500, min_samples=5, algorithm="ball_tree", ...)` fitted with
   `sample_weight=n_detections`.
5. **Cap the footprint.** A cluster wider than a few kilometres is a landscape, not a
   source; flag it instead of registering it.

**Memory:** scikit-learn's DBSCAN materialises every point's neighbour list, so memory
grows with the square of cluster size *whatever the tree algorithm*. Measured:
- `ball_tree` and `kd_tree` gave identical labels and identical memory (1,258 MB on
  one year), and `"auto"` already picks `kd_tree`.
- Doubling the points tripled peak memory. The full 2012–2024 archive would need
  roughly 45–70 GB raw.
- The gated method took 271 MB.

`ball_tree` is kept for consistency; gridding plus `sample_weight` is what bounds
memory, as scikit-learn's own docs recommend. No state partitioning: sources can
straddle borders, and the grid makes it unnecessary.

`eps=500` because VIIRS pixels are 375 m. Cells that fail the gate, and `cluster == -1`
noise, are Road A territory.

### Promotion never silences an alert

A Road A site that keeps burning becomes a **provisional** source. Provisional sources keep
alerting until they look like stable infrastructure — classified by Model 1 as an
industrial class, with a stable fingerprint — and ideally an analyst confirms the
promotion. Accidents can burn for months: the Baghjan blowout (Assam, 2020) burned for
about five. **Baghjan is the regression test** — it must stay an alert for its whole life
and never become "normal".

### All temperature statistics must be NaN-safe

Temperature comes from VIIRS Nightfire (VNF), and many detections have none: VNF is
night-only and Planck-fitting fails on cooler sources. Plain `np.median` returns NaN for
the whole cluster. Use `np.nanmedian`, `np.nanquantile` everywhere.

Do **not** impute. XGBoost handles missing values natively by learning a default split
direction. A daytime detection genuinely has no temperature measurement; filling in a
median invents data.

`temp_cov` — the fraction of a source's detections that *had* a temperature — is itself a
useful feature. Keep it.

**VNF is optional enrichment.** It is licence-gated (a free academic licence needs a signed
agreement and approval) and night-only, and only **29% of Indian detections are at
night** (2023, measured). So VNF can annotate at most 29% of detections, not the old
mock's 45%. It remains a strong feature at sources, which are seen on hundreds of nights
a year. The pipeline must run end to end without it.

### Persistence divides by nights observable, not calendar nights

Monsoon cloud hides sites for weeks. Calendar-night persistence understates every source,
so persistence = **nights with a detection / nights the site was observable** — numerator
and denominator both nights.

FIRMS publishes detections only: no swath footprints, no cloud masks. Observability
therefore comes from **ERA5 cloud cover at the overpass time, via Open-Meteo** (free, no
key): expected clear nights = Σ(1 − cloud fraction). It is a reanalysis proxy, not a
satellite measurement — say so. Write observability for every cell-date whether or not
anything burned.

### MODIS and VIIRS use different column names

MODIS emits `brightness` / `bright_t31`. VIIRS emits `bright_ti4` / `bright_ti5`.
Normalise to `bt4` / `bt5` **at ingestion** (`firewatch/ingest/normalize.py`), or half
the joins silently drop rows. FIRMS `type` becomes `firms_type`.

### SP beats NRT; never count a pixel twice

The archive (SP, science-quality) and live (NRT) products overlap for recent months, and
reprocessing moves positions slightly, so the natural key does not catch the duplicate.
When SP covers a sensor-day, NRT rows for that sensor-day are dropped — `dedupe_sp_nrt`
within a batch, `supersede_nrt` in the database. Every row stores `product` and `version`.

### Use Geofabrik for OSM, not Overpass

Overpass was unreachable during feasibility testing (503 on the main endpoint, timeouts on
two mirrors). Download the India extract (~1.7 GB) and filter it locally with pyosmium
(`pip install osmium`), so Windows needs no native tools. Reproducible, faster, and it
cannot be down during a demo.

### The critical asset register is static and manual

Nuclear plants dump waste heat into cooling water at 30–40 °C — far below satellite
detection. They are thermally invisible until something burns. Same for ammunition depots
and LPG bottling plants. If criticality is derived from thermal history, the
highest-consequence assets score zero. Seed the table from PESO, CEA and MoPNG listings.

## Data: real first

| Source | Access | Role |
|---|---|---|
| FIRMS archive | Public yearly India CSVs, **no key**: `firms.modaps.eosdis.nasa.gov/data/country/{viirs-snpp,viirs-jpss1,modis}/{YYYY}/{sensor}_{YYYY}_India.csv`. S-NPP 2012–2024, NOAA-20 2018–2024, MODIS 2000–2024 | Primary history. Carries `type` and `version` |
| FIRMS API | Free `FIRMS_MAP_KEY` | 2025 onward, and the live 3-hourly NRT pull. Sensor-agnostic: S-NPP ends 1 Nov 2026 |
| VIIRS Nightfire | EOG academic licence | Optional temperature enrichment |
| GIHS (Ma et al. 2024) | Zenodo 10.5281/zenodo.10570342, CC BY 4.0 | Imagery-verified industrial heat sources, 2012–2021. Registry evaluation — never labels |
| OSM | Geofabrik India extract | Weak labels, Road A context |
| WorldCover | 10 m land cover COGs | Forest/cropland labels, Road A context |
| FSI fire alerts | fsiforestfire.gov.in | Weak forest labels |
| Open-Meteo | Free, no key | ERA5 cloud cover (observability), wind (risk) |

`MOCK_MODE=0` (the default) runs against real data. `MOCK_MODE=1` switches to the synthetic
**test fixture**, which is offline and deterministic, for tests and CI. Anomaly detection is
tested by injecting synthetic spikes into *real* source histories, not by simulating a
world. The fixture models biomass fires as spatially diffuse, never as point sources — that
mistake is what hid the landscape-chaining problem.

No credential is needed to start. Each client checks for its own key when it is used:
`FIRMS_MAP_KEY` only for API pulls, and VNF skips cleanly without EOG credentials.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.13 (every pinned wheel exists for 3.13 on Windows; not for 3.14) |
| Database | PostgreSQL 16 + PostGIS 3.4; TimescaleDB optional (auto-detected by the schema) |
| ML | scikit-learn (DBSCAN), XGBoost — one learned model |
| Backend | FastAPI |
| Frontend | Vanilla JS + MapLibre GL JS, with an offline basemap (PMTiles) |
| Deploy | Docker Compose (Docker Desktop on Windows); native Postgres + PostGIS as fallback |

**No deep learning.** ~15 tabular features and a few thousand rows is exactly where
gradient boosting wins, and feature importances are needed to justify alerts to a
government agency.

## Conventions

- Config is environment-driven via `firewatch/config.py`. No hardcoded paths, no
  hardcoded keys, ever.
- All geometry stored in EPSG:4326. Reproject to metres (EPSG:7755 or the equirectangular
  approximation above) for distance work.
- Work in the Python 3.13 virtualenv: `py -3.13 -m venv .venv`.
- Every stage exposes the same target in both runners — `make <target>` and
  `.\make.ps1 <target>` (Windows PowerShell 5.1 compatible) — plus a check script.
- A stage is not done until its acceptance command in `docs/ROADMAP.md` passes **with the
  database tests running**. Skipped DB tests are not a pass.
- Type hints on public functions. Docstrings explain *why*, not *what*.
- Never commit `.env`, data files, or trained model binaries.

## Where things are

- `docs/ROADMAP.md` — the build stages with acceptance criteria. **Read the relevant
  stage before starting work.**
- `reports/stage1_5_spike.md` — the real-data clustering test that settled the registry
  design.
- `docs/plan.md` — feasibility results from the synthetic benchmark. Partly superseded:
  see Changed decisions.
- `docs/PS26162_Blueprint.md` — full technical reference: data sources, field lists, the
  risk formula. Partly superseded: see Changed decisions.
- `reference/verify_pipeline.py` — the synthetic benchmark. Runnable, but it models
  biomass fires as point sources, so its numbers are upper bounds.

## Measured numbers

**Real data** — one year of VIIRS over India (2023, 1,170,878 detections), from
`reports/stage1_5_spike.md`:

- **29.0%** of detections are at night; **12.7%** carry FIRMS' `type=2` static flag.
- Raw DBSCAN: **33,963** "sources"; **57%** of the Punjab paddy belt inside one (72% in
  a ten-year stand-in); **46%** of all detections would skip Road A.
- Gated cells (≥ 6 months, ≥ 10 days): **442** sources, the widest 6.8 km.
  - Paddy belt **1.8%**, Jharia **97.8%**.
  - **99.1%** of FIRMS-static detections within 500 m of a source.
  - **83%** of sources on a GIHS site; GIHS recall **65%** of active objects.
- Memory: `ball_tree` = `kd_tree` = **1,258 MB**; gated **271 MB**. The raw archive
  extrapolates to ~45–70 GB.
- Jamnagar's largest flare group was detected on **111** days, not ~340 nights.

**Synthetic benchmark** (`reference/verify_pipeline.py`) — upper bounds only. The benchmark
draws from the distributions the model learns, *and* it models stubble and forest fires as
tight point sources:

- Source classifier **94.5%**. The real-data reference is the published **77%** (Liu et
  al. 2018, VNF industrial sub-category accuracy, global).
- Anomaly test: **0.78%** false positives before two-pass confirmation, **96.6%**
  detection of 6–12× excursions.
- Cluster purity 0.944 with 18% co-located sources — **not valid for real data**.
- Observability 64% annual, 29% monsoon — from a synthetic cloud model.

## Changed decisions

Reviewed 2026-09-28, approved 2026-09-29. Each row replaces an earlier rule; the old rule
is kept here so the history isn't lost.

| # | Old rule | New rule | Why |
|---|---|---|---|
| 1 | DBSCAN on raw detections; `ball_tree` fixes memory; cluster state by state | 375 m cells, recurrence gate, DBSCAN with `sample_weight`, footprint cap; no state partitioning | Stage 1.5, real 2023 data: raw DBSCAN made 33,963 "sources" (vs 442) and put 57% of Punjab's stubble fires inside one. The synthetic benchmark hid this by modelling biomass fires as point sources. `ball_tree` and `kd_tree` used identical memory; the 330k-vs-158k comparison changed the data, not just the algorithm |
| 2 | Single-tier, two-consecutive-pass confirmation | Provisional alert on one extreme pass; confirmed alert on two consecutive breaching passes | Short blasts, the deadliest events, often burn out between passes |
| 3 | Road A sites repeating ~20 nights are promoted into the registry | Promoted sources stay provisional and keep alerting until they look like stable infrastructure; Baghjan 2020 is the regression test | A long-burning accident would become "normal" within a month; a competing team's repo documents exactly this failure at Baghjan |
| 4 | Baselines keyed `sensor \| daynight \| season` (per satellite) | Keyed `instrument \| daynight \| season` | S-NPP delivery ends 1 Nov 2026; NOAA-21 has little history; VIIRS units share one algorithm |
| 5 | "Model 1 must never see location features" | "No label may be built from a model input" — covers location, FIRMS `type`, season | The narrow rule missed other circular labels |
| 6 | Two XGBoost models: Model 1 plus a Road A classifier | One learned model; Road A is rules plus physics whose rule path is the UI's reason text | Road A's features are the layers its labels would be built from |
| 7 | Six classes, but weak-label SQL that yields only industrial / forest / cropland | Explicit tag → class table with India counts; classes without an honest label source are flagged, not invented | Flare vs furnace vs kiln vs mining had no label source at all |
| 8 | `MOCK_MODE=1` by default; the mock is the demo dataset | Real data first; the mock shrinks to a test fixture; spikes injected into real histories | Ten years of FIRMS for India is free; a synthetic world hid problem 1 |
| 9 | Observability written per pass from swath and cloud mask | ERA5 cloud cover at overpass time via Open-Meteo, as a proxy | FIRMS publishes no swaths or cloud masks |
| 10 | VNF via a free account, required in live mode | Licence-gated and optional; the pipeline runs fully without it | EOG moved VNF behind a licence on 10 Jan 2025 |
| 11 | FSI alerts are "validated" forest fires | FIRMS points inside forest boundaries with partial state feedback — weak labels | That is what FSI's own FAQ says they are |
| 12 | Headline: source-level accuracy vs 77% | Headline: per-fire recall on the verified industrial fire set; 77% stays the reference for source sub-classification | Deliverable (i) is about fires; the verified set is the only non-circular number |
| 13 | Python 3.12; TimescaleDB required | Python 3.13; TimescaleDB optional | Windows wheels exist for 3.13 (not 3.14); native Postgres fallback while Docker is missing |
| 14 | Persistence = distinct detection dates / observed nights | Nights detected / nights observable | Day dates over a night denominator can exceed 1 |
| 15 | Online basemap | Offline basemap (PMTiles India extract) | Venue internet at the finale is unreliable |
| 16 | FIRMS `type`, `version` and product not stored | Stored as `firms_type`, `version`, `product`; SP supersedes NRT | Needed for evaluation, and to stop double counting |
