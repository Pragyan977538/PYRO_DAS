# FireWatch — Design

How FireWatch works, and why each part is built the way it is. The build stages and their
acceptance tests are in [ROADMAP.md](ROADMAP.md); every number quoted here comes from a
report in [`reports/`](../reports).

---

## 1. The problem

NASA FIRMS reports that a pixel is hot. It does not report *what* is hot. A refinery
flare, a stubble fire, a forest fire and a chemical-plant explosion all arrive as the
same point.

FIRMS' archive does carry a `type=2` "static land source" flag, but it has three limits:
- It says only that a site recurs, not what the site is.
- It is not available in near-real time.
- It cannot say whether today's reading is abnormal.

FireWatch learns each location's thermal fingerprint from a decade of history, and uses it
to answer the question FIRMS cannot: **is this normal for this place?**

**Deliverables of PS 26162:**

1. Classification and segregation of industrial fires from forest fires and other natural
   fires
2. A GIS-based solution for data storage and visualisation as a map overlay

Event assembly and risk scoring extend the system beyond these two.

**Headline metric: per-fire recall on a verified set of industrial accidents.** These are
20–40 events verified from news reports, each with a date and a location, and Baghjan 2020
is among them. It is the only measure not derived from the data the system learns from.
Source-level classification accuracy is reported as a secondary measure.

## 2. Architecture

The system runs on two clocks.

**Offline, rebuilt weekly: the registry.**

1. The FIRMS archive is snapped to ~375 m cells.
2. Cells that burn across several years, not in one season only, are clustered into
   physical sources.
3. Each source gets a **fingerprint**: persistence, seasonality, the distribution of fire
   radiative power (FRP), and temperature where VIIRS Nightfire has it.
4. Each source also gets a **baseline**: median, MAD and p99, keyed by instrument ×
   day/night × season.
5. One XGBoost model (Model 1) classifies each source as **mining and coal fires**,
   **heavy industry**, or **oil and gas**.

The industrial / forest / agricultural split at the level of single detections
(deliverable 1) comes from the recurrence gate, Road A's rules and Road C. It does not
come from these three sub-types.

**Online, every three hours: routing.** Each new detection asks one question: *is there a
known source within 500 m?*

- **No → Road A.** There is no baseline. Transparent rules plus physics assign a class:
  land cover, distance to industrial footprints, season, and temperature where available.
  The rule that fired becomes the "reason" text on the map.
- **Yes, inside its envelope → Road B.** Infrastructure operating normally. A map layer,
  no alert.
- **Yes, breaching its own baseline → Road C.** An anomaly at a running plant, in two
  tiers:
  - an extreme breach on a single pass raises a **provisional** alert
  - a breach on two consecutive passes raises a **confirmed** alert

Road A sites that keep burning become **provisional** sources in the registry, and keep
alerting until they look like stable infrastructure. The registry grows, but never
silently absorbs an accident. In the database schema the roads are numbered A = 1, B = 2,
C = 3.

### 2.1 Events and risk

Routed detections are assembled into **events**, so that one real fire is one incident:
- **Fires:** Road A detections within 750 m and 72 h are linked, with the event's extent
  capped at 10 km.
- **Anomalies:** Road C passes at one source, grouped into 72 h runs.
- **New persistent sites:** one event for the life of a provisional source, including the
  Road A detections before its promotion.
- **Lifecycle:** an event is active up to 24 h after its last detection, dormant up to 72 h,
  then closed.

Each event is scored for risk, and the decomposition is shown with it:

```
Risk = 100 × H^0.40 × E^0.35 × V^0.25        H, E, V each floored at 0.05
```

| Term | Definition |
|---|---|
| **H** hazard = 0.5 f + 0.3 n + 0.2 g | f: percentile of the peak pixel's FRP in the national history; n: pixels in the biggest pass, as n / (n + 3); g: growing 1, flat 0.5, shrinking 0 |
| **E** exposure = 0.45 p5 + 0.30 pw + 0.25 a | p5: people within 5 km; pw: people in the 30° sector downwind to 10 km; both ranked against historical fire locations. a = 1 − exp(−A/2), where A is the criticality-weighted count of assets within 10 km |
| **V** vulnerability | criticality × exp(−d / 2 km) of the nearest critical asset within 2 km, or the burning source's own class, whichever is higher |

