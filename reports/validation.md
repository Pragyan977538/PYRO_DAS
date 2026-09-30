# Stage 10 — Validation

Run 2026-09-29 on the real archive (FIRMS VIIRS and MODIS over India, 2012–2024).
Commands: `make validate` (`scripts/validate_events.py`, then `scripts/check_baghjan.py`)
and `make demo` (`scripts/demo.ps1` or `scripts/demo.sh`). Per-event detail:
[validation/events.json](validation/events.json).

## Summary

| Metric | Result |
|---|---|
| **Per-fire recall, verified industrial accidents** (headline) | **1 of 18 (5.6%)**, against 0.12 expected by chance |
| … with the accident itself labelled industrial | **0 of 18**: Baghjan's 532 alerts called it a "persistent forest fire" |
| Accidents FIRMS saw at all — the ceiling for any FIRMS-based system | **6 of 18 (33%)** |
| Mean time to the first FIRMS detection (5 events with a published time) | **6.0 h** (median 2.8 h) |
| Mean time to an alert | none measurable: the one alerted event has no published time of day |
| Model 1, sub-type accuracy (block CV) | **65.2%** (balanced 61.2%); reference 77% (Liu et al. 2018, with temperature) |
| Road B false positives, share of passes at registry sources (2024) | **0.040%** confirmed, **0.000%** provisional |
| Road A discard rate | **0%** discarded; **0.98%** left unclassified, each with a reason |
| Registry vs GIHS | **83.4%** of active GIHS objects found; **86.2%** of sources on one |
| Registry vs FIRMS `type=2` | **98.6%** of 2024 static detections routed to a registry source |

**The headline fails, and it fails mostly upstream of the classifier.** Twelve of the
eighteen accidents left no trace in FIRMS: they burned for a few hours between
polar-orbiter passes, or under monsoon cloud. Of the six that FIRMS saw, three were
inside running plants and looked like those plants' normal. Two were unmapped factories
that Road A put in the wrong class. The one alert, Baghjan, came from promotion, 21 days
into a five-month fire, and carried the wrong class. (The script's `flagged_as_industrial`
count is 1, but only because one confirmed anomaly fired at Baghjan's neighbouring flare,
registry source 549, during the blowout.)

The strong numbers are about persistent industrial heat: the registry, the per-site
baselines and Model 1. The weak number is about sudden accidents, and for those the
limit is the satellites, not the classifier. The last section says what would change
that.

---

## 1. Per-fire recall on the verified industrial fire set

### The set

`reference/verified_events.csv` holds **20 industrial accidents in India, 2013–2024**.
Each has a published date, time and place, and a source URL. They span refineries, gas
plants, a pipeline, steel plants, a power station boiler, chemical plants and explosives
and fireworks factories. Baghjan 2020 is included.

Where the location came from:

