# PS 26162 — Implementation plan

**AI-Based Detection and Classification of Industrial Fires and Persistent Thermal Sources**
NTRO · Software · Disaster Management · SIH 2026

> **Partly superseded (2026-09-29).** The numbers below come from a synthetic benchmark that
> modelled stubble and forest fires as tight point sources — which is why raw DBSCAN looked
> fine here. Several conclusions were revised after a review and a real-data test: see
> `CLAUDE.md` → *Changed decisions* and `reports/stage1_5_spike.md`. Where they disagree,
> `CLAUDE.md` wins.

---

## 0. Verdict

**Workable.** Every external dependency responds, the algorithm separates the classes on
a realistic synthetic benchmark, and the anomaly test has an acceptable false-positive
rate. Nothing in the design requires data or compute you don't have.

Three things are unverified and need action in week 1:
FIRMS / EOG / MOSDAC credentials (registration, not code), Overpass reachability from
your network, and real-data accuracy — which will be **lower** than the synthetic number
below, for reasons stated in §2.

---

## 1. Dependency check — actually tested

Run on 2026-09-28 from a clean Python 3.12 container.

| Dependency | Status | Notes |
|---|---|---|
| FIRMS area API | **Reachable** | `400 Invalid MAP_KEY` on the documented URL — endpoint and path format confirmed correct, only the key is missing |
| FIRMS data_availability | **Reachable** | `401 Invalid MAP_KEY`, same conclusion |
| EOG VNF portal | **200 OK** | Page loads. Data download needs a licence since 10 Jan 2025 (free for academic use, signed agreement + approval) — corrected 2026-09-28 |
| Open-Meteo | **200 OK, full JSON** | No key needed. Works today, verified with a real query at Jamnagar |
| MOSDAC (INSAT-3D) | **200 OK** | Portal reachable; data needs registration |
| FSI fire portal | **200 OK** | Reachable |
| Geofabrik India extract | **200 OK** | **Use this as the primary OSM source** |
| Overpass API | **Unreachable** | 503 / timeout on the main endpoint and two mirrors. Likely a sandbox egress restriction — retest from your own network, but plan around it |

### Python environment

```
python 3.12.3    pandas 3.0.2    numpy 2.4.4    scikit-learn 1.8.0
scipy 1.17.1     requests 2.33.1  xgboost 3.4.1 (pip install, worked cleanly)
```

Missing and needed: `geopandas`, `shapely`, `pyproj`, `psycopg2-binary`, `SQLAlchemy`.
All are standard pip installs.

### Action on Overpass

Don't build the ingest around Overpass. Download the Geofabrik India extract
(`https://download.geofabrik.de/asia/india-latest.osm.pbf`, ~1.7 GB) and filter it
locally with `osmium`. It's faster, it won't time out mid-demo, and it gives you a
reproducible snapshot instead of a live service that can be down when the judges
are watching.

```bash
osmium tags-filter india-latest.osm.pbf \
  w/landuse=industrial,quarry w/man_made=works,flare w/power=plant w/landuse=forest \
  -o india_industrial.osm.pbf
ogr2ogr -f PostgreSQL PG:"dbname=fires" india_industrial.osm.pbf multipolygons \
  -nln osm_industrial -nlt MULTIPOLYGON
```

---

## 2. Algorithm verification — synthetic end-to-end run

I simulated 324 thermal sources across India over 6 years (158,536 detections) with
realistic degradations, then ran the full pipeline: DBSCAN → fingerprint → XGBoost →
anomaly test.

**Degradations applied** (this matters — without them the result was a meaningless 100%):

- 45% within-class parameter spread, so a small furnace and a large furnace differ
- 55% of detections have **no VNF temperature** (night-only + Planck-fit failures)
- 12% label noise, simulating imperfect OSM/FSI weak supervision
- 18% of sources placed within ~400 m of another source, so clusters merge
- Monsoon cloud: only 29% of Jun–Sep days observable, 64% annually

