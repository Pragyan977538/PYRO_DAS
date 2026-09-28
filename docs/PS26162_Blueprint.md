# PS 26162 — Technical Blueprint

**AI-Based Detection and Classification of Industrial Fires and Persistent Thermal Sources Using NASA FIRMS, OSM & Satellite Data**

| | |
|---|---|
| Problem Statement ID | 26162 |
| Organisation | National Technical Research Organisation (NTRO) |
| Category | Software |
| Theme | Disaster Management |
| Event | Smart India Hackathon 2026 |
| Dataset given | `firms.modaps.eosdis.nasa.gov/map` |

> **Partly superseded (2026-09-29).** Several design decisions here were revised after a
> review and a real-data test: registry clustering, promotion, alert tiers, baseline keys,
> the Road A classifier (now rules plus physics), observability, VNF access and the
> headline metric. See `CLAUDE.md` → *Changed decisions*. Where the two disagree,
> `CLAUDE.md` wins. This document remains the reference for data sources, field lists and
> the risk formula.

---

## Table of contents

1. [The problem](#1-the-problem)
2. [The core idea](#2-the-core-idea)
3. [System architecture](#3-system-architecture)
4. [Data sources](#4-data-sources)
5. [Database schema](#5-database-schema)
6. [Module 1 — Ingestion](#6-module-1--ingestion)
7. [Module 2 — The registry](#7-module-2--the-registry-memory-framework)
8. [Module 3 — Inference, the three roads](#8-module-3--inference-the-three-roads)
9. [Module 4 — Event assembly](#9-module-4--event-assembly)
10. [Module 5 — Risk scoring](#10-module-5--risk-scoring)
11. [Module 6 — GIS frontend](#11-module-6--gis-frontend)
12. [Validation](#12-validation)
13. [Tech stack](#13-tech-stack)
14. [Build order](#14-build-order)
15. [Pitch notes](#15-pitch-notes)
16. [Known limitations](#16-known-limitations)
17. [Quick reference](#17-quick-reference)

---

## 1. The problem

### What the PS says

**Background.** Industrial facilities generate thermal signatures observable from space, but current satellite-based monitoring systems like NASA FIRMS cannot distinguish between different types of thermal anomalies. The challenge is to develop an AI-enabled geospatial system that integrates thermal data, land-cover information, industrial databases, and satellite imagery to automatically identify, classify, and monitor industrial fires and persistent thermal sources.

**Description.** Industrial facilities such as oil refineries, petrochemical complexes, thermal power plants, steel industries, mining areas, and LNG terminals generate thermal signatures observable from space. Accidental industrial fires, gas leaks, explosions, and abnormal thermal events pose significant risks to critical infrastructure, public safety, and the environment. Current satellite-based fire monitoring systems such as NASA FIRMS provide thermal anomaly detections but do not distinguish between industrial fires, gas flares, agricultural burning, mining activity, and wildfires.

**Expected deliverables.**

- **(i)** Classification and segregation of industrial fires from forest fires and other natural fires.
- **(ii)** GIS based solution for data storage, visualization of the output as an overlay over maps.

### What it actually means

FIRMS tells you a pixel is hot. It does not tell you what is hot.

Each detection is the **centre of a pixel** — roughly 1 km for MODIS, 375 m for VIIRS — flagged as containing one or more thermal anomalies. You cannot determine the exact location or size of the fire from it.

A single hot pixel could be:

- A real industrial accident — chemical plant fire, refinery blast, warehouse burning
- Routine intentional heat — gas flare, mineral processing plant, waste incinerator, steel furnace, brick kiln
- Agricultural stubble burning
- A forest fire
- A false positive — hot bare soil, sun glint, detector noise

FIRMS ships a crude `type` field (0 = presumed vegetation fire, 1 = active volcano, 2 = other static land source, 3 = offshore) which is nowhere near enough for an Indian industrial-safety use case.

### Scope discipline

The deliverables are narrow. **No alerting infrastructure, no prediction, no mobile app, no dispersion modelling is required.** Everything beyond (i) and (ii) is unscored. Build the deliverables first; treat event assembly and risk scoring as differentiation to be cut if time runs out.

---

## 2. The core idea

> **Every heat source has a thermal fingerprint — a signature of temperature, persistence and rhythm — and that fingerprint tells you what it is, without ever looking at a picture of it.**

A gas flare at Jamnagar, a blast furnace at Bhilai, a stubble fire in Punjab and a forest fire in Uttarakhand are indistinguishable as red dots. As **time series**, they are not remotely alike:

| Source | Temperature | Source size | Temporal behaviour |
|---|---|---|---|
| Gas flare | 1,600–1,900 K | Very small | ~340 nights/year, low variance |
| Steel / smelter | 1,000–1,500 K | Small–medium | Shift-cyclical, weekday pattern |
| Brick kiln | 1,000–1,300 K | Medium | Seasonal (dry months) |
| Stubble burning | 800–1,000 K | Large | Sharp Oct–Nov, Apr–May windows |
| Forest fire | 700–1,100 K | Large, spreading | Moves across pixels |
| **Industrial accident** | Variable | Variable | **A site that breaks its own pattern** |

FIRMS gives one frame of a movie and asks you to identify the film. We give the model the whole movie.

### The framing hook

India already solves this problem — badly, and in the opposite direction.

The **Forest Survey of India** runs an operational system, **FAST 3.0**, processing MODIS and SNPP-VIIRS detections received at Shadnagar via NRSC roughly six times in 24 hours. To stop factories triggering false forest-fire alarms, FSI applies a filter to mask out mining and industrial areas — and **that mask is built by manual digitisation**, hand-drawn polygons maintained as a filtering tool.

So the operational state of the art in India is a hand-drawn map, and the industrial detections it catches are discarded as noise.

> **Pitch line:** *FSI solves this today with a manually digitised mask and then deletes the industrial signal. We learn the mask from thermal physics, keep what FSI throws away, and rank it by risk.*

### Why this is not obvious

The obvious approach — "check if the hot pixel falls inside an OSM industrial polygon, label it industrial" — is a **geofence, not AI**. It fails three ways:

1. Cannot separate a normal flare from a refinery explosion (both inside the polygon)
2. Goes blind wherever OSM is incomplete — which is most Indian industrial estates
3. Has no answer for a fire *next to* a factory

Our approach inverts the dependency. We do not ask *where is this point?* We ask *what does this point do over time?* Location becomes one feature among many, so the system still works over unmapped terrain — which for NTRO is the actual point.

---

## 3. System architecture

```
                    ┌──────────────────────────────────┐
                    │        INGESTION LAYER           │
                    │  FIRMS · VNF · INSAT · OSM ·     │
                    │  FSI · WorldCover · WorldPop     │
                    └────────────────┬─────────────────┘
                                     ▼
                    ┌──────────────────────────────────┐
                    │   POSTGIS + TIMESCALEDB          │
                    │   detections · sources · events  │
                    │   observability · assets         │
                    └────────────────┬─────────────────┘
                                     ▼
              ┌──────────────────────┴──────────────────────┐
              │                                             │
    ┌─────────▼──────────┐                    ┌─────────────▼────────────┐
    │  OFFLINE (weekly)  │                    │   ONLINE (every 3 hrs)   │
    │  Build registry:   │───baselines───────▶│   Route each detection   │
    │  cluster →         │                    │   down Road 1 / 2 / 3    │
    │  fingerprint →     │◀──new persistent───│                          │
    │  classify          │      sources       │   ↓ event assembly       │
    └────────────────────┘                    │   ↓ risk scoring         │
                                              └─────────────┬────────────┘
                                                            ▼
                                        ┌────────────────────────────────┐
                                        │  FASTAPI  →  REACT + MAPLIBRE  │
                                        │  layers · time slider · detail │
                                        └────────────────────────────────┘
```

**Two clocks.** The registry is rebuilt weekly from the full archive (slow, expensive, offline). Classification runs every few hours on new detections (fast, cheap, online). They are connected by a feedback loop: Road 1 detections that persist for ~30 days get promoted into the registry.

---

## 4. Data sources

### 4.1 NASA FIRMS — primary input

| | |
|---|---|
| Get a key | `https://firms.modaps.eosdis.nasa.gov/api/map_key/` |
| API docs | `https://firms.modaps.eosdis.nasa.gov/api/area/` |
| Python tutorials | `https://firms.modaps.eosdis.nasa.gov/content/academy/data_api/firms_api_use.html` |
| Date coverage | `https://firms.modaps.eosdis.nasa.gov/api/data_availability/` |
| Interactive map | `https://firms.modaps.eosdis.nasa.gov/map/` |
| FAQ | `https://earthdata.nasa.gov/data/tools/firms/faq` |

**Endpoint format**

```
https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAY_RANGE}/{DATE}
```

- `BBOX` = `west,south,east,north`. India = `68,6,98,37`
- `DAY_RANGE` = 1 to 10 maximum per call
- `DATE` = `YYYY-MM-DD`, optional; omit for most recent
- Rate limit: 5,000 transactions per 10-minute interval; larger requests count as multiple transactions

**Sources**

| Source string | Use |
|---|---|
| `VIIRS_NOAA20_NRT` | Live, 375 m |
| `VIIRS_NOAA21_NRT` | Live, 375 m |
| `MODIS_NRT` | Live, 1 km |
| `VIIRS_SNPP_SP` | Archive, science quality |
| `MODIS_SP` | Archive, science quality |

Use the `_SP` (science processing) variants for building baselines — they are better calibrated than NRT.

**Columns returned**

| Field | Meaning |
|---|---|
| `latitude`, `longitude` | Pixel **centre**, not the fire location |
| `bright_ti4` / `brightness` | Brightness temperature, 4 µm channel (K) — the fire channel |
| `bright_ti5` / `bright_t31` | Brightness temperature, 11 µm channel (K) — background |
| `frp` | Fire Radiative Power, megawatts |
| `scan`, `track` | Pixel footprint dimensions — grows at scan edge |
| `acq_date`, `acq_time` | Acquisition, UTC |
| `confidence` | VIIRS: `l`/`n`/`h`. MODIS: 0–100 |
| `daynight` | `D` or `N` |
| `satellite`, `instrument` | Sensor identity |
| `version` | Collection / processing version |
| `type` | MODIS only: 0 vegetation, 1 volcano, 2 static land, 3 offshore |

> **Critical operational note.** Suomi NPP data delivery ceases **1 November 2026**; transition to NOAA-20 and NOAA-21 products. Build ingestion sensor-agnostic. State this on stage — it demonstrates you read primary documentation.

### 4.2 VIIRS Nightfire (VNF) — the differentiator

| | |
|---|---|
| Product page | `https://eogdata.mines.edu/products/vnf/` |
| Algorithm background | `https://payneinstitute.mines.edu/eog/viirs-nightfire-vnf/` |
| Global flare explorer | `https://eogdata.mines.edu/products/vnf/global_gas_flare.html` |
| Reference implementation | `https://github.com/flaringmonitor/viirs-flare-code` |

Licence required since 10 January 2025: academic use is a free one-year licence, but it
needs a signed agreement and EOG's approval. Interim access downloads only the reduced
"ezCSV" files. **Treat VNF as optional enrichment — the pipeline must run without it.**

**Why this matters more than anything else in the pipeline.** VNF uses nine night-collected channels — with M10, M11, M12 and M13 used to detect combustion sources — and calculates **temperature, source size and radiant heat using physical laws** (Planck curve fitting). With sunlight eliminated at night, the recorded signal is fully attributable to the combustion source.

FIRMS gives you *how bright*. VNF gives you *how hot* — and temperature is the physical property that separates a 1,750 K flare from a 900 K wildfire.

**Fields to extract** (verify against the actual CSV header — names have shifted across versions):

| Field | Meaning |
|---|---|
| `Temp_BB` | Blackbody temperature, Kelvin — **the gold** |
| `Area_BB` | Source area, m² — sub-pixel size estimate |
| `RH`, `RHi` | Radiant heat, MW |
| `Lat_GMTCO`, `Lon_GMTCO` | Position |
| `Cloud_Mask` | Obscuration flag |
| `Date_Mscan` | Scan timestamp |

Night-only by design.

**Proven precedent.** EOG's global flare survey identified flaring sites from VNF heat anomalies, then **separated high-temperature biomass burning from low-temperature gas flaring based on temperature and persistence**. A published study built an inventory of **15,199 industrial heat sources** — 49.52% of all higher-confidence nighttime thermal anomalies — using temperature-histogram patterns and an object-oriented method, reaching **~77% sub-category accuracy** with only 1.43% misclassified as biomass burning or volcanoes.

**That 77% is your benchmark. Beat it and say so.**

### 4.3 INSAT-3D / 3DR / 3DS — Indian high-cadence data

| | |
|---|---|
| Data portal | `https://www.mosdac.gov.in` |
| Payload specs | `https://www.mosdac.gov.in/insat-3d-payloads` |

Registration required.

Geostationary at 74° E. Imager bands: visible, SWIR 1.55–1.70 µm, **MIR 3.80–4.00 µm**, TIR-1 10.2–11.2 µm, TIR-2 11.5–12.5 µm. Infrared resolution 4 km, visible/SWIR 1 km. Full-disc observation every **15–30 minutes**.

SAC has published a multi-channel contextual fire detection algorithm for this platform. The INSAT-3DS study reports **45.31% probability of detection** against high-confidence MODIS fires with **73.34% precision**, performing better on higher-FRP fires, and noting that the coarse 4 km resolution may miss small scattered fires.

**Role in the system:** not for finding small fires — VIIRS does that at 375 m. INSAT provides **~48 looks per day at large events**, which is exactly the regime where it performs best, and its cadence lets you see through gaps between moving clouds.

**Strategic point for NTRO:** Indian satellite, Indian ground segment, Indian portal. Every module that runs without NASA survives a data-access disruption. Put this on a slide.

### 4.4 OpenStreetMap — industrial footprints

| | |
|---|---|
| Prototype queries | `https://overpass-turbo.eu` |
| API endpoint | `https://overpass-api.de/api/interpreter` |

```
[out:json][timeout:600];
area["ISO3166-2"="IN-MH"]->.a;
(
  way(area.a)["landuse"="industrial"];
  way(area.a)["landuse"="quarry"];
  way(area.a)["man_made"="works"];
  way(area.a)["man_made"="flare"];
  way(area.a)["power"="plant"];
  way(area.a)["landuse"="forest"];
  relation(area.a)["landuse"="industrial"];
);
out geom;
```

Query **state by state** — an all-India request will time out.

### 4.5 Forest Survey of India — free forest fire labels

| | |
|---|---|
| Alert dashboard | `https://fsiforestfire.gov.in` |
| Van Agni / focus areas | `https://fsi.nic.in/focus-areas` |

Each point is a FIRMS detection that falls inside a forest boundary; FSI's own FAQ says only hotspots within the forest area are disseminated, and state forest departments send feedback on only some of them. **Treat them as weak forest labels, not ground truth.** Alerts are disseminated as SMS, KML and CSV.

FSI's operational conventions worth borrowing (and citing):

- **Large fire definition:** three proximate VIIRS pixels
- **Tracking:** monitor across subsequent passes within a buffer while active
- **Re-ignition window:** keep scanning for **three days** after a fire goes quiet

### 4.6 Supporting layers

| Data | Source | Use |
|---|---|---|
| Land cover, 10 m | `https://esa-worldcover.org` | Forest / cropland / built-up classification |
| Population, 100 m | `https://www.worldpop.org/geodata` | Exposure term in risk score |
| Wind (free, no key) | `https://open-meteo.com` | Downwind smoke sector |
| Sentinel-2 imagery | `https://dataspace.copernicus.eu` | Optional visual confirmation (SWIR B12/B11/B8A) |
| Google Earth Engine | `https://earthengine.google.com` | Alternative imagery access without bulk download |

### 4.7 Registration checklist — do all of these on day one

- [ ] FIRMS MAP_KEY (emailed instantly)
- [ ] EOG account for VNF
- [ ] MOSDAC account for INSAT-3D
- [ ] Copernicus Data Space or GEE account (optional)

---

## 5. Database schema

PostgreSQL 15+ with PostGIS 3.3+ and TimescaleDB.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────
-- Raw hot pixels, one row per satellite detection
-- ─────────────────────────────────────────────────────────
CREATE TABLE detections (
  detection_id   BIGSERIAL,
  geom           GEOMETRY(Point, 4326) NOT NULL,
  acq_datetime   TIMESTAMPTZ NOT NULL,
  sensor         TEXT NOT NULL,           -- VIIRS_NOAA20 | MODIS | INSAT3D
  daynight       CHAR(1),
  frp            REAL,
  bright_ti4     REAL,
  bright_ti5     REAL,
  scan           REAL,
  track          REAL,
  confidence     TEXT,
  -- joined from VNF
  vnf_temp_k     REAL,
  vnf_area_m2    REAL,
  vnf_rh_mw      REAL,
  -- assigned downstream
  source_id      INT,
  event_id       INT,
  road           SMALLINT,                -- 1 | 2 | 3
  pred_class     TEXT,
  pred_conf      REAL,
  PRIMARY KEY (detection_id, acq_datetime)
);
SELECT create_hypertable('detections', 'acq_datetime');
CREATE INDEX ON detections USING GIST (geom);
CREATE INDEX ON detections (source_id);

-- ─────────────────────────────────────────────────────────
-- The registry — persistent thermal sources
-- ─────────────────────────────────────────────────────────
CREATE TABLE sources (
  source_id      SERIAL PRIMARY KEY,
  geom           GEOMETRY(Point, 4326) NOT NULL,
  footprint      GEOMETRY(Polygon, 4326),
  n_detections   INT,
  first_seen     DATE,
  last_seen      DATE,
  class          TEXT,      -- flare|furnace|kiln|mining|forest|cropland|unknown
  class_conf     REAL,
  osm_ref        BIGINT,
  osm_name       TEXT,
  fingerprint    JSONB,
  baselines      JSONB,
  provisional    BOOLEAN DEFAULT FALSE,   -- promoted from Road 1, still on probation
  updated_at     TIMESTAMPTZ
);
CREATE INDEX ON sources USING GIST (geom);
CREATE INDEX ON sources USING GIST (footprint);

-- ─────────────────────────────────────────────────────────
-- Events — one row per real-world incident
-- ─────────────────────────────────────────────────────────
CREATE TABLE events (
  event_id       SERIAL PRIMARY KEY,
  source_id      INT REFERENCES sources(source_id),   -- NULL for Road 1
  footprint      GEOMETRY(Polygon, 4326),
  centroid       GEOMETRY(Point, 4326),
  first_seen     TIMESTAMPTZ,
  last_seen      TIMESTAMPTZ,
  status         TEXT,      -- active | dormant | closed
  peak_frp       REAL,
  n_detections   INT,
  event_class    TEXT,
  risk_score     REAL,
  risk_breakdown JSONB
);
CREATE INDEX ON events USING GIST (footprint);

-- ─────────────────────────────────────────────────────────
-- Observability — were we even looking?
-- ─────────────────────────────────────────────────────────
CREATE TABLE observability (
  cell_id      BIGINT,        -- H3 or grid index
  obs_date     DATE,
  sensor       TEXT,
  clear_looks  SMALLINT,
  total_passes SMALLINT,
  PRIMARY KEY (cell_id, obs_date, sensor)
);

-- ─────────────────────────────────────────────────────────
-- Static critical asset register — NOT derived from satellite
-- ─────────────────────────────────────────────────────────
CREATE TABLE critical_assets (
  asset_id     SERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  geom         GEOMETRY(Point, 4326) NOT NULL,
  asset_type   TEXT NOT NULL,
  criticality  REAL NOT NULL CHECK (criticality BETWEEN 0 AND 1),
  operator     TEXT,
  source_ref   TEXT
);
CREATE INDEX ON critical_assets USING GIST (geom);

-- ─────────────────────────────────────────────────────────
-- Context layers
-- ─────────────────────────────────────────────────────────
CREATE TABLE osm_industrial (
  osm_id BIGINT PRIMARY KEY, name TEXT, tag TEXT,
  geom GEOMETRY(MultiPolygon, 4326)
);
CREATE INDEX ON osm_industrial USING GIST (geom);

CREATE TABLE forest_boundary (
  id SERIAL PRIMARY KEY, geom GEOMETRY(MultiPolygon, 4326)
);
CREATE INDEX ON forest_boundary USING GIST (geom);
```

**Projection note.** Store everything in EPSG:4326. For distance and clustering computations, reproject to **EPSG:7755** (India-wide, metres) or the appropriate UTM zone. Never run DBSCAN on raw lat/lon — the eps parameter becomes meaningless.

---

## 6. Module 1 — Ingestion

### 6.1 FIRMS backfill

```python
import pandas as pd, time
from datetime import date, timedelta

MAP_KEY    = "your_key_here"
INDIA_BBOX = "68,6,98,37"
BASE       = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

def fetch(source: str, start: date, days: int = 10) -> pd.DataFrame:
    url = f"{BASE}/{MAP_KEY}/{source}/{INDIA_BBOX}/{days}/{start:%Y-%m-%d}"
    return pd.read_csv(url)

def backfill(source: str, start: date, end: date) -> pd.DataFrame:
    frames, cur = [], start
    while cur < end:
        try:
            frames.append(fetch(source, cur, 10))
        except Exception as e:
            print(f"skip {cur}: {e}")
        cur += timedelta(days=10)
        time.sleep(1.5)          # stay well under the rate limit
    return pd.concat(frames, ignore_index=True)
```

Backfill target: **2015 to present** for India, all available sensors. Expect a few million rows — trivial for PostGIS.

### 6.2 VNF join

VNF is night-only and independently geolocated, so join spatially and temporally:

```sql
UPDATE detections d
SET vnf_temp_k  = v.temp_bb,
    vnf_area_m2 = v.area_bb,
    vnf_rh_mw   = v.rh
FROM vnf_raw v
WHERE d.daynight = 'N'
  AND ST_DWithin(d.geom::geography, v.geom::geography, 750)
  AND ABS(EXTRACT(EPOCH FROM (d.acq_datetime - v.scan_time))) < 1800;
```

750 m tolerance covers VIIRS geolocation uncertainty at 375 m pixel size. 30-minute window covers scan timing differences.

Coverage will be partial — VNF only catches sources hot enough for Planck fitting, and only at night. **Design the classifier to work with `vnf_temp_k` missing**, using it as a strong feature when present rather than a hard requirement.

### 6.3 Context layers

Load once:

```bash
# OSM, state by state
python fetch_overpass.py --state MH --out osm_mh.geojson
ogr2ogr -f PostgreSQL PG:"dbname=fires" osm_mh.geojson -nln osm_industrial -append

# Land cover
gdalwarp -t_srs EPSG:4326 -tr 0.0001 0.0001 worldcover_in.tif lc.tif
raster2pgsql -s 4326 -I -C lc.tif landcover | psql fires

# Population
raster2pgsql -s 4326 -I -C ind_ppp_2020.tif population | psql fires
```

### 6.4 Observability writer

On **every** pass, whether or not anything burned:

```python
def record_observability(pass_footprint, sensor, cloud_mask, obs_date):
    cells = h3_cells_intersecting(pass_footprint, resolution=6)
    for c in cells:
        clear = 0 if cloud_mask.covers(c) else 1
        upsert_observability(c, obs_date, sensor, clear)
```

Without this table, "no detection" silently reads as "no fire" when it means "we were blind."

### 6.5 Scheduling

| Job | Cadence |
|---|---|
| FIRMS NRT pull | Every 3 hours |
| VNF nightly CSV | Daily, 06:00 IST |
| INSAT-3D pull | Every 30 min (optional) |
| Registry rebuild | Weekly |
| OSM refresh | Monthly |

Use APScheduler or Prefect. Do not build a Kubernetes cron system for a hackathon.

---

## 7. Module 2 — The registry (memory framework)

This is the core intellectual property. It is also a **standalone deliverable**: a national registry of India's persistent thermal sources, which does not currently exist.

### 7.1 Clustering

```python
import numpy as np, geopandas as gpd
from sklearn.cluster import DBSCAN

gdf = gpd.read_postgis("SELECT * FROM detections", con, geom_col="geom")
gdf = gdf.to_crs(7755)                      # metres

coords = np.c_[gdf.geometry.x, gdf.geometry.y]
db = DBSCAN(eps=500, min_samples=5, n_jobs=-1).fit(coords)
gdf["cluster"] = db.labels_
```

**Parameter rationale:**

- `eps = 500 m` — VIIRS pixels are 375 m, so anything closer is plausibly the same physical source
- `min_samples = 5` — filters one-off noise
- Label `-1` = unclustered. **These are your Road 1 candidates by definition.**

### 7.2 The fingerprint

```python
from scipy.stats import entropy

def fingerprint(g, observed_nights):
    frp = g.frp.dropna()
    months = g.acq_datetime.dt.month.value_counts(normalize=True)
    return {
        "temp_p50":      float(g.vnf_temp_k.median()),
        "temp_p90":      float(g.vnf_temp_k.quantile(0.90)),
        "temp_iqr":      float(g.vnf_temp_k.quantile(.75) - g.vnf_temp_k.quantile(.25)),
        "area_p50":      float(g.vnf_area_m2.median()),
        "frp_p50":       float(frp.median()),
        "frp_p95":       float(frp.quantile(0.95)),
        "frp_mad":       float((frp - frp.median()).abs().median()),
        "ti4_ti5_p50":   float((g.bright_ti4 - g.bright_ti5).median()),
        "persistence":   g.acq_datetime.dt.date.nunique() / max(observed_nights, 1),
        "night_ratio":   float((g.daynight == "N").mean()),
        "month_entropy": float(entropy(months)),
        "n_years":       int(g.acq_datetime.dt.year.nunique()),
        "n_detections":  int(len(g)),
    }
```

**The two features that do most of the work:**

- **`month_entropy`** — a flare burns every month (high entropy, near uniform); stubble burns in two months (low entropy, spiky). This single number separates industrial from agricultural almost by itself.
- **`persistence`** — divide by nights you *actually observed*, not calendar nights. Otherwise monsoon cloud makes every source look intermittent.

### 7.3 Conditioned baselines

**One baseline per cluster is wrong.** Store nested, keyed by condition:

```json
{
  "VIIRS_NOAA20|N|winter": {"med": 42.1, "mad":  8.3, "p99":  96.0, "n": 340},
  "VIIRS_NOAA20|D|winter": {"med": 61.7, "mad": 14.2, "p99": 140.0, "n": 298},
  "MODIS|N|monsoon":       {"med": 38.0, "mad": 11.0, "p99":  88.0, "n":  41}
}
```

Split by **sensor × day/night × season**. The same flare observed by different sensors at different geometries yields wildly different FRP. Without this split you will flag orbital mechanics as an industrial accident.

**Use median and MAD, never mean and standard deviation.** FRP is heavily right-skewed; one past explosion permanently inflates σ and your threshold stops firing.

Require `n ≥ 30` per bucket; below that, fall back to the parent bucket, then the cluster-wide baseline.

### 7.4 Free labels

```sql
-- Industrial, from OSM
UPDATE sources s SET class = 'industrial_osm'
FROM osm_industrial o
WHERE ST_DWithin(s.geom::geography, o.geom::geography, 300);

-- Forest, from FSI alerts (weak: FIRMS points inside forest boundaries)
UPDATE sources s SET class = 'forest'
FROM fsi_alerts f
WHERE ST_DWithin(s.geom::geography, f.geom::geography, 500);

-- Cropland, from WorldCover
UPDATE sources s SET class = 'cropland'
WHERE landcover_at(s.geom) = 40 AND s.class IS NULL;
```

This yields a few thousand labelled clusters without a single hand annotation.

### 7.5 Source classifier

```python
from xgboost import XGBClassifier
from sklearn.model_selection import GroupKFold

FEATURES = ["temp_p50","temp_p90","temp_iqr","area_p50","frp_p50","frp_p95",
            "frp_mad","ti4_ti5_p50","persistence","night_ratio",
            "month_entropy","n_years","dist_industrial","dist_forest",
            "landcover_class"]

clf = XGBClassifier(
    n_estimators=400, max_depth=6, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    objective="multi:softprob", eval_metric="mlogloss")

# Group by state so the model can't memorise one region
cv = GroupKFold(n_splits=5)
```

**Why XGBoost, not a neural network.** The data is tabular, ~15 features, a few thousand rows. Deep learning offers nothing here and costs interpretability. `clf.feature_importances_` is a slide — you can stand up and say "temperature and seasonality carry 60% of the decision," which is a far stronger answer than "the network learned it."

### 7.6 The feedback loop

The registry is **not static**. Road 1 detections that repeat get promoted:

```python
def promote_provisional(days=30, min_nights=20):
    """A Road 1 site that keeps burning is infrastructure, not an accident."""
    candidates = query("""
      SELECT ST_ClusterDBSCAN(geom, 500, 5) OVER () AS c, *
      FROM detections
      WHERE road = 1 AND acq_datetime > now() - interval '%s days'
    """, days)
    for cluster in candidates.groupby("c"):
        if cluster.acq_datetime.dt.date.nunique() >= min_nights:
            insert_source(cluster, provisional=True)
```

An accident burns and stops forever. A newly commissioned flare burns and keeps burning. Time resolves the ambiguity that day-one classification cannot.

---

## 8. Module 3 — Inference, the three roads

### 8.1 The router

```python
def route(detection):
    src = nearest_source(detection.geom, max_dist=500)

    if src is None:
        return road_1(detection)                    # no history

    baseline = pick_baseline(src, detection.sensor,
                             detection.daynight, season(detection))

    if is_anomaly(detection.frp, baseline):
        return road_3(detection, src, baseline)     # broke its own pattern
    return road_2(detection, src)                   # behaving normally
```

One routing question — *does this pixel have history?* — then one sub-question. That is the entire top-level logic.

### 8.2 Road 2 — normal operation

**A location with repeated fire history is not a fire. It is infrastructure.**

If you alert on these, you generate roughly fifteen thousand "fire alerts" per night across India, every one a factory operating as designed. This is exactly the failure FSI hit and solved with their mask.

Road 2 output: **map layer and catalogue entry. No alert.**

It is still a deliverable — the PS title asks for persistent thermal sources to be identified and monitored, not just fires. And Road 2 is what makes Road 3 possible: watching normal behaviour for years is how you learn what abnormal looks like.

### 8.3 Road 3 — anomaly at a known source

Pure statistics, no training required:

```python
def is_anomaly(frp, baseline):
    if baseline is None or baseline["n"] < 30:
        return False
    z = 0.6745 * (frp - baseline["med"]) / max(baseline["mad"], 1e-6)
    return z > 3.5 and frp > baseline["p99"] * 1.5
```

- `0.6745` converts MAD to a σ-equivalent so 3.5 means roughly what you expect
- Requiring **both** a robust z-score and a percentile breach kills most single-pass noise
- Add a persistence requirement: flag only if **two consecutive passes** breach. A single hot pass at a refinery is usually a flaring event during a plant upset, not a fire.

**Being untrained is a feature.** You have no labelled refinery explosions. Say it plainly: *"Road 3 needs no training data because it learns each site's own normal."*

### 8.4 Road 1 — new ignition

The case a purely statistical design cannot handle. A warehouse fire's ten-year history is: nothing, nothing, nothing. The deviation from a baseline of zero is undefined.

Road 1 asks a **categorical** question instead: *given everything around this pixel, what could plausibly be burning here?*

**Step 1 — is it real?**

- Drop low-confidence detections using the FIRMS `confidence` field
- Mask water and barren land (INSAT-3DS uses MODIS land cover for exactly this, plus a glint-angle rejection scheme)
- Reject daytime detections over water and known metal-roof zones
- Prefer detections confirmed on two overpasses

Measure and report your discard rate. You will throw away real fires here.

**Step 2 — where is it?**

- Land cover: forest / cropland / barren / urban / water
- **Distance** to nearest industrial polygon — continuous, not a binary inside/outside test
- Season and state

> **Why distance, not containment.** Real industrial fires very often start at the *edge* — scrap yard, storage shed, tanker parking — just outside whatever polygon OSM drew. A binary test misses these. A continuous distance feature lets the classifier learn its own cutoff.

**Step 3 — how hot, how big?**

A brand-new gas flare and a warehouse fire have identical history: none. Both are Road 1. Location cannot separate them — both are industrial. **Temperature can:**

- Very hot (>1,500 K), tiny source → probably a **newly commissioned flare**
- Cooler (~1,000 K), large source → probably an **accident**

**Classifier**

```python
ROAD1_FEATURES = [
  "vnf_temp_k", "vnf_area_m2", "frp", "bright_ti4", "bright_ti5",
  "ti4_minus_ti5", "dist_industrial", "dist_forest", "dist_quarry",
  "landcover_class", "month", "state_code", "daynight",
  "n_hot_neighbours_this_pass",
]

clf1 = XGBClassifier(
    n_estimators=300, max_depth=5,
    scale_pos_weight=compute_class_weights(y),   # MANDATORY
)
```

**Class imbalance is the trap here.** You will have thousands of labelled forest and agricultural fires and maybe forty industrial accidents. Without class weighting the model learns to never predict "industrial" because that maximises accuracy. **Report recall on the industrial class specifically**, not overall accuracy — which will look excellent and mean nothing.

**Output priority**

| Class | Volume | Handling |
|---|---|---|
| Agricultural | Overwhelming majority | Suppress into a summary layer |
| Forest | High, seasonal | Hand cleanly to FSI, don't duplicate their work |
| **Industrial** | Small | **Route straight to risk scoring** |
| New persistent source | Rare | Provisional; watch 30 days |

---

## 9. Module 4 — Event assembly

One fire must produce one row, not forty.

```python
SPATIAL_LINK_M   = 1000      # same pass, within 1 km → one cluster
TEMPORAL_LINK_M  = 2000      # across passes, within 2 km → same event
TEMPORAL_LINK_H  = 24
DORMANT_H        = 72        # FSI's re-ignition window

def assemble(detections_this_pass):
    clusters = spatial_cluster(detections_this_pass, SPATIAL_LINK_M)
    for c in clusters:
        ev = find_active_event(c.centroid, TEMPORAL_LINK_M, TEMPORAL_LINK_H)
        if ev:
            ev.append(c)
        else:
            ev = open_event(c)
    close_events_idle_longer_than(DORMANT_H)
```

FSI's operational conventions are the template, and citing them makes your parameter choices defensible rather than arbitrary: three proximate VIIRS pixels constitute a large fire; monitor across subsequent passes while active; keep scanning three days after it goes quiet to catch dormant re-ignition.

---

## 10. Module 5 — Risk scoring

### 10.1 The formula

```
Risk = 100 × H^0.40 × E^0.35 × V^0.25
```

with each of H, E, V normalised to [0.05, 1].

**Multiplicative, not additive — this is the key design choice.** A massive fire in an empty desert next to nothing valuable should score *low*. Additive scoring would hand it a high score off the hazard term alone, which is wrong. Multiplication means all three conditions must hold for risk to be high.

The exponents weight terms without breaking that property. The 0.05 floor stops any single missing input from zeroing the whole score.

### 10.2 H — hazard (how violent)

```
H = 0.50·f̂ + 0.30·n̂ + 0.20·g
```

| Term | Definition |
|---|---|
| `f̂` | Percentile rank of log(FRP) against the national historical distribution |
| `n̂` | Normalised contiguous hot-pixel count; FSI's threshold of 3 pixels as the midpoint |
| `g` | Growth flag — 1 if FRP rose since the last pass, 0.5 if flat, 0 if falling |

**Percentile rank, not min-max.** One 5,000 MW outlier would otherwise squash every normal fire into the bottom 2%.

A shrinking fire is a fire being handled — hence the growth term.

### 10.3 E — exposure (who and what is nearby)

```
E = 0.45·p̂₅ + 0.30·p̂_wind + 0.25·â
```

| Term | Definition |
|---|---|
| `p̂₅` | Population within 5 km (WorldPop), log-scaled then percentile-ranked |
| `p̂_wind` | Population in a 30° downwind sector out to 10 km, using Open-Meteo wind direction |
| `â` | Count of other critical assets within 10 km — cascading failure risk |

The downwind term is the one that makes evaluators sit up. **Smoke does not spread in a circle.** A fire upwind of a town is a different problem from the same fire downwind of it.

### 10.4 V — vulnerability (what is burning)

**This is a lookup, not a computation.**

| Asset type | Criticality |
|---|---|
| LNG / LPG terminal | 1.00 |
| Oil refinery | 0.95 |
| Petrochemical complex | 0.90 |
| Chemical / pharmaceutical plant | 0.80 |
| Thermal power station | 0.70 |
| Fertiliser plant | 0.70 |
| Major port | 0.60 |
| Steel plant / smelter | 0.50 |
| General industrial estate | 0.30 |
| Warehouse / godown | 0.25 |

```
V = criticality × exp(−distance_m / 2000)
```

using the nearest asset within 2 km.

> **Why this must be a static table.** Nuclear plants dump waste heat into cooling water at 30–40 °C — far below the sub-pixel high-temperature signature satellites detect. **They are thermally invisible until something actually burns.** The same applies to ammunition depots, LPG bottling plants, and cold storage: high consequence, no thermal signature in normal operation. If your vulnerability term is derived from thermal history, your highest-criticality assets score zero.

**Populating the register:** PESO licensed premises lists, the CEA thermal power station directory, MoPNG's refinery list, OSM `man_made=works` with operator names. **Two hundred well-chosen entries covers most of what NTRO cares about.** Exhaustiveness is not required for a hackathon.

### 10.5 Emit the decomposition, never a bare number

```json
{
  "risk": 78,
  "hazard": {
    "score": 0.81, "frp_percentile": 0.94, "pixels": 6, "growing": true
  },
  "exposure": {
    "score": 0.72, "pop_5km": 41200, "pop_downwind": 18400, "assets_10km": 3
  },
  "vulnerability": {
    "score": 0.88, "nearest": "Panipat Refinery", "distance_m": 240
  }
}
```

Every judge's first question is "why 78?" Having the answer already on screen ends that line of questioning in your favour.

If someone pushes on the exact exponents, the honest answer is that they are a starting point calibrated against your validation events and tunable per user — which is true, and far better than pretending they are derived from first principles.

---

## 11. Module 6 — GIS frontend

Deliverable (ii) is deliberately modest. Do not over-build it.

### Required

- **Toggleable layer per class** — "segregation" implies separable streams, not just a label column
- **Persistent source overlay** — your learned registry, rendered against a satellite basemap. This is your FSI-mask analogue and it looks impressive for almost no effort
- **Click-through detail panel** — class, confidence, VNF temperature, FRP, baseline comparison, nearest named facility, and **the reason for the call**
- **Time slider** across the archive
- **Observability confidence overlay** — where were we blind

### Stack

- React + **MapLibre GL JS** (open source; avoids Mapbox billing)
- Deck.gl if point counts get large
- Recharts for the FRP time series in the detail panel
- FastAPI serving GeoJSON and vector tiles from PostGIS

### API surface

```
GET  /api/detections?bbox=&start=&end=&class=
GET  /api/sources?bbox=&class=
GET  /api/events?status=active&min_risk=50
GET  /api/events/{id}                    → full detail + risk breakdown
GET  /api/sources/{id}/timeseries        → FRP history for the chart
GET  /api/observability?bbox=&date=
```

Serve tiles with `ST_AsMVT` for anything above ~10,000 points.

---

## 12. Validation

**Nothing else in this document matters if you cannot show it works.**

### Held-out design

Split **by state**, not randomly. Random splits let the model memorise specific facilities and inflate your scores. Train on 20 states, test on the rest.

### Ground truth

| Class | Source | Expected count |
|---|---|---|
| Forest fire | FSI alerts (weak labels) + WorldCover tree cover | Thousands |
| Agricultural | WorldCover cropland + Oct–Nov / Apr–May | Thousands |
| Industrial persistent | OSM + EOG global flare list | Hundreds |
| **Industrial fire** | **Hand-built from news archives** | **20–40** |

For the industrial fire set: find reported Indian industrial fires, note date and location, pull FIRMS detections in that bounding box on that date, verify by eye. Twenty to forty well-verified events is enough to demonstrate the method. **Say honestly that this is the class where more labelled data would help most.**

### Metrics to report

- **Confusion matrix**, all classes
- **Precision and recall per class** — especially recall on industrial
- **Mean time-to-detect** against known event start times
- **False positive rate** on the Road 2 stream (the thing FSI cares about)
- **Discard rate** at Road 1 Step 1

### Benchmark

Published VIIRS Nightfire industrial-heat-source work reached **~77% sub-category classification accuracy**. Cite it, report where you land, and be honest if you land below.

---

## 13. Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11 | Ecosystem |
| Ingestion | `requests`, `pandas`, APScheduler | No infra overhead |
| Geospatial | GeoPandas, Shapely, Rasterio, pyproj | Standard |
| Database | **PostgreSQL + PostGIS + TimescaleDB** | Spatial joins and time series in one place |
| ML | scikit-learn (DBSCAN), **XGBoost** | Tabular data; interpretable |
| Backend | **FastAPI** | Async, auto-docs, fits the Python stack |
| Queue | Celery + Redis | Only if imagery fetch is added |
| Frontend | React + **MapLibre GL JS** + Recharts | Open source, no billing |
| Imagery (optional) | Google Earth Engine Python API | Avoids terabyte downloads |
| Deploy | Docker Compose → Render or Railway | Do not spend hackathon hours on Kubernetes |

### Explicitly not using

- **No LLM as the core classifier.** A tuned XGBoost on thermal features will outperform it and is defensible under questioning. If a GenAI component is wanted, scope it narrowly — natural-language incident summaries, or a Hindi/Marathi map query interface.
- **No deep learning on tabular features.** Fifteen features and a few thousand rows. Gradient boosting wins and gives you feature importances.

---

## 14. Build order

Build in this sequence. Cut from the bottom.

| # | Task | Deliverable | Priority |
|---|---|---|---|
| 1 | API registrations (FIRMS, EOG, MOSDAC) | — | Day 1 |
| 2 | FIRMS backfill → PostGIS | — | Critical |
| 3 | OSM, land cover, FSI layers loaded | — | Critical |
| 4 | DBSCAN clustering + fingerprints → **registry** | (i) | Critical |
| 5 | VNF temperature join | (i) | Critical |
| 6 | Source classifier + confusion matrix | (i) | **Critical** |
| 7 | Three-road router + Road 1 classifier | (i) | Critical |
| 8 | MapLibre frontend, layers, detail panel | (ii) | Critical |
| 9 | Time slider, observability layer | (ii) | High |
| 10 | Event assembly | — | Differentiator |
| 11 | Risk scoring | — | Differentiator |
| 12 | Sentinel-2 visual confirmation | — | Cut first |

**Items 1–9 are the graded deliverables. Items 10–11 are what wins. Item 12 is decoration.**

If you fall behind: **cut 12, then 11, then 10.** Never cut 6 — a strong classifier with a plain map beats a beautiful map over a threshold rule, every time.

---

## 15. Pitch notes

### Three-minute demo narrative

1. Open on the raw FIRMS map of India — an undifferentiated red mess
2. State that FSI currently handles this with a manually digitised mask, and discards the industrial half
3. Toggle classification on — the mess separates into coloured layers
4. Click a persistent flare at Jamnagar: show its ten-year FRP baseline and its VNF temperature near 1,750 K
5. Click a known industrial fire date at the same site: show it spiking far outside its own envelope
6. Close on the confusion matrix

### Questions you will be asked

**"Isn't this just a lookup against a map of factories?"**
No. That fails on the case that matters most — a fire at a facility during normal operations, because both are inside the same polygon. Our discriminator is temporal deviation from the site's own baseline, plus physical temperature from VNF.

**"What if the fire is somewhere with no data?"**
The fingerprint is computed from that pixel's own detection history, not from an external database. An unmapped site builds a baseline after a few weeks of observation. That is precisely why this works beyond OSM coverage.

**"Why won't clouds break it?"**
They will, and we say so. Thermal infrared is blocked by cloud. We surface observation gaps as a confidence layer instead of pretending coverage is continuous — which is more than the current operational systems do. INSAT-3D's 15–30 minute cadence gives ~48 looks a day to catch gaps between moving clouds.

**"Why not deep learning?"**
Fifteen tabular features, a few thousand rows. Gradient boosting outperforms and gives interpretable feature importances — which matters for a system that has to justify an alert.

### Slides

1. **The red dot problem** — one FIRMS screenshot. "Four completely different things. One colour. Which one needs a fire brigade?"
2. **India's current answer** — FSI's manual mask, and the fact that it deletes the industrial half
3. **The thermal fingerprint** — four FRP-vs-time curves: flare, furnace, stubble, accident. **This is the money slide.**
4. **What we deliver** — the two PS deliverables in NTRO's own words, with your module against each
5. **Why us** — data sources named, benchmark cited, limitations named before anyone asks

### Naming options

- **TAPAS** — Thermal Anomaly Persistence & Attribution System (*tapas* = heat in Sanskrit)
- **AGNI-ID**
- **FIREPRINT**

---

## 16. Known limitations

State these before a judge finds them. Volunteering your blind spots is one of the strongest moves available in a finals Q&A.

| Limitation | Detail | Mitigation |
|---|---|---|
| **Cloud cover** | Thermal IR is completely blocked; monsoon coverage is degraded | Observability layer; INSAT high cadence |
| **Pixel geometry** | Detection is the pixel centre; exact fire location and size are not determinable | Report footprint, not point precision |
| **Suomi NPP sunset** | Data delivery ceases 1 Nov 2026 | Sensor-agnostic ingestion, NOAA-20/21 |
| **VNF is night-only** | Temperature unavailable for daytime detections | Classifier degrades gracefully without it |
| **INSAT resolution** | 4 km IR may miss small scattered fires; 45.31% POD, 73.34% precision vs MODIS | Used for cadence on large events only, not detection sensitivity |
| **OSM incompleteness** | Many MIDC/GIDC plots unmapped or roughly drawn | Learned registry as an independent second source of industrial-ness |
| **Class imbalance** | Industrial accidents ≈1% of labels | Class weighting; report per-class recall |
| **Thermally invisible assets** | Nuclear, ammunition, LPG bottling have no normal signature | Static criticality register, not thermal-derived |
| **Small labelled event set** | 20–40 verified industrial fires | Road 3 is unsupervised by design and needs none |

---

## 17. Quick reference

### Registration URLs

```
FIRMS key    https://firms.modaps.eosdis.nasa.gov/api/map_key/
EOG / VNF    https://eogdata.mines.edu/products/vnf/
MOSDAC       https://www.mosdac.gov.in
```

### Key endpoints

```
FIRMS area   https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}/{SRC}/{BBOX}/{DAYS}/{DATE}
FIRMS avail  https://firms.modaps.eosdis.nasa.gov/api/data_availability/
Overpass     https://overpass-api.de/api/interpreter
Open-Meteo   https://api.open-meteo.com/v1/forecast?latitude=&longitude=&hourly=wind_direction_10m
```

### Constants

```python
INDIA_BBOX        = "68,6,98,37"
PROJ_METRIC       = 7755          # India-wide, metres
DBSCAN_EPS_M      = 500
DBSCAN_MIN_SAMP   = 5
VNF_JOIN_RADIUS_M = 750
VNF_JOIN_WINDOW_S = 1800
ANOMALY_Z         = 3.5
ANOMALY_P99_MULT  = 1.5
MIN_BASELINE_N    = 30
LARGE_FIRE_PIXELS = 3             # FSI convention
DORMANT_HOURS     = 72            # FSI re-ignition window
PROMOTE_NIGHTS    = 20            # Road 1 → registry
```

### Temperature cheat sheet

```
Gas flare        1600–1900 K   tiny source
Steel / smelter  1000–1500 K   small–medium
Brick kiln       1000–1300 K   medium
Stubble burning   800–1000 K   large area
Forest fire       700–1100 K   large, moving
```

### The one-line summary

> India's operational system draws the industrial mask by hand and deletes what it catches.
> We learn it from thermal physics, keep it, and rank it by risk.
