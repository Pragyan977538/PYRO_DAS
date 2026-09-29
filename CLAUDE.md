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
season). One XGBoost model classifies each source: **mining and coal fires**, **heavy
industry**, or **oil and gas**. The output is the **registry**. Industrial vs forest vs
agricultural at the detection level (deliverable i) comes from the gate, Road A's rules
and Road C, not from these sub-types.

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
- **No absolute brightness temperatures in Model 1.** A pixel's I4 / I5 temperature
  carries its background's climate — Rajasthan in May, Assam in the monsoon — so it is
  a location proxy. The I4 − I5 contrast carries the fire and is used.

`scripts/train.py` must declare the inputs used to build each label (`LABEL_INPUTS`) and
**assert** that `FEATS ∩ LABEL_INPUTS` is empty, failing loudly if not.

### Weak-label sources, class by class

Approved 2026-09-29, from a census of how many *registry sources* each group actually
labels (`reports/stage1_5b_multiyear.md`), and recounted in Stage 4 on the 557-source
registry (`firewatch/models/labels.py`). A source takes the first group with evidence
within 1 km of any of its cells, most specific first:

```
oil_gas > steel_cement > thermal_power (OSM or WRI GPPD) > mining > kiln (no label) > industrial_other
```

That is the order the approved census counted in. Mining sits above generic
`landuse=industrial` because coalfields are ringed by washeries and depots tagged
industrial, and the fire is in the mine. Where a class has no honest label source, say
so — never invent labels to fill it.

| Model 1 class | Label sources | Sources labelled | Caveat |
|---|---|---|---|
| oil_gas | `industrial=refinery`, `industrial=oil`, `man_made=petroleum_well`, `man_made=flare` | 34 (census 27) | Thin, and the class NTRO cares most about: report its recall separately. If it is unusable, the map names the nearest OSM oil and gas facility — a display lookup, never a class |
| heavy_industry | `power=plant` with `plant:source` coal/gas/oil/diesel/biomass; `man_made=works`; `industrial=steel`/`cement`/`factory`; `landuse=industrial`; WRI Global Power Plant Database (combustion plants) | 267 (census 215) | Power, steel, cement and smelters are merged: captive power plants inside steel works and smelters make them inseparable by label |
| mining | `landuse=quarry`, `resource=coal`, `industrial=mine`, `man_made=mineshaft` | 165 (census 163) | Coal-seam fires (Jharia) are fires *in* mines, not mining activity. They are labelled mining, and the report says so |
| recurrent_biomass (conditional) | WorldCover cropland (40) or tree cover (10) at the source, never season | **not added** | Needs ≥ 30 candidates *and* fewer than half of them on a GIHS-confirmed industrial site. Stage 4 found 42 candidates, but 81% sat on confirmed industry (92% of the tree-cover ones): mines and plants in forested or farmed country that OSM has not mapped. Recurrent biomass does not survive the gate |
| — | FIRMS `type=2`, GIHS | — | Evaluation only, never labels |

- **Dropped:** kiln (2 sources labelled, both wrong — one is a Raniganj coal fire).
  Flare and furnace are no longer separate classes.
- **Not trained on:** the 91 sources with no label (89 with no evidence, 2
  kiln-first; 79% of the evidence-free ones on GIHS sites, mostly unmapped
  industry). They are predicted.
- **Contested:** 119 labelled sources have evidence for two classes within 1 km, usually
  a refinery or a mine inside an industrial estate. Model 1 is right on 52% of them
  against 70% of the uncontested ones.
- **Road A, not Model 1:** forest and cropland fires at the detection level come from
  WorldCover and FSI alerts. FSI alerts are FIRMS points inside forest boundaries with
  partial state feedback: weak, not validated.

### Baselines are per source, never per source type

A large furnace and a small furnace differ by an order of magnitude in normal FRP. Pooling
them means a real fire at the large one may sit below the pooled p99 while the small one
breaches it during normal operation. **A site is compared only against itself.**