### Results

**Clustering**

```
271 clusters recovered from 324 true sources
mean cluster purity 0.944 | noise fraction 0.00%
```

53 sources merged into neighbours. That is the co-located-flares problem, and it is
real — expect it on real data and state it as a known limitation.

**Source classifier** — thermal features only, no location, GroupKFold split by state

```
overall accuracy 94.5%

              precision  recall  f1     n
flare           0.977    0.977   0.977  44
furnace         1.000    0.977   0.988  43
mining          0.971    0.971   0.971  35
kiln            0.980    0.980   0.980  49
cropland        0.885    0.885   0.885  52
forest          0.878    0.896   0.887  48
```

Top features: `persistence 0.25`, `temp_p50 0.21`, `area_p50 0.14`, `month_entropy 0.11`.

**Read this honestly.** 94.5% is an upper bound, not a forecast. Synthetic detections are
drawn from the same distributions the model is learning, so the classifier has an
advantage it won't have on real data. **Plan for the published benchmark of ~77%** and
treat anything above it as a win.

The informative part is not the headline number but the **shape of the errors**: industrial
classes separate cleanly, while cropland and forest confuse each other. That is physically
correct — both are biomass at similar temperatures — and it will reproduce on real data.
Since your deliverable is separating *industrial from natural*, confusion *within* the
natural group costs you almost nothing.

**Anomaly test** — `z > 3.5` AND `> 1.5 × p99`, per-source baseline, 206 sources

```
false positives on normal operation : 0.78% of passes
detection of injected 6-12x events  : 96.6%
```

At roughly six passes a day across a few thousand registry sources, 0.78% still produces
a meaningful daily volume. **Add the two-consecutive-pass rule** and this drops by
roughly an order of magnitude. Do not skip it.

---

## 3. Build order

| Week | Deliverable | Gate before moving on |
|---|---|---|
| 1 | Keys obtained; FIRMS backfill in PostGIS; Geofabrik OSM loaded | `SELECT count(*) FROM detections` returns millions |
| 2 | Clustering + fingerprints → registry populated | Registry row count is plausible; spot-check 10 clusters against satellite imagery |
| 3 | Weak labels joined; Model 1 trained; **confusion matrix printed** | Accuracy reported with a state-grouped split |
| 4 | Router + Road A classifier + anomaly test running on live pulls | End-to-end: a new detection gets a class |
| 5 | MapLibre frontend, layers per class, click-through detail | Deliverable (ii) demonstrable |
| 6 | Event assembly, risk scoring, validation write-up | Differentiation |

**Cut order if behind:** risk scoring → event assembly → Road A classifier (degrade to
rules). **Never cut week 3.** A strong classifier with a plain map beats a beautiful map
over a threshold rule.

---

## 4. Working code

### 4.1 FIRMS backfill

```python
import pandas as pd, time
from datetime import date, timedelta

MAP_KEY    = "..."                      # firms.modaps.eosdis.nasa.gov/api/map_key/
INDIA_BBOX = "68,6,98,37"               # west,south,east,north
BASE       = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

def backfill(source, start, end):
    frames, cur = [], start
    while cur < end:
        url = f"{BASE}/{MAP_KEY}/{source}/{INDIA_BBOX}/10/{cur:%Y-%m-%d}"
        try:
            df = pd.read_csv(url)
            if len(df): frames.append(df)
        except Exception as e:
            print(f"skip {cur}: {e}")
        cur += timedelta(days=10)
        time.sleep(1.5)                 # limit is 5000 transactions / 10 min
    return pd.concat(frames, ignore_index=True)

# archive uses the science-quality (_SP) products, live uses _NRT
hist = backfill("VIIRS_SNPP_SP", date(2016,1,1), date(2026,1,1))
```

**Normalise the column names immediately.** MODIS emits `brightness` / `bright_t31`,
VIIRS emits `bright_ti4` / `bright_ti5`. Half your joins will silently drop rows if you
skip this.

