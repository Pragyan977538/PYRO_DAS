# Stage 6 — event assembly

A fire is seen as many detections: several pixels on one pass, again on the next,
and for days if it keeps burning. The map, the alerts and Stage 7's risk score need
the *incident*. `firewatch/inference/events.py` assembles the routed detections into
events. Commands: `make events` (`scripts/build_events.py`), and
`scripts/check_dedup.py` for acceptance.

The rules were written into `docs/ROADMAP.md` before anything ran:
- **Road B makes no events.** A plant operating normally is not an incident.
- **Anomaly** (Road C at a registry source): one event per source. An alert within
  72 h of its last alert extends it, and a later one opens a new event.
- **New source** (a provisional source): one event for its whole life. It includes the
  Road A detections that got it promoted, so a blowout starts at its first fire.
- **Fire** (Road A): a day's detections are clustered at 750 m. A cluster joins an open
  event within 750 m of that event's detections from the last 72 h, capped at 10 km
  wide.
- **Lifecycle:** active within 24 h of the last detection, dormant to 72 h (a new
  detection re-ignites the event), closed after. Provisional incidents stay open until
  the source is retired.

## Result: 2024

`make events` assembles the 2024 replay (Stage 5) in about 70 s, then writes it.

| | |
|---|---|
| Routed detections (Roads A and C) | 1,049,887, every one linked to an event; no Road B detection linked |
| **Events** | **461,007**: 460,864 fires, 115 new-source incidents, 28 anomalies |
| Detections per event | median 1 (most crop fires are one pixel on one pass); largest 2,058 |
| Events spanning more than one day | 53,926 |
| Status as of 1 Jan 2025 | 1,155 fires active, 1,482 dormant; provisional incidents 29 active, 37 dormant, 49 closed (retired) |

| Category | Events | Mean detections | Largest |
|---|---|---|---|
| agricultural | 267,174 | 1.9 | 416 |
| forest | 138,928 | 3.0 | 845 |
| other vegetation | 43,628 | 2.2 | 259 |
| **industrial** | **6,112** | 3.8 | 2,058 |
| unclassified | 5,165 | 1.5 | 91 |

The 30 confirmed-alert passes at registry sources make **28 anomaly events**:
consecutive alerts at one plant are one incident. The 10,331 `new_source` detections
make **115 events**: one per site, not one alert per pass.

## Acceptance: one fire, one event

`check_dedup.py` replays every event in `reference/verified_events.csv` from 60 days
before it, dry. It passes only if the fire's detections (within the event's radius,
between its published dates) all land in exactly one event.

**Baghjan 2020: pass.** All 645 of the fire's detections are in one `new_source`
event, which starts on 9 June 2020, the fire's first detection, not the day of
promotion. It ends on 14 November, a day before the well was killed.

The first attempt failed with three events. Road A detections just outside the
promoted cells, after gaps longer than 72 h, started new fires, because linking only
looked at an event's last 72 hours. That contradicted the rule written above ("one
event for its whole life"). Fires near a live provisional incident now link to it at
any time.

## Incidents worth a look (2024)

The largest `new_source` incidents are fires the registry cannot hold yet. Its gate
needs three years; these sites are younger, or were quiet in earlier years.

| Where | Road A class | Seen | Note |
|---|---|---|---|
| 22.04 N 83.73 E | mining | 299 days, 2,058 detections | The year's largest: Odisha's coal belt, beside mapped mines |
| 22.36 N 82.30 E | mining | 138 days | Chhattisgarh, beside mapped mines |
| 21.10 N 72.64 E | forest | 269 days | Hazira, near Surat, an industrial port: a fire on 269 days is not a forest fire. Land cover misleads here |
| 10.25 N 77.37 E | forest | 23 days, peak 606 MW | April 2024, Palani hills: consistent with the forest fires reported around Kodaikanal that month (not yet in the verified set) |
| 22.13 N 72.77 E | agricultural | 208 days, peak 4 MW | Cambay basin oil field: very likely an unmapped gas flare, not crop burning |

The last row is why a provisional source's *class* from land cover is weak. Only
after three years, in the registry, does Model 1 classify it from its fingerprint.
The *alert* is what matters meanwhile, and it holds.

Stage 7 (risk scoring) will rank these events, and Stage 8 serves them.