Keys are **instrument (VIIRS | MODIS) × day/night × season** — never the satellite.
Suomi NPP delivery ends on 1 Nov 2026, before the finale, and NOAA-21 has little history.
The three VIIRS units run the same 375 m algorithm, so their history pools. Each bucket
needs n ≥ 30; otherwise fall back to instrument × day/night, then to the source-wide
baseline.

- **The unit is the pass:** one sensor's overpass of one source. Its value is the
  **hottest pixel**, and baselines are built from pass maxima. The most extreme of k
  pixels is not one pixel, and a pass total would drown a one-pixel fire at a big site.
- **The anomaly test stops at the instrument.** A pass is judged only against its own
  instrument's baseline; the source-wide one describes and never judges. The pool is
  mostly VIIRS, and MODIS only detects the bigger fires, so a MODIS pass looks abnormal
  against it by construction. That bias alone halved the confirmed false positives when
  removed (Stage 5).

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
  - "Consecutive" means the previous detection pass at the source, from any sensor,
    with no normal pass between, and **no time limit**.
  - A weak source is not detected on every overpass. A 24 h limit cut injected-spike
    recall from 90% to 55% on real 2024 histories, while false positives barely moved.
- **Provisional alert:** `extreme` on a single pass. Short blasts are the deadliest events,
  and they often burn out between passes; after S-NPP stops a site is seen in about two
  windows a day. Thresholds live in config (`ANOMALY_EXTREME`) and were calibrated in
  Stage 5 against spikes injected into real histories: **z > 7 and > 6× p99**.
  - The starting 3× p99 fired on 0.045% of real 2023 passes, against a 0.01% target.
  - At 6× p99, the tier catches only ~7% of single-pass spikes. That trade-off is
    the user's to revisit.

### Class B is the complement of Class C, not a separate test

Class B is simply "neither alert tier fired". It is **not** "FRP below median" — half of
all normal readings sit above the median by definition.

### The registry holds persistent sources, not landscapes

Raw DBSCAN over detections turns crop and forest landscapes into "sources". On three
real years (2021–2023, `reports/stage1_5b_multiyear.md`):
- It merged **the whole Punjab paddy belt into one cluster**: 379,180 detections,
  369 km corner to corner, 98% in stubble months, none carrying FIRMS' static flag.
- **97%** of the belt's detections, and **75.5%** of all detections in India, would
  skip Road A.
- On one year the belt merely fragmented into 2–10 km field clusters (60% inside a
  "source"). The chaining grows with history.

The gated method keeps 477 sources and misroutes 0.01% of the belt. Instead:

1. **Project to metres with EPSG:7755** (WGS 84 / India NSF LCC), always through
   `firewatch.grid.to_metres`, the one implementation. It is conformal, so a local
   distance is right in every direction, with scale error within ~2% across India.
   Degrees are not a distance: one degree of longitude is 111 km at the equator and
   85 km at Kashmir's latitude.

   **Never use `x = lon * 111320 * cos(lat)` with each point's own latitude.** It
   shears the plane: at Jharia, two points 500 m apart north–south come out 586 m
   apart, the whole scale of a DBSCAN neighbourhood.

2. **Snap to ~375 m cells** (the VIIRS pixel) and aggregate: detections, distinct days,
   nights, months and years.
3. **Gate on recurrence:** keep cells that burn across multiple years *and* across much of
   the year. Within a year, **≥ 3 distinct months on ≥ 10 distinct days, in at least 3
   years** of the 2012–2024 archive (`REGISTRY_GATE=3,10,3`). How it got there, each
   step by a rule fixed before its sweep ran:
   - One year proposed ≥ 4 months.
   - Three years (2021–2023) moved it to 3 months in ≥ 2 years (F1 81.6% vs 80.4%):
     477 sources, 78% GIHS recall, 86% on GIHS.
   - The full archive moved it to ≥ 3 years. Over 13 years, two qualifying years is a
     looser test than two of three: the two-year gate registered 679 sources with only
     79.5% on a GIHS site, below the 85% floor. Three years gives 557 sources, 83.4%
     GIHS recall, 86.2% on GIHS, 0.014% of the paddy belt misrouted, and Reliance,
     Nayara and HMEL all found (`reports/stage3/sweep.md`).

   A multi-year rule keeps a single long accident (Baghjan) out by construction.
   Thresholds live in config (`REGISTRY_GATE`); rerun the sweep
   (`scripts/build_registry.py --sweep`) when the archive grows by years.