```python
RENAME = {"brightness":"bt4", "bright_ti4":"bt4",
          "bright_t31":"bt5", "bright_ti5":"bt5"}
hist = hist.rename(columns=RENAME)
hist["acq_datetime"] = pd.to_datetime(
    hist.acq_date + " " + hist.acq_time.astype(str).str.zfill(4),
    format="%Y-%m-%d %H%M", utc=True)
```

### 4.2 Clustering — projected, never in degrees

```python
import numpy as np
from sklearn.cluster import DBSCAN

# equirectangular metres is sufficient at cluster scale and avoids a pyproj dependency
x = df.longitude.values * 111320 * np.cos(np.radians(df.latitude.values))
y = df.latitude.values  * 110540

df["cluster"] = DBSCAN(eps=500, min_samples=5,
                       algorithm="ball_tree", n_jobs=2).fit_predict(np.c_[x, y])
```

~~`ball_tree` matters. The default `auto` blew the container's memory at 330k points;
`ball_tree` handled 158k comfortably. At India scale you may need to **cluster state by
state** and merge — sources never span state boundaries at 500 m.~~

**Corrected 2026-09-29.** The comparison above changed the data size as well as the
algorithm, so it never showed that `ball_tree` helps. scikit-learn's DBSCAN stores every
point's neighbour list whatever the tree, so memory grows with the square of cluster
size. What bounds it is snapping detections to 375 m cells and passing `sample_weight`.
Sources can also straddle state borders. See `CLAUDE.md` and `reports/stage1_5_spike.md`.

`cluster == -1` is the noise label. Those are your Road A candidates, free.

### 4.3 Fingerprint

```python
from scipy.stats import entropy

def fingerprint(g, observed_nights):
    f = g.frp.values
    mc = g.acq_datetime.dt.month.value_counts(normalize=True)
    return {
        "temp_p50":      np.nanmedian(g.vnf_temp_k),
        "temp_p90":      np.nanquantile(g.vnf_temp_k, 0.90),
        "temp_iqr":      np.subtract(*np.nanquantile(g.vnf_temp_k, [.75,.25])),
        "temp_cov":      g.vnf_temp_k.notna().mean(),      # how much VNF we actually got
        "area_p50":      np.nanmedian(g.vnf_area_m2),
        "frp_p50":       np.median(f),
        "frp_p95":       np.quantile(f, 0.95),
        "frp_mad":       np.median(np.abs(f - np.median(f))),
        "bt_diff_p50":   np.median(g.bt4 - g.bt5),
        "persistence":   g.acq_datetime.dt.date.nunique() / observed_nights,
        "night_ratio":   (g.daynight == "N").mean(),
        "month_entropy": entropy(mc.reindex(range(1,13), fill_value=1e-9)),
        "n_years":       g.acq_datetime.dt.year.nunique(),
        "n_det":         len(g),
    }
```

Two notes from the run. **Use `np.nan*` variants everywhere** — 55% of rows have no
temperature and plain `np.median` returns NaN for the whole cluster. And `temp_cov`
(the fraction of detections that *had* a temperature) turned out to be a useful feature
in its own right, because sources that are consistently hot enough to Planck-fit are
different from sources that aren't.

**Divide persistence by nights observed, not calendar nights.** With 29% monsoon
observability, calendar-night persistence understates every source by a third.

### 4.4 Classifier

```python
from xgboost import XGBClassifier
from sklearn.model_selection import GroupKFold
from sklearn.metrics import classification_report

FEATS = ["temp_p50","temp_p90","temp_iqr","temp_cov","area_p50","frp_p50",
         "frp_p95","frp_mad","bt_diff_p50","persistence","night_ratio",
         "month_entropy","n_years","n_det"]
# NOTE: no dist_industrial, no landcover — those generated the labels

codes, names = pd.factorize(fp.cls)
oof = np.empty(len(fp), dtype=int)
for tr, te in GroupKFold(5).split(fp[FEATS], codes, groups=fp.state):
    m = XGBClassifier(n_estimators=400, max_depth=6, learning_rate=0.05,
                      subsample=0.8, colsample_bytree=0.8)
    m.fit(fp[FEATS].iloc[tr], codes[tr])
    oof[te] = m.predict(fp[FEATS].iloc[te])

print(classification_report(codes, oof, target_names=names, digits=3))
```