| Location basis | Events | Match radius |
|---|---|---|
| OSM facility outline (named, or identified by position) | 7 | outline's equivalent radius + 500 m (1.1–2.6 km) |
| Wikipedia coordinates of the plant or site | 4 | 2 km (3 km for Baghjan's oilfield point) |
| Village or industrial-estate point (Nominatim) | 5 | 3 km |
| Wikipedia coordinates for Baghjan | 1 | 3 km |
| **Not located** (Dahej 2020, Atchutapuram 2024) | 2 | not scored |

**No location comes from the satellite record.** If it did, the check would find
whatever FIRMS detections the location was built from, and a hit would be guaranteed.
The window rule, the radius rule and the outcome ladder were all fixed before any event
was scored:

- **Window:** the published burning period, or 24 h from the published start. With no
  published time of day, the start date and the next (IST).
- **Replay:** dry, from 60 days before the event, so promotion and baselines have
  realistic history.

Twenty events is at the bottom of the 20–40 target, and two are unlocated. **This is the
class where more verified data would help most.**

### The test

A verified accident counts as **flagged** if, within its radius and window, the system
did either of these:

- raised any Road C alert: provisional, confirmed, or a new persistent site
- classified a Road A detection as industrial

Otherwise, it falls down the ladder:

- **routine** — the heat was seen, but called this plant's normal (Road B)
- **misclassified** — seen, but Road A called it agricultural, forest, other or
  unclassified
- **no signature** — FIRMS has nothing there

A refinery can trip the same test on an ordinary day, so every event also has a
**chance** rate. It is the share of the 60 lead days on which the test would have fired,
compounded over the window. Summed over the events, it is the number of hits a system
firing at random would score: **0.12**.

### Result

| Event | What burned | Outcome | Detections in window | First FIRMS detection after start |
|---|---|---|---|---|
| BAGHJAN-2020 | well blowout, 5 months | **flagged** | 630 (533 Road C) | day only |
| HAZIRA-ONGC-2020 | gas plant | no signature | 0 | — |
| URAN-ONGC-2019 | gas plant | no signature | 0 | — |
| BPCL-MUMBAI-2018 | refinery | no signature | 0 | — |
| HPCL-VISAKH-2013 | refinery | no signature | 0 | — |
| HPCL-VISAKH-2021 | refinery | no signature | 0 | — |
| IOC-HALDIA-2021 | refinery | routine | 7 (Road B) | 9.9 h |
| GAIL-NAGARAM-2014 | pipeline | no signature | 0 | — |
| NLC-NEYVELI-2020 | boiler explosion | no signature | 0 | — |
| SAIL-BHILAI-2018 | steel plant gas line | routine | 1 (Road B) | 14.8 h |
| TATA-JAMSHEDPUR-2022 | steel plant gas line | routine | 15 (Road B) | 1.8 h |
| DEEPAK-NANDESARI-2022 | chemical plant | no signature | 0 | — |
| VISAKHA-SOLVENTS-2020 | chemical plant | no signature | 0 | — |
| PORUS-AKKIREDDIGUDEM-2022 | chemical plant | no signature | 0 | day only |
| AETHER-SACHIN-2023 | chemical plant, 7 h | no signature | 0 | — |
| AMUDAN-DOMBIVLI-2024 | chemical plant | misclassified (unclassified) | 1 (Road A) | 0.9 h |
| HARDA-2024 | fireworks factory | misclassified (agricultural) | 3 (Road A) | 2.8 h |
| SOLAR-NAGPUR-2023 | explosives plant | no signature | 0 | — |

The five first-detection times average **6.0 h**, median 2.8 h. That is when FIRMS
first saw heat there, not when the system alerted.

**FIRMS on its own would flag none of the 18 as industrial accidents.** Its `type` field
marks Haldia, Bhilai and Tata's pixels as static (that is, routine), and Harda's and
Dombivli's VIIRS pixels as vegetation fires.

### Why each one was missed

Satellite passes near each site were read from FIRMS itself: a detection within 1° shows
the satellite was overhead then. Times below are relative to the published start.

**Between passes (4).**
- **Hazira** caught fire at 03:05 IST. The first pass to see anything nearby was
  10 h later.
- **HPCL Visakh 2013** started at 16:46, after the 13:00 pass. The next pass, 9 h
  later, saw only a neighbouring registered plant 2.9 km away (source 321).
- **HPCL Visakh 2021** started at 15:00 and was put out quickly. The next useful pass
  was 22 h later.
- **Deepak Nitrite** started at 18:00 and was controlled within a couple of hours. The
  next pass was 7.6 h later.

**Monsoon, no pass timing recoverable (5).** Uran (September), BPCL Mumbai (August),
GAIL Nagaram (June), Neyveli (July) and Visakha Solvents (July). No fire was detected
within 100 km during their windows, so neither the passes nor the cloud can be read from
FIRMS. Uran burned 07:00–10:00 and Nagaram 05:30–08:30, both outside the usual pass
times as well.

**Overhead, but nothing detected (2).**
- **Aether, Surat**, burned from 01:35 to 08:30. S-NPP passed 5 minutes after it
  started, NOAA-20 at +0.9 h and Aqua at +1.5 h. No detection within 15 km. The heat was
  confined inside a building, or too small against the pixel.
- **Solar Industries, Nagpur**, was an explosion, not a sustained fire. Terra passed at
  +1.1 h and NOAA-20 at +3.6 h.

**Location too coarse to judge (1).** Porus Laboratories: a village-level point (two
villages share the name) and no published time of day.

**Seen, but the plant's normal (3).**
- **Haldia:** the only heat was 10 h later, at night, 1.5–2 km from the published
  point. It was 0.9–2.1 MW against the refinery's own p99 of 2.8 MW: the flares, not the
  fire.
- **Bhilai:** one night detection 15 h later, at 1.7 MW, below the plant's median.
- **Tata Steel:** NOAA-20 and S-NPP passed 1.8 h and 2.6 h after the blast. They saw
  3.1 and 4.8 MW within 400 m, below the works' daytime median of 7.5 MW. If that was the
  fire, nothing in FRP separates it from a steel plant operating. One of the two pixels
  is FIRMS low-confidence.

  This is the per-site baseline working as designed: a fire must beat the site's own
  p99 by half again. A small fire inside a big hot plant is invisible to it.

**Seen, and misrouted by Road A (2).**
- **Dombivli:** seen 52 min after the blast (S-NPP, 8.6 MW, high confidence), on
  built-up land in an industrial estate that OSM does not outline. Road A said
  "unclassified: an unmapped plant, or an urban fire". The reason text was right, but
  the class was not industrial.
- **Harda:** seen 2.8 h after the blast (NOAA-20, 3.7–4.1 MW, and Aqua). The factory
  stood among fields. WorldCover says cropland, OSM has no factory, and February is
  outside the stubble seasons. Road A said "agricultural, outside the main
  stubble-burning seasons"; FIRMS' own `type` said vegetation fire.

**Baghjan, the one alert.**
- The well pad has tree cover, and no mapped well within 375 m. So Road A called its
  first 21 days of fire **forest**.
- It was promoted to a provisional source at the end of 29 June. From the next pass
  (30 June, **21 days after the fire began**; FIRMS first saw it on 9 June at 22:04 IST)
  until the well was killed, every pass alerted: 532 detections, never Road B.
- **That is the promotion rule doing its job** (Stage 5 regression test). But the
  alert's class is the class Road A had given it: "persistent forest fire in one place".

### What would change the number

These are hypotheses from reading the misses. They are **not re-scored**: a rule tuned
on these 18 events would make the recall circular.

1. **Geostationary data for accidents.** Twelve misses are about when the satellite
   looked, not what the system did with what it saw. Geostationary imagers look every
   10–30 minutes:
   - INSAT-3DS (4 km thermal infrared)
   - Himawari-9 AHI (2 km)

   Sun-synchronous FIRMS fundamentally cannot time an accident. The registry and
   baselines built here would carry over unchanged.
2. **Industrial-estate outlines as a Road A layer.** State development corporations
   (MIDC, GIDC, APIIC) publish estate boundaries. With them, Dombivli becomes
   "industrial", and the two unlocated events can be placed.
3. **OSM oil and gas wells.** A mapped well pad would have made Baghjan oil and gas from
   day one.
4. **Cell-level baselines inside big plants.** Tata's pixels were in a coke-plant cell
   that is normally cooler than the works as a whole. This trades against the memory
   and n ≥ 30 floor that per-source baselines were chosen for.

---

## 2. Model 1: source sub-classification

Out-of-fold, 5-fold GroupKFold on 2° blocks, 466 labelled registry sources (Stage 4;
[model1_metrics.json](model1_metrics.json)).

| True ↓ / Predicted → | oil and gas | heavy industry | mining | Recall |
|---|---|---|---|---|
| oil and gas (34) | **20** | 10 | 4 | 58.8% |
| heavy industry (267) | 7 | **204** | 56 | 76.4% |
| mining (165) | 4 | 81 | **80** | 48.5% |
| **Precision** | 64.5% | 69.2% | 57.1% | |

- **Accuracy is 65.2%.** Balanced accuracy is 61.2%, and macro-F1 0.62.
- **The references:**
  - Location alone: 40.8% balanced.
  - Shuffled labels: 33.3%; the permutation test gives p ≈ 0.01.
  - The published reference is 77% (Liu et al. 2018, VNF industrial sub-types, global).
    That work used VNF temperature, the strongest discriminator, which is empty here until
    the licence arrives.
- **Oil and gas**, the class NTRO cares most about, is usable at 58.8% recall and
  64.5% precision.
- **Mining vs heavy industry is where it errs.** Much of that is label noise: 119
  sources have evidence for two classes within 1 km.

---

## 3. Road B false-positive rate (the normal stream)

These are passes at registry sources in a year never used for tuning, with baselines
only from earlier years (Stage 5).

| | 2024 (79,114 passes) | 2023 |
|---|---|---|
| Confirmed false alerts | **0.040%** (32) | 0.083% |
| Provisional false alerts | **0.000%** (0) | 0.006% |
| Confirmed recall on spikes injected into real histories | 88.2% (target 90%) | 87.8% |

These are upper bounds. Every alert in an un-injected year counts as false, and some are
real, for example HMEL Bathinda breaching for three weeks in January 2023.

---

## 4. Road A discard rate

Road A discards nothing. Every one of **1,039,512** Road A detections in 2024 gets a
class and a reason.

- **1.0% are left unclassified** (10,232), each saying why:
  - built-up land with no mapped facility (4,820)
  - water at ~300 m: riverbanks, char lands, wetlands (2,938)
  - bare ground with no mapped mine (2,474)
- **The alternative the original design anticipated** was a FIRMS confidence filter
  (VIIRS low, MODIS < 30). It would have discarded:
  - **19.9% of Road A** (207,064 detections)
  - 19.4% of Road A's industrial detections (3,184 of 16,449)
  - one of Tata Steel's two post-blast pixels

---

## 5. Registry vs GIHS and vs FIRMS `type=2`

The registry is 557 sources from the 13-year archive (Stage 3). GIHS (Ma et al. 2024)
is evaluation only, never a label.

| | Result |
|---|---|
| GIHS objects active in 2021 found (within 1 km) | **83.4%** (floor 75%) |
| Sources on a confirmed GIHS object (a lower bound on precision) | **86.2%** (floor 85%) |
| All confirmed GIHS objects, including those that stopped before 2021 | 72.5% |
| FIRMS `type=2` detections on a source, 2012–2024 | **99.3%** |
| 2024 replay: VIIRS `type=2` detections routed to a registry source | **98.6%** (136,855 of 138,845) |
| 2024 replay: registry-source detections that FIRMS marks static | 89.1% |
| 2024 replay: detections called industrial that FIRMS does *not* mark static | 34,911 of 172,033 (20.3%) |

**FIRMS' static flag and the registry agree almost everywhere** FIRMS has an opinion.

The last row is where they part. About 35,000 detections a year are called industrial
here but not static by FIRMS. They are:
- Road A fires on mapped industry
- new persistent sites
- registry passes that FIRMS has not yet marked

FIRMS also says only that a site *recurs*. The registry adds the site's class, its
baseline, and whether today's reading is normal for it.

---

## 6. The demo

`scripts/demo.ps1` (Windows) and `scripts/demo.sh` start the API if nothing is answering
on port 8000. They then walk six map stops, 30 s each (about three minutes), each a URL
with its narration:

1. **India in a November week.** Every class in its own colour.
2. **HMEL Bathinda in the paddy belt.** A refinery source among stubble fires.
3. **Reliance Jamnagar at night.** Road B, "normal for this site", with the baseline
   gauge.
4. **A confirmed anomaly at the Chennai refinery.** Road C, the reason, and the risk
   breakdown.
5. **NMDC Nagarnar steel plant.** A new persistent industrial site, alerting until
   reviewed.
6. **The highest-risk event of 2024.** A new hot spot inside the Reliance complex:
   Road A, oil and gas, risk 84.

`-Check` (`--check`) opens nothing. It verifies every stop's page and the API record
behind its panel, and exits 1 if any would show empty. It passes, and all six stops were
also opened in a browser and render their panels.

---

## 7. Limitations

| Limitation | Effect | Where it shows |
|---|---|---|
| **Polar-orbiter timing** | Accidents shorter than the gap between passes (~12 h at a site for VIIRS, less with MODIS) leave no FIRMS trace | 12 of 18 verified accidents: Hazira, Uran, BPCL, HPCL ×2, Nagaram, Neyveli, Deepak, Visakha Solvents, Porus, Aether, Solar |
| **Monsoon cloud** | No detection at all under cloud; observability is modelled (NASA POWER) but cannot restore a missed fire | Uran, BPCL, Nagaram, Neyveli, Visakha Solvents |
| **Heat inside buildings or vessels** | Reactor and warehouse fires can burn for hours without a pixel | Aether: three passes during a 7-h fire, nothing within 15 km |
| **Small fire inside a big hot plant** | Below the site's own p99: Road B by design | Tata Steel, Bhilai, Haldia |
| **Road A depends on OSM** | An unmapped factory is classed by land cover | Harda (cropland), Dombivli (built-up), Baghjan's first 21 days (forest) |
| **A provisional source keeps Road A's class** | The alert is right, the label may not be | Baghjan alerted as "persistent forest fire" |
| **No VNF temperature** | Model 1 at 65% vs a 77% reference that used temperature | Stage 4 |
| **Small verified set** | 18 located events: one event moves recall by 5.6 points, and the 33% signature rate is ±11 points at one standard error | This report |
| **Event locations are coarse for five events** | 3 km village or estate radii; two events not located at all | Porus, Harda, Dombivli, Solar, Deepak; Dahej, Atchutapuram |
| **Docker not verified** | `docker compose up` written but untested (no Docker on the build machine) | Stage 9 |