4. **Cluster the surviving cells** with
   `DBSCAN(eps=500, min_samples=5, algorithm="ball_tree", ...)` fitted with
   `sample_weight=n_detections`.
5. **Cap the footprint.** A cluster wider than a few kilometres is a landscape, not a
   source; flag it instead of registering it.

**Memory:** scikit-learn's DBSCAN materialises every point's neighbour list, so memory
grows with the square of cluster size *whatever the tree algorithm*. Measured:
- `ball_tree` and `kd_tree` gave identical labels and the same memory (1,321 and
  1,323 MB on one year), and `"auto"` already picks `kd_tree`.
- Three years took 8.6 GB and 277 s, against 1.3 GB and 12 s for one. The full
  2012–2024 archive would need about 55 GB raw.
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

Promotion counts persistence over **observable** days, like everything else:
- A Road A cluster no wider than 2 km is promoted when it burned on ≥ 10 distinct
  days, and on ≥ 50% of the days it could be seen since its first fire (cloud from
  NASA POWER).
- A calendar rule (≥ 20 days) promoted Baghjan 66 days after it caught fire, because
  the monsoon hid it. The observable-day rule promotes it in 21. It is never Road B.
- Nothing automatic clears `provisional`, not even the weekly registry rebuild. Only
  an analyst does (`promotion.confirm`).

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
therefore comes from **NASA POWER's daily cloud amount (CERES SYN1deg)**: free, no key,
satellite-observed cloud on a 1° grid. Expected clear nights = Σ(1 − daily cloud
fraction). It is a daily mean at 1°, not the sky at the overpass — say so. Store every
cell-date whether or not anything burned (`firewatch/ingest/observability.py`).

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

### The critical asset register is static

Nuclear plants dump waste heat into cooling water at 30–40 °C — far below satellite
detection. They are thermally invisible until something burns. Same for ammunition depots
and LPG bottling plants. If criticality is derived from thermal history, the
highest-consequence assets score zero.

- **Source.** For the prototype the register is **generated from maps**, never from
  fires (`firewatch/risk/assets.py`, approved 2026-09-29):
  - WRI's power plant list, including India's 9 nuclear plants at criticality 1.0
  - OSM refineries, LNG/LPG, chemical and fertiliser works, steel and cement, mines,
    kilns and industrial estates, typed from their tags
- **Size.** 48,107 entries in all.
- **Manual rows.** Rows added by hand (`source_ref` `manual:…`) survive reseeding, so
  PESO, CEA and MoPNG lists can be layered on. Heritage sites are left out.

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
| NASA POWER | Free, no key | Daily cloud amount, CERES SYN1deg 1° (observability) |
| NASA POWER wind | Free, no key | Daily U/V wind at 10 m, MERRA-2 grid (risk: the downwind exposure term) |
| WorldPop 2020, 1 km | Free, CC BY 4.0, one 19 MB file | Population (risk exposure) |

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
- All geometry stored in EPSG:4326. Reproject to metres with EPSG:7755 via
  `firewatch.grid` for all distance work — never the per-point equirectangular
  shortcut.
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
- `reports/validation.md` — every headline number in one place: per-fire recall on the
  verified accidents, why each was missed, and the limitations table.
- `reference/verified_events.csv` — the verified industrial accidents, each with a
  source URL. Locations come from publications and maps, never from FIRMS.
- `reports/stage3_registry.md` — the registry on the full archive: 557 sources, every
  acceptance floor, and the sweep that moved the gate to three years.
- `reports/stage1_5b_multiyear.md` — the gate on three real years, the demo refineries,
  and the label census behind the proposed classes.