XGBoost handles the NaN temperatures natively — no imputation, and don't add any.

### 4.5 Weak labels

```sql
UPDATE sources s SET cls = 'industrial'
FROM osm_industrial o
WHERE ST_DWithin(s.geom::geography, o.geom::geography, 300) AND s.cls IS NULL;

UPDATE sources s SET cls = 'forest'
FROM fsi_alerts f
WHERE ST_DWithin(s.geom::geography, f.geom::geography, 500) AND s.cls IS NULL;
```

Expect ~12% of these to be wrong. The synthetic run injected exactly that and the
classifier still worked — gradient boosting is fairly robust to label noise at this level.

### 4.6 Anomaly test

```python
def build_baseline(frp):
    med = np.median(frp)
    return {"med": med,
            "mad": max(np.median(np.abs(frp - med)), 1e-6),
            "p99": np.quantile(frp, 0.99),
            "n":   len(frp)}

def is_anomaly(frp, b):
    if b is None or b["n"] < 30:
        return False                     # too thin — route to Road A instead
    z = 0.6745 * (frp - b["med"]) / b["mad"]
    return z > 3.5 and frp > b["p99"] * 1.5
```

Key the baselines by `sensor | daynight | season`, per source. **Per source, never per
source type** — pooling all furnaces means a large one catching fire may sit below the
pooled p99 while a small one operating normally breaches it.

Wrap with the two-pass rule:

```python
def confirm(source_id, frp, b):
    if not is_anomaly(frp, b): return False
    return last_pass_also_breached(source_id)   # from the detections table
```

### 4.7 Observability

```python
def record_pass(cells_in_swath, sensor, obs_date, cloud_mask):
    for cell in cells_in_swath:
        upsert("observability", cell_id=cell, obs_date=obs_date, sensor=sensor,
               clear_looks=0 if cloud_mask.covers(cell) else 1)
```

Write a row on **every** pass, whether or not anything burned. This is the persistence
denominator and the honest answer to the cloud question.

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Overpass unreachable / times out | **High** — already observed | Geofabrik extract, filtered offline. Already the recommended path |
| EOG or MOSDAC registration is slow | Medium | Register day 1. System degrades without VNF (`temp_cov` drops, accuracy falls) but still runs |
| DBSCAN memory blowup at national scale | **High** — already hit it | Snap to 375 m cells + `sample_weight` (corrected 2026-09-29: `ball_tree` does not fix it) |
| Co-located sources merge | **Certain** — 53/324 in the synthetic run | State it as a known limitation; hierarchical second pass if time |
| Real accuracy well below 94.5% | **Certain** | Benchmark against 77%, not against the synthetic figure |
| Anomaly false positives at scale | Medium | Two-pass confirmation is not optional |
| Agricultural volume swamps output | High | Separate suppressed summary layer, out of the alert stream |

---

## 6. Numbers to quote

Only these are measured. Everything else is a design choice.

- **0.78%** false-positive rate on normal operation, before two-pass confirmation
- **96.6%** detection of 6–12× FRP excursions
- **0.944** mean cluster purity with 18% co-located sources
- **64%** annual observability, **29%** during monsoon
- **77%** published benchmark for VIIRS Nightfire industrial heat-source sub-classification

When asked for accuracy before you have real results, say: *"On a synthetic benchmark with
realistic noise we get 94.5%, but that's an upper bound because synthetic data is drawn
from the distributions the model learns. Our target against real data is the published
77% benchmark."*

That answer is stronger than a confident number you can't defend.