The score is **multiplicative on purpose**. A huge fire in empty land, next to nothing of
value, must score low; an additive score would rate it high on hazard alone. The
exponents are a starting point, not a calibration (`reports/stage7_risk.md`).

## 3. Design principles

Each rule below was established by measurement, several of them twice. Changing one needs
the same kind of evidence. Revisions are recorded in the [decision log](#8-decision-log).

### 3.1 No label is built from a model input

Training labels come from weak supervision. Suppose a label is derived from something the
model can also see. Then the model learns the labelling rule, scores close to 100%, and
has learned nothing. Concretely:

- **Model 1 never sees location features.** It gets no `dist_industrial`, `landcover`,
  latitude, longitude or state, because its labels come from location: OSM tags,
  WorldCover, FSI.
- **FIRMS `type` is never a label or a feature.** FIRMS derives `type=2` from recurrence,
  which is what `persistence` measures. It is stored as `firms_type` and used only as an
  evaluation comparator.
- **Cropland labels come from WorldCover land cover only, never from season.** Season is a
  feature (`month_entropy`).
- **Road A is rules plus physics, not a trained model.** Its inputs, land cover and
  distance to industry, are exactly the layers every available label is built from. A
  model trained there could only memorise the rule, so Model 1 is the only learned model.
- **No absolute brightness temperatures in Model 1.** A pixel's I4 / I5 temperature carries
  its background's climate (Rajasthan in May, Assam in the monsoon), so it is a location
  proxy. The I4 − I5 contrast carries the fire, and is used.

`scripts/train.py` declares the inputs used to build each label (`LABEL_INPUTS`). It
**asserts** that `FEATS ∩ LABEL_INPUTS` is empty, and fails loudly if not.

### 3.2 Weak labels, class by class

Adopted 2026-09-29, from a census of how many *registry sources* each group labels
(`reports/stage1_5b_multiyear.md`). Recounted in Stage 4 on the 557-source registry
(`firewatch/models/labels.py`).

A source takes the first group with evidence within 1 km of any of its cells, most
specific first:

```
oil_gas > steel_cement > thermal_power (OSM or WRI GPPD) > mining > kiln (no label) > industrial_other
```

Mining sits above generic `landuse=industrial` because coalfields are ringed by washeries
and depots tagged industrial, and the fire is in the mine. Where a class has no honest
label source, the class is left empty: labels are never invented to fill it.

| Model 1 class | Label sources | Sources labelled | Caveat |
|---|---|---|---|
| oil_gas | `industrial=refinery`, `industrial=oil`, `man_made=petroleum_well`, `man_made=flare` | 34 (census 27) | Thin, and the class that matters most operationally, so its recall is reported separately. Had it been unusable, the map would name the nearest OSM oil and gas facility as a display lookup, never as a class |
| heavy_industry | `power=plant` with `plant:source` coal/gas/oil/diesel/biomass; `man_made=works`; `industrial=steel`/`cement`/`factory`; `landuse=industrial`; WRI Global Power Plant Database (combustion plants) | 267 (census 215) | Power, steel, cement and smelters are merged: captive power plants inside steel works and smelters make them inseparable by label |
| mining | `landuse=quarry`, `resource=coal`, `industrial=mine`, `man_made=mineshaft` | 165 (census 163) | Coal-seam fires (Jharia) are fires *in* mines, not mining activity. They are labelled mining, and the reports say so |
| recurrent_biomass (conditional) | WorldCover cropland (40) or tree cover (10) at the source, never season | **not added** | Needed ≥ 30 candidates *and* fewer than half of them on a GIHS-confirmed industrial site. Stage 4 found 42 candidates, but 81% sat on confirmed industry (92% of the tree-cover ones): mines and plants in forested or farmed country that OSM has not mapped. Recurrent biomass does not survive the gate |
| — | FIRMS `type=2`, GIHS | — | Evaluation only, never labels |

- **Dropped:** kiln. Two sources were labelled, both wrongly; one is a Raniganj coal fire.
  Flare and furnace are no longer separate classes.
- **Not trained on:** 91 sources with no label (89 with no evidence, 2 kiln-first). 79% of
  the evidence-free ones are on GIHS sites, mostly unmapped industry. They are predicted.
- **Contested:** 119 labelled sources have evidence for two classes within 1 km, usually a
  refinery or a mine inside an industrial estate. Model 1 is right on 52% of them, against
  70% of the uncontested ones.
- **Forest and cropland fires** at the detection level come from Road A (WorldCover and
  FSI alerts), not from Model 1. FSI alerts are FIRMS points inside forest boundaries, with
  partial state feedback: weak labels, not validated ones.

### 3.3 Each source is compared only with itself

A large furnace and a small furnace differ by an order of magnitude in normal FRP. Pooled
together, a real fire at the large one may sit below the pooled p99, while the small one
breaches it during normal operation. **Baselines are per source, never per source type.**

Keys are **instrument (VIIRS | MODIS) × day/night × season**, never the individual
satellite:
- Suomi NPP data delivery ends on 1 Nov 2026, and NOAA-21 has little history.
- The three VIIRS units run the same 375 m algorithm, so their history pools.
- Each bucket needs n ≥ 30. Otherwise it falls back to instrument × day/night, then to the
  source-wide baseline.

- **The unit is the pass:** one sensor's overpass of one source. Its value is the
  **hottest pixel**, and baselines are built from pass maxima. The most extreme of k pixels
  is not one pixel, and a pass total would drown a one-pixel fire at a big site.
- **The anomaly test stops at the instrument.** A pass is judged only against its own
  instrument's baseline; the source-wide baseline describes, and never judges. That pool is
  mostly VIIRS, and MODIS detects only the bigger fires, so a MODIS pass looks abnormal
  against it by construction. Removing that bias alone halved the confirmed false
  positives (Stage 5).

### 3.4 Median and MAD, never mean and standard deviation

FRP is heavily right-skewed. One past explosion permanently inflates σ, and a σ-based
detector then stops firing at that site for good.

```python
z = 0.6745 * (frp - med) / max(mad, 1e-6)
breach  = (z > 3.5) and (frp > p99 * 1.5)                      # both: they fail differently
extreme = (z > Z_EXTREME) and (frp > p99 * P99_EXTREME)        # calibrated: 7 and 6
```

- **Confirmed alert:** a `breach` on two consecutive passes. That lowers the
  false-positive rate by roughly an order of magnitude.
  - "Consecutive" means the previous detection pass at the source, from any sensor, with no
    normal pass in between, and **no time limit**.
  - A weak source is not detected on every overpass. A 24 h limit cut injected-spike recall
    from 90% to 55% on real 2024 histories, and barely moved the false positives.
- **Provisional alert:** an `extreme` reading on a single pass. Short blasts are the
  deadliest events, and they often burn out between passes; once S-NPP stops, a site is
  seen in about two windows a day. The thresholds live in config (`ANOMALY_EXTREME`) and
  were calibrated in Stage 5 against spikes injected into real histories:
  **z > 7 and > 6× p99**.
  - The starting value, 3× p99, fired on 0.045% of real 2023 passes against a 0.01%
    target.
  - At 6× p99 the tier catches only ~7% of single-pass spikes. This is an operational
    trade-off, open to revision.

### 3.5 Class B is the complement of Class C

Class B means "neither alert tier fired". It is **not** "FRP below the median": half of all
normal readings sit above the median by definition.

### 3.6 The registry holds persistent sources, not landscapes

Raw DBSCAN over detections turns crop and forest landscapes into "sources". On three real
years (2021–2023, `reports/stage1_5b_multiyear.md`):
- It merged **the whole Punjab paddy belt into one cluster**: 379,180 detections, 369 km
  corner to corner. 98% were in stubble months, and none carried FIRMS' static flag.
- **97%** of the belt's detections, and **75.5%** of all detections in India, would have
  skipped Road A.
- On one year the belt merely fragmented into 2–10 km field clusters, with 60% inside a
  "source". The chaining grows with history.

The registry is built in five steps instead:

1. **Project to metres with EPSG:7755** (WGS 84 / India NSF LCC), always through
   `firewatch.grid.to_metres`, the one implementation.
   - The projection is conformal, so a local distance is right in every direction, with
     scale error within ~2% across India.
   - Degrees are not a distance: one degree of longitude is 111 km at the equator and 85 km
     at Kashmir's latitude.
   - The per-point shortcut `x = lon * 111320 * cos(lat)` shears the plane. At Jharia, two
     points 500 m apart north–south come out 586 m apart: the whole scale of a DBSCAN
     neighbourhood.
2. **Snap to ~375 m cells** (the VIIRS pixel), and aggregate detections, distinct days,
   nights, months and years.
3. **Gate on recurrence.** Keep cells that burn across multiple years *and* across much of
   the year: within a year, **≥ 3 distinct months on ≥ 10 distinct days, in at least 3
   years** of the 2012–2024 archive (`REGISTRY_GATE=3,10,3`). Each step below was decided
   by a rule fixed before its sweep ran:
   - One year of data suggested ≥ 4 months.
   - Three years (2021–2023) moved it to 3 months in ≥ 2 years (F1 81.6% vs 80.4%): 477
     sources, 78% GIHS recall, 86% on GIHS sites.
   - The full archive moved it to ≥ 3 years.
     - Over 13 years, two qualifying years is a looser test than two of three. The two-year
       gate registered 679 sources, with only 79.5% on a GIHS site: below the 85% floor.
     - Three years gives 557 sources, 83.4% GIHS recall, 86.2% on GIHS sites and 0.014% of
       the paddy belt misrouted. Reliance, Nayara and HMEL are all found
       (`reports/stage3/sweep.md`).

   A multi-year rule keeps a single long accident (Baghjan) out by construction. Rerun the
   sweep (`scripts/build_registry.py --sweep`) when the archive grows by years.
4. **Cluster the surviving cells** with
   `DBSCAN(eps=500, min_samples=5, algorithm="ball_tree")`, fitted with
   `sample_weight=n_detections`. `eps=500` because VIIRS pixels are 375 m.
5. **Cap the footprint.** A cluster wider than a few kilometres is a landscape, not a
   source. It is flagged, not registered.

Cells that fail the gate, and DBSCAN noise (`cluster == -1`), are Road A territory.

**Memory.** scikit-learn's DBSCAN materialises every point's neighbour list, so memory
grows with the square of cluster size, whatever the tree algorithm. Measured:
- `ball_tree` and `kd_tree` gave identical labels and the same memory: 1,321 and
  1,323 MB on one year. `"auto"` already picks `kd_tree`.
- Three years took 8.6 GB and 277 s, against 1.3 GB and 12 s for one. The full 2012–2024
  archive would need about 55 GB raw.
- The gated method took 271 MB.

Gridding plus `sample_weight` is what bounds memory, as scikit-learn's own documentation
recommends. There is no state-by-state partitioning: sources can straddle borders, and the
grid makes it unnecessary.

### 3.7 Promotion never silences an alert

A Road A site that keeps burning becomes a **provisional** source. It keeps alerting until
it looks like stable infrastructure: classified by Model 1 as an industrial class, with a
stable fingerprint, and ideally confirmed by an analyst. Accidents can burn for months; the
Baghjan blowout (Assam, 2020) burned for about five. **Baghjan is the regression test.** It
must stay an alert for its whole life and never become "normal".

Promotion counts persistence over **observable** days, like everything else:
- A Road A cluster no wider than 2 km is promoted when two things hold since its first fire:
  - it burned on ≥ 10 distinct days
  - it burned on ≥ 50% of the days it could be seen (cloud from NASA POWER)
- A calendar rule (≥ 20 days) promoted Baghjan 66 days after it caught fire, because the
  monsoon hid it. The observable-day rule promotes it in 21. It is never Road B.
- Nothing automatic clears `provisional`, not even the weekly registry rebuild. Only an
  analyst does (`promotion.confirm`).

### 3.8 Temperature statistics are NaN-safe, and VIIRS Nightfire is optional

Temperature comes from VIIRS Nightfire (VNF), and many detections have none: VNF is
night-only, and its Planck fit fails on cooler sources. A plain `np.median` returns NaN for
the whole cluster, so `np.nanmedian` and `np.nanquantile` are used everywhere.

Missing temperatures are **not imputed**. XGBoost handles missing values natively, by
learning a default split direction. A daytime detection genuinely has no temperature
measurement, and filling in a median would invent data. `temp_cov`, the fraction of a
source's detections that *had* a temperature, is itself a useful feature.

**VNF is optional enrichment:**
- It is licence-gated: a free academic licence needs a signed agreement and approval.
- It is night-only, and only **29% of Indian detections are at night** (2023, measured).
  So VNF can annotate at most 29% of detections.
- It is still a strong feature at sources, which are seen on hundreds of nights a year.

The pipeline runs end to end without it.

### 3.9 Persistence is measured against observable nights

Monsoon cloud hides sites for weeks, so calendar-night persistence understates every
source. Persistence = **nights with a detection / nights the site was observable**:
numerator and denominator both nights.

FIRMS publishes detections only: no swath footprints, no cloud masks. Observability
therefore comes from **NASA POWER's daily cloud amount (CERES SYN1deg)**: free, no key,
satellite-observed cloud on a 1° grid.
- Expected clear nights = Σ(1 − daily cloud fraction).
- The limitation: it is a daily mean at 1°, not the sky at the overpass.
- Every cell-date is stored, whether or not anything burned
  (`firewatch/ingest/observability.py`).

### 3.10 Sensor schemas are normalised at ingestion

MODIS emits `brightness` / `bright_t31`; VIIRS emits `bright_ti4` / `bright_ti5`. Both are
normalised to `bt4` / `bt5` **at ingestion** (`firewatch/ingest/normalize.py`); otherwise
half the joins silently drop rows. FIRMS `type` becomes `firms_type`.

### 3.11 Science-quality data supersedes near-real-time; no pixel counts twice

The archive (SP, science-quality) and live (NRT) products overlap for recent months.
Reprocessing moves positions slightly, so the natural key does not catch the duplicate.
When SP covers a sensor-day, that sensor-day's NRT rows are dropped: by `dedupe_sp_nrt`
within a batch, and by `supersede_nrt` in the database. Every row stores `product` and
`version`.

### 3.12 OpenStreetMap comes from Geofabrik, not Overpass

Overpass was unreachable during feasibility testing: a 503 on the main endpoint, and
timeouts on two mirrors. FireWatch downloads the Geofabrik India extract (~1.7 GB) and
filters it locally with pyosmium, which needs no native tools on Windows. This is
reproducible, faster, and cannot be down during a demonstration.

### 3.13 The critical-asset register is static

Nuclear plants dump waste heat into cooling water at 30–40 °C, far below satellite
detection. They are thermally invisible until something burns. The same holds for
ammunition depots and LPG bottling plants. If criticality were derived from thermal
history, the highest-consequence assets would score zero.

- **Source.** For the prototype the register is **generated from maps**, never from fires
  (`firewatch/risk/assets.py`):
  - WRI's power plant list, including India's 9 nuclear plants at criticality 1.0
  - OSM refineries, LNG/LPG, chemical and fertiliser works, steel and cement, mines, kilns
    and industrial estates, typed from their tags
- **Size.** 48,107 entries in all.
- **Manual rows.** Rows added by hand (`source_ref` `manual:…`) survive reseeding, so
  PESO, CEA and MoPNG lists can be layered on.

## 4. Data sources

Real data is the default. Nothing needs a credential to start.

| Source | Access | Role |
|---|---|---|
| FIRMS archive | Public yearly India CSVs, **no key**: `firms.modaps.eosdis.nasa.gov/data/country/{viirs-snpp,viirs-jpss1,modis}/{YYYY}/{sensor}_{YYYY}_India.csv`. S-NPP 2012–2024, NOAA-20 2018–2024, MODIS 2000–2024 | Primary history. Carries `type` and `version` |
| FIRMS API | Free `FIRMS_MAP_KEY` | 2025 onward, and the live 3-hourly NRT pull. Sensor-agnostic: S-NPP ends 1 Nov 2026 |
| VIIRS Nightfire | EOG academic licence | Optional temperature enrichment |
| GIHS (Ma et al. 2024) | Zenodo 10.5281/zenodo.10570342, CC BY 4.0 | Imagery-verified industrial heat sources, 2012–2021. Registry evaluation, never labels |
| OpenStreetMap | Geofabrik India extract | Weak labels, Road A context |
| ESA WorldCover | 10 m land-cover COGs | Forest and cropland labels, Road A context |
| WRI Global Power Plant Database | Public CSV | Weak labels, critical assets |
| FSI fire alerts | fsiforestfire.gov.in | Weak forest labels |
| NASA POWER | Free, no key | Daily cloud amount, CERES SYN1deg 1° (observability) |
| NASA POWER wind | Free, no key | Daily U/V wind at 10 m, MERRA-2 grid (risk: the downwind exposure term) |
| WorldPop 2020, 1 km | Free, CC BY 4.0, one 19 MB file | Population (risk exposure) |
| Natural Earth | Public domain | The offline basemap |

- **Modes.** `MOCK_MODE=0` (the default) runs against real data. `MOCK_MODE=1` switches to
  a synthetic **test fixture**, offline and deterministic, for tests and CI.
- **Anomaly testing** injects synthetic spikes into *real* source histories rather than
  simulating a world.
- **The fixture** models biomass fires as spatially diffuse, never as point sources.
  Modelling them as point sources is what once hid the landscape-chaining problem (§3.6).

Each client checks for its own credential only when it is used. `FIRMS_MAP_KEY` is needed
only for API pulls, and VNF skips cleanly without EOG credentials.

## 5. Technology stack

| Layer | Choice |
|---|---|
| Language | Python 3.13 (every pinned wheel exists for 3.13 on Windows; not yet for 3.14) |
| Database | PostgreSQL 16 + PostGIS 3.4; TimescaleDB optional (auto-detected by the schema) |
| Machine learning | scikit-learn (DBSCAN), XGBoost: one learned model |
| Backend | FastAPI, with vector tiles from PostGIS `ST_AsMVT` |
| Frontend | Vanilla JavaScript + MapLibre GL JS (vendored), offline Natural Earth basemap |
| Deployment | Docker Compose; native PostgreSQL + PostGIS as a fallback |

**No deep learning.** About 20 tabular features and a few hundred labelled sources is the
regime where gradient boosting wins. Feature importances are also needed to justify alerts
to a government agency.

## 6. Engineering conventions

- **Configuration** is environment-driven through `firewatch/config.py`. There are no
  hard-coded paths or keys.
- **Geometry** is stored in EPSG:4326. All distance work reprojects to metres with
  EPSG:7755 through `firewatch.grid`.
- **Environment:** Python 3.13 in a virtualenv (`py -3.13 -m venv .venv`).
- **Two runners:** every stage exposes the same target in `make <target>` and
  `.\make.ps1 <target>` (Windows PowerShell 5.1 compatible), plus a check script.
- **Done means tested:** a stage is done only when its acceptance command in
  [ROADMAP.md](ROADMAP.md) passes **with the database tests running**. Skipped database
  tests are not a pass.
- **Code style:** type hints on public functions. Docstrings explain *why*, not *what*.
- **Never committed:** `.env`, data files, or trained model binaries.

## 7. Key measurements

The full numbers are in the reports; these are the ones the design rests on.

**Verified industrial accidents (headline)** — 18 located accidents, 2013–2024, each
replayed from 60 days before it (`reports/validation.md`):
- **Per-fire recall: 1 of 18** (Baghjan), against 0.12 expected by chance. Baghjan alerted
  from promotion, 21 days in, under Road A's class "forest".
- **FIRMS saw only 6 of 18.** The other 12 burned between satellite passes, under monsoon
  cloud, or inside buildings.
- **Of the six seen:**
  - three were Road B, below their plants' own baselines (Haldia, Bhilai, Tata Steel)
  - two were unmapped factories that Road A classed by land cover (Harda, Dombivli)
- **Mean time to the first FIRMS detection:** 6.0 h (n = 5).

**The registry, full archive** — VIIRS over India, 2012–2024: 11,485,898 detections, plus
1,068,405 MODIS (`reports/stage3_registry.md`):
- **557** sources from 2,745 cells; the widest is 8.0 km.
- GIHS recall **83.4%**, with **86.2%** of sources on a GIHS site.
- Paddy belt **0.014%** misrouted; FIRMS-static coverage **99.3%**.
- 12.5% of VIIRS detections land on a source; the rest go to Road A.
- Night persistence: median 0.23, and 0.94–0.96 at the large coal, steel and refinery
  complexes.

**Model 1** — 466 labelled sources, 5-fold GroupKFold on 2° blocks, fingerprint features
only (`reports/stage4_model1.md`):
- Accuracy **65.2%**, balanced accuracy **61.2%**, macro-F1 **0.62**.
- References:
  - majority class 57.3% (33.3% balanced)
  - latitude/longitude only: 40.8% balanced
  - shuffled labels: 33.3% balanced on average, none of 100 above 41.7% (p ≈ 0.01)
- Oil and gas: recall **58.8%**, precision **64.5%**.
- The published 77% (Liu et al. 2018) used VNF temperature, which is not available here.

**Routing, 2024 replay** (`reports/stage5_inference.md`):
- 1.20 M detections: Road A 86.3%, Road B 12.8%, Road C 0.86%.
- Road C against spikes injected into real histories:
  - confirmed recall **88.2%**
  - confirmed false positives **0.040%** of passes
  - provisional false positives **0.000%**

**Why raw clustering fails** — the three- and one-year studies
(`reports/stage1_5b_multiyear.md`, `reports/stage1_5_spike.md`):
- Raw DBSCAN put 379,180 detections in one cluster spanning the paddy belt.
- `ball_tree` ≈ `kd_tree` ≈ 1.32 GB on one year; the gated method uses 271 MB.
- 29.0% of 2023 detections are at night, and 12.7% carry FIRMS' `type=2`.

**Synthetic benchmark** (`reference/verify_pipeline.py`) — upper bounds only. It draws from
the distributions the model learns, *and* models stubble and forest fires as tight point
sources:
- source classifier 94.5%
- 0.78% anomaly false positives before two-pass confirmation
- 96.6% detection of 6–12× excursions

## 8. Decision log

Decisions revised during development. Each row keeps the original rule, so the history is
not lost.

| # | Original rule | Current rule | Evidence |
|---|---|---|---|
| 1 | DBSCAN on raw detections; `ball_tree` fixes memory; cluster state by state | 375 m cells, recurrence gate, DBSCAN with `sample_weight`, footprint cap; no state partitioning | Stages 1.5/1.5b, real data: on 2021–2023, raw DBSCAN merged the whole Punjab paddy belt into one 369 km cluster (97% of its detections). The synthetic benchmark hid this by modelling biomass fires as point sources. `ball_tree` and `kd_tree` used identical memory |
| 2 | Single-tier, two-consecutive-pass confirmation | Provisional alert on one extreme pass; confirmed alert on two consecutive breaching passes | Short blasts, the deadliest events, often burn out between passes |
| 3 | Road A sites repeating ~20 nights are promoted into the registry | Promoted sources stay provisional and keep alerting until they look like stable infrastructure; Baghjan 2020 is the regression test | A long-burning accident would otherwise become "normal" within a month |
| 4 | Baselines keyed `sensor \| daynight \| season` (per satellite) | Keyed `instrument \| daynight \| season` | S-NPP delivery ends 1 Nov 2026; NOAA-21 has little history; VIIRS units share one algorithm |
| 5 | "Model 1 must never see location features" | "No label may be built from a model input": covers location, FIRMS `type`, season | The narrow rule missed other circular labels |
| 6 | Two XGBoost models: Model 1 plus a Road A classifier | One learned model; Road A is rules plus physics, whose rule path is the map's reason text | Road A's features are the layers its labels would be built from |
| 7 | Six classes (flare, furnace, kiln, mining, forest, cropland) | Three Model 1 classes from a census of labelled registry sources: oil and gas 27, heavy industry 215, mining 163. Kiln dropped; forest and cropland handled by Road A | OSM has 22 flare tags in India; power and steel cannot be separated by label; kiln labels at sources were wrong |
| 8 | Synthetic data by default; the mock is the demo dataset | Real data first; the mock shrinks to a test fixture; spikes injected into real histories | Ten years of FIRMS for India is free; a synthetic world hid problem 1 |
| 9 | Observability written per pass from swath and cloud mask | Cloud cover as a proxy (see 18) | FIRMS publishes no swaths or cloud masks |
| 10 | VNF via a free account, required in live mode | Licence-gated and optional; the pipeline runs fully without it | EOG moved VNF behind a licence on 10 Jan 2025 |
| 11 | FSI alerts are "validated" forest fires | FIRMS points inside forest boundaries with partial state feedback: weak labels | That is what FSI's own FAQ says they are |
| 12 | Headline: source-level accuracy vs 77% | Headline: per-fire recall on the verified industrial fire set; 77% stays the reference for source sub-classification | Deliverable 1 is about fires; the verified set is the only non-circular number |
| 13 | Python 3.12; TimescaleDB required | Python 3.13; TimescaleDB optional | Windows wheels exist for 3.13 (not 3.14); native PostgreSQL fallback |
| 14 | Persistence = distinct detection dates / observed nights | Nights detected / nights observable | Day dates over a night denominator can exceed 1 |
| 15 | Online basemap | Offline basemap | Venue internet is unreliable |
| 16 | FIRMS `type`, `version` and product not stored | Stored as `firms_type`, `version`, `product`; SP supersedes NRT | Needed for evaluation, and to stop double counting |
| 17 | Metres from `x = lon * 111320 * cos(lat)`, each point's own latitude | EPSG:7755 through `firewatch.grid.to_metres`, the one implementation | The per-point cosine shears the plane: at Jharia, a 500 m north–south pair measured 586 m. A Stage 1 test caught it, and every study was rerun with the fix |
| 18 | Cloud cover from ERA5 via Open-Meteo | NASA POWER daily cloud amount (CERES SYN1deg, 1°): one regional request per 10° tile-year, ~160 for India 2012–2024 | Open-Meteo's free tier counts each 14 days per location as a call: 13 years for a few hundred cells is ~135,000 calls against 10,000 a day. POWER is also satellite-observed rather than reanalysis; the price is 1° daily instead of 0.25° at the overpass |
| 19 | Recurrence gate in ≥ 2 years (set on 2021–2023) | ≥ 3 years on the full 2012–2024 archive; months and days unchanged | The rule fixed before the full-archive sweep: the two-year gate missed the 85% on-GIHS floor (79.5%, 679 sources); the best passing setting was ≥ 3 years (557 sources, 83.4% recall, 86.2% on GIHS) |
| 20 | recurrent_biomass added if ≥ ~30 unlabelled sources sit on cropland or forest | Also needs fewer than half the candidates on a GIHS-confirmed industrial site; GIHS decides only whether the class exists, never a label | Stage 4: 42 candidates, 81% on confirmed industry. The WorldCover rule was labelling unmapped mines and plants as biomass |
| 21 | Label groups in table order (heavy industry before mining) | Most specific first: oil_gas > steel_cement > thermal_power > mining > kiln (no label) > industrial_other | The census counts were computed in that order; generic `landuse=industrial` around coalfields would otherwise relabel coal fires as heavy industry |
| 22 | Baselines computed on detections (pixels) | Computed on passes: each sensor overpass's hottest pixel | A pixel baseline flags every large multi-pixel site on most passes, because the most extreme of k pixels is not one pixel |
| 23 | Anomaly test falls back to the source-wide baseline | Own-instrument baselines only; the source-wide one is descriptive | Most 2023 confirmed false positives were MODIS passes judged against a mostly-VIIRS pool. Removing it halved them (0.161% → 0.083% on 2023, 0.059% → 0.040% on 2024) |
| 24 | Promote a Road A site after ~20 nights | ≥ 10 distinct days *and* ≥ 50% of observable days since its first fire | Calendar days promoted Baghjan 66 days after it caught fire (the monsoon hid it); observable days, 21 |
| 25 | Extreme tier z > 7 and > 3× p99 | z > 7 and > 6× p99 (calibrated on 2023) | 3× p99 fired on 0.045% of real passes against a 0.01% target. The cost: single-pass recall falls from 62% to ~7% |
| 26 | Critical-asset register: ~200 entries compiled by hand from PESO, CEA and MoPNG | Generated from OSM tags and WRI's power plant list (48,107 entries, including 9 nuclear plants); manual rows survive reseeding | Manual compilation deferred for the prototype. Still static and never thermal, so §3.13 holds |
| 27 | Wind for the downwind term from Open-Meteo | NASA POWER daily U10M/V10M components | The same keyless service as the cloud record. POWER's daily `WD10M` is unreliable, and a day's mean direction is meaningless where its mean vector is not |
| 28 | Exposure's asset term: a count of assets within 10 km | A criticality-weighted count | 41,000 of the 48,000 entries are mines and industrial estates; an unweighted count would rate every coal-belt fire as exposed as one beside a refinery |