- `reports/stage1_5_spike.md` — the one-year clustering test: memory and the first
  gate proposal.
- `docs/plan.md` — feasibility results from the synthetic benchmark. Partly superseded:
  see Changed decisions.
- `docs/PS26162_Blueprint.md` — full technical reference: data sources, field lists, the
  risk formula. Partly superseded: see Changed decisions.
- `reference/verify_pipeline.py` — the synthetic benchmark. Runnable, but it models
  biomass fires as point sources, so its numbers are upper bounds.

## Measured numbers

**Real data, verified industrial accidents: the headline** — 18 located accidents,
2013–2024, each replayed dry from 60 days before it (`reports/validation.md`):

- Per-fire recall **1 of 18** (Baghjan), against 0.12 expected by chance. Baghjan
  alerted from promotion, 21 days in, under Road A's class "forest".
- FIRMS saw only **6 of 18**. The other 12 burned between passes, under monsoon cloud, or
  inside buildings.
- Of the six seen:
  - three were Road B, below their plants' own baselines (Haldia, Bhilai, Tata Steel)
  - two were unmapped factories that Road A classed by land cover (Harda, Dombivli)
- Mean time to the first FIRMS detection **6.0 h** (n = 5).

**Real data, full archive: the registry.** VIIRS over India, 2012–2024, 11,485,898
detections, plus 1,068,405 MODIS, from `reports/stage3_registry.md`:

- Gate ≥ 3 months, ≥ 10 days, ≥ 3 years: **557** sources from 2,745 cells, the
  widest 8.0 km.
  - GIHS recall **83.4%**, with **86.2%** of sources on a GIHS site.
  - Paddy belt **0.014%** misrouted; FIRMS-static coverage **99.3%**.
  - Reliance 0.72 km, Nayara 0.25 km, HMEL 2.1 km.
- 12.5% of VIIRS detections land on a source; the rest go to Road A.
- Night persistence: median 0.23, and 0.94–0.96 at the big coal, steel and
  refinery complexes.
- One day-only source: a day-shift plant on a confirmed GIHS site.
- **27.0%** of all detections are at night.

**Model 1, real data** — 466 labelled sources, 5-fold GroupKFold on 2° blocks,
fingerprint features only, fixed hyperparameters (`reports/stage4_model1.md`):

- Accuracy **65.2%**, balanced accuracy **61.2%**, macro-F1 **0.62**.
- References: majority class 57.3% (33.3% balanced); lat/lon only 40.8% balanced;
  shuffled labels 33.3% balanced on average, none of 100 above 41.7% (p ≈ 0.01).
- Oil and gas: recall **58.8%**, precision **64.5%**. That clears the 50% usability
  bar, so it stays a class.
- Mining vs heavy industry is the main confusion. Contested labels explain part of
  it: 69.7% accuracy on uncontested sources, 52.1% on contested.
- No VNF temperature, the strongest discriminator in the literature. The published
  77% (Liu et al. 2018) had it.

**Real data, three years** — VIIRS over India, 2021–2023, 3,906,061 detections, from
`reports/stage1_5b_multiyear.md`:

- Raw DBSCAN put **379,180 detections in one cluster spanning the paddy belt**, and
  97% of the belt's detections inside a "source".
- Gated (≥ 3 months, ≥ 10 days, ≥ 2 years): **477** sources.
  - Paddy belt **0.01%** misrouted.
  - GIHS recall **78%**, with **86%** of sources on a GIHS site.
  - FIRMS-static coverage **99.1%**.
  - Reliance (84%), Nayara (90%) and HMEL (97%) all found.
- Registry sources labelled by OSM: mining 163, heavy industry 215, oil and gas 27,
  kiln 2.

**Real data, one year** — VIIRS over India, 2023, 1,170,878 detections, from
`reports/stage1_5_spike.md`:

- **29.0%** of detections are at night; **12.7%** carry FIRMS' `type=2` static flag.
- Raw DBSCAN: **34,797** "sources"; **60%** of the Punjab paddy belt inside one (73% in
  a ten-year stand-in); **47%** of all detections would skip Road A.
