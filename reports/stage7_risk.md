# Stage 7 — risk scoring

Every event carries a score and the answer to "why that number?".

```
Risk = 100 × H^0.40 × E^0.35 × V^0.25        H, E, V each floored at 0.05
```

**Multiplicative on purpose.** A huge fire in empty land next to nothing valuable must
score low, and an additive score would hand it a high one on hazard alone.

Code: `firewatch/risk/` (`assets`, `population`, `score`); commands: `make risk`
(`scripts/score_risk.py`, then `scripts/check_risk.py`). The formula and weights are the
original design's ([DESIGN.md](../docs/DESIGN.md) §2.1).

| Term | What it is | From |
|---|---|---|
| **H** hazard = 0.5 f + 0.3 n + 0.2 g | f: percentile of the peak pixel's FRP in the national history; n: pixels in the biggest pass, n / (n + 3); g: growing 1, flat 0.5, shrinking 0 | detections; a 3% sample of every detection before the scored year |
| **E** exposure = 0.45 p5 + 0.30 pw + 0.25 a | p5: people within 5 km; pw: people in the 30° sector downwind to 10 km, on the event's last day; both ranked against historical fire locations. a = 1 − exp(−A/2), A the criticality-weighted count of other assets within 10 km | WorldPop 2020, 1 km; NASA POWER daily wind components |
| **V** vulnerability | criticality × exp(−d / 2 km) of the nearest critical asset within 2 km, or the burning source's own class, whichever is higher | the asset register |

## The asset register, generated rather than hand-compiled

The original design called for ~200 entries compiled by hand from PESO, CEA and MoPNG.
For the prototype it is **generated from maps** instead. It stays static
and never thermal, which is the non-negotiable: a nuclear plant is thermally invisible
until something burns.

| Asset type | Entries | Criticality |
|---|---|---|
| nuclear power station (WRI) | 9 | 1.00 |
| LNG / LPG terminal | 15 | 1.00 |
| oil refinery | 56 | 0.95 |
| petrochemical | 4 | 0.90 |
| chemical plant | 129 | 0.80 |
| thermal power station (OSM and WRI) | 503 | 0.70 |
| fertiliser plant | 31 | 0.70 |
| oil and gas field (wells, flares) | 96 | 0.60 |
| steel and cement | 38 | 0.50 |
| mine | 12,004 | 0.40 |
| industrial estate | 29,526 | 0.30 |
| kiln | 5,696 | 0.25 |
| **total** | **48,107** | |

- **The criticality values** follow the original design's table. Nuclear, oil and gas fields,
  mines and kilns, which it does not list, are placed between its rows.
- **Manual rows** (`source_ref` `manual:…`) survive reseeding, so the official lists can
  still be layered on.
- **Heritage sites** are left out for the prototype.

## Result: 2024

All **461,008** events are scored in about 3 minutes, and every one has a complete
decomposition.

| Kind | Events | Median risk | Max |
|---|---|---|---|
| anomaly at a registry source | 28 | **52.2** | 77.8 |
| new persistent site (provisional) | 115 | **47.4** | 72.6 |
| fire (Road A) | 460,865 | 25.3 | 83.7 |

The highest-scoring events are fires inside or beside refineries:
- Reliance Jamnagar: 83.7 and 81.6
- Gujarat Refinery at Koyali: 81.5, with 300,000 people within 5 km
- HMEL Bathinda: 80.5
- Haldia Petrochemicals: 79.1
- Prayagraj thermal power station: 77.9

## Acceptance: a large fire far from people and assets scores low

The acceptance group is every 2024 event with hazard ≥ 0.8, fewer than 2,000 people
within 5 km *and* downwind, no critical asset within 10 km, and vulnerability at its
floor.

| | Events | Median risk | Max |
|---|---|---|---|
| **Big fire, remote** | 482 | **15.6** | 17.8 |
| Big fire at a critical asset (V ≥ 0.5) | 5 | **79.2** | 83.7 |

**The largest remote fire of 2024** was a forest fire: 74 detections in one day, peaking
at **2,559 MW**. It scores **16.5**. An additive formula with the same weights would have
given it **42.4**, above most refinery incidents. That is the argument for
multiplication.

## What the tests caught

- **The downwind term pointed upwind.** FFT *convolution* flips its kernel, so the
  sector sums were counting the people behind the fire. The 5 km disk is symmetric and
  hid it. A test with 1,000 people 5 km east and an eastward wind found it.
  - It is now a correlation, and every event was rescored.
  - The first run's numbers were all computed with the bug; the ones above are after the
    fix.
- **One "remote" fire then scored 25.4,** over the check's ceiling. It had 1,951 people
  within 5 km but 9,064 downwind. Its score rose for the right reason once the wind
  pointed the right way. The check's definition of "far from population" now includes
  the people the smoke reaches; the threshold did not move.

## Limits

- **Wind is a daily mean** on a ~50 km grid, not the wind at the pass.
- **Population is 2020 WorldPop at 1 km.**
- **Criticality comes from map tags.** A refinery tagged only `landuse=industrial` counts
  as an industrial estate (0.30), not a refinery (0.95). Manual rows are the remedy.
- **The exponents and weights are the original design's starting point,** not
  calibrated. They can be tuned per deployment.