- Gated cells (≥ 6 months, ≥ 10 days): **443** sources, the widest 5.8 km.
  - Paddy belt **1.8%**, Jharia **98.0%**.
  - **99.2%** of FIRMS-static detections within 500 m of a source.
  - **81%** of sources on a GIHS site; GIHS recall **65%** of active objects.
- Memory: `ball_tree` ≈ `kd_tree` ≈ **1.32 GB**; gated **271 MB**.
- Jamnagar's largest flare group was detected on **112** days, not ~340 nights.

All real-data numbers are from reruns with EPSG:7755 distances (changed decision 17).

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
| 1 | DBSCAN on raw detections; `ball_tree` fixes memory; cluster state by state | 375 m cells, recurrence gate, DBSCAN with `sample_weight`, footprint cap; no state partitioning | Stages 1.5/1.5b, real data: on 2021–2023, raw DBSCAN merged the whole Punjab paddy belt into one 369 km cluster (97% of its detections). The synthetic benchmark hid this by modelling biomass fires as point sources. `ball_tree` and `kd_tree` used identical memory; the 330k-vs-158k comparison changed the data, not just the algorithm |
| 2 | Single-tier, two-consecutive-pass confirmation | Provisional alert on one extreme pass; confirmed alert on two consecutive breaching passes | Short blasts, the deadliest events, often burn out between passes |
| 3 | Road A sites repeating ~20 nights are promoted into the registry | Promoted sources stay provisional and keep alerting until they look like stable infrastructure; Baghjan 2020 is the regression test | A long-burning accident would become "normal" within a month; a competing team's repo documents exactly this failure at Baghjan |
| 4 | Baselines keyed `sensor \| daynight \| season` (per satellite) | Keyed `instrument \| daynight \| season` | S-NPP delivery ends 1 Nov 2026; NOAA-21 has little history; VIIRS units share one algorithm |
| 5 | "Model 1 must never see location features" | "No label may be built from a model input" — covers location, FIRMS `type`, season | The narrow rule missed other circular labels |
| 6 | Two XGBoost models: Model 1 plus a Road A classifier | One learned model; Road A is rules plus physics whose rule path is the UI's reason text | Road A's features are the layers its labels would be built from |
| 7 | Six classes (flare, furnace, kiln, mining, forest, cropland), but weak-label SQL that yields only industrial / forest / cropland | Three Model 1 classes chosen by a census of labelled registry sources: oil and gas 27, heavy industry 215, mining 163. Kiln dropped; forest and cropland handled by Road A | OSM has 22 flare tags in India; power and steel can't be separated by label; kiln labels at sources were wrong. Approved 2026-09-29 |
| 8 | `MOCK_MODE=1` by default; the mock is the demo dataset | Real data first; the mock shrinks to a test fixture; spikes injected into real histories | Ten years of FIRMS for India is free; a synthetic world hid problem 1 |
| 9 | Observability written per pass from swath and cloud mask | ERA5 cloud cover at overpass time via Open-Meteo, as a proxy | FIRMS publishes no swaths or cloud masks |
| 10 | VNF via a free account, required in live mode | Licence-gated and optional; the pipeline runs fully without it | EOG moved VNF behind a licence on 10 Jan 2025 |
| 11 | FSI alerts are "validated" forest fires | FIRMS points inside forest boundaries with partial state feedback — weak labels | That is what FSI's own FAQ says they are |
| 12 | Headline: source-level accuracy vs 77% | Headline: per-fire recall on the verified industrial fire set; 77% stays the reference for source sub-classification | Deliverable (i) is about fires; the verified set is the only non-circular number |
| 13 | Python 3.12; TimescaleDB required | Python 3.13; TimescaleDB optional | Windows wheels exist for 3.13 (not 3.14); native Postgres fallback while Docker is missing |
| 14 | Persistence = distinct detection dates / observed nights | Nights detected / nights observable | Day dates over a night denominator can exceed 1 |
| 15 | Online basemap | Offline basemap (PMTiles India extract) | Venue internet at the finale is unreliable |
| 16 | FIRMS `type`, `version` and product not stored | Stored as `firms_type`, `version`, `product`; SP supersedes NRT | Needed for evaluation, and to stop double counting |
| 17 | Metres from `x = lon * 111320 * cos(lat)`, each point's own latitude | EPSG:7755 through `firewatch.grid.to_metres`, the one implementation | The per-point cosine shears the plane: at Jharia, a 500 m north–south pair measured 586 m. A Stage 1 test caught it on 2026-09-29, and every spike was rerun with the fix |
| 18 | Observability from ERA5 cloud cover via Open-Meteo | NASA POWER daily cloud amount (CERES SYN1deg, 1°): one regional request per 10° tile-year, ~160 for India 2012–2024 | Open-Meteo's free tier counts each 14 days per location as a call: 13 years for a few hundred cells is ~135,000 calls against 10,000 a day. POWER is also satellite-observed rather than reanalysis; the price is 1° daily instead of 0.25° at the overpass |
| 19 | Recurrence gate in ≥ 2 years (approved on 2021–2023) | ≥ 3 years on the full 2012–2024 archive; months and days unchanged | The rule fixed before the full-archive sweep: the two-year gate missed the 85% on-GIHS floor (79.5%, 679 sources); the best passing setting was ≥ 3 years (557 sources, 83.4% recall, 86.2% on GIHS). Two years out of thirteen is a looser test than two out of three |
| 20 | recurrent_biomass added if ≥ ~30 unlabelled sources sit on cropland or forest | Also needs fewer than half the candidates on a GIHS-confirmed industrial site; GIHS decides only whether the class exists, never a label | Stage 4: 42 candidates, 81% on confirmed industry. The WorldCover rule was labelling unmapped mines and plants as biomass |
| 21 | "A source takes the first group in table order" (heavy industry before mining) | Most specific first, in the census's order: oil_gas > steel_cement > thermal_power > mining > kiln (no label) > industrial_other | The approved counts were computed in that order; generic `landuse=industrial` around coalfields would otherwise relabel coal fires as heavy industry |
| 22 | Baselines computed on detections (pixels) | Computed on passes: each sensor overpass's hottest pixel | A pixel baseline flags every large multi-pixel site on most passes, because the most extreme of k pixels is not one pixel |
| 23 | Anomaly test falls back to the source-wide baseline | Own-instrument baselines only; the source-wide one is descriptive | The pool is mostly VIIRS and MODIS sees only bigger fires: most 2023 confirmed false positives were MODIS passes judged against it. Removing it halved them (0.161% → 0.083% on 2023, 0.059% → 0.040% on 2024) |
| 24 | Promote a Road A site after ~20 nights | ≥ 10 distinct days *and* ≥ 50% of observable days since its first fire | Calendar days promoted Baghjan 66 days after it caught fire (the monsoon hid it); observable days, 21 |
| 25 | Extreme tier z > 7 and > 3× p99 | z > 7 and > 6× p99 (calibrated on 2023) | 3× p99 fired on 0.045% of real passes against a 0.01% target. The cost: single-pass recall falls from 62% to ~7% |
| 26 | Critical asset register: ~200 entries compiled by hand from PESO, CEA and MoPNG | Generated from OSM tags and WRI's power plant list (48,107 entries incl. 9 nuclear plants); manual rows survive reseeding | The user approved skipping the manual compilation for the prototype. Still static and never thermal, so the non-negotiable holds |
| 27 | Wind for the downwind term from Open-Meteo | NASA POWER daily U10M/V10M components | Same keyless service as the cloud record, one request per tile-year per component. POWER's daily `WD10M` comes back as nonsense, and a day's mean direction is meaningless where its mean vector is not |
| 28 | Exposure's asset term: a count of assets within 10 km | A criticality-weighted count | 41,000 of the 48,000 entries are mines and industrial estates; an unweighted count would rate every coal-belt fire as exposed as one beside a refinery |
