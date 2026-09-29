"""Stage 10 headline: per-fire recall on the verified industrial fire set.

    python scripts/validate_events.py                 # every located event
    python scripts/validate_events.py --only HARDA-2024

Each event in reference/verified_events.csv comes from a published report, and
its location from Wikipedia or an OpenStreetMap outline -- never from the
satellite record, so a hit cannot be manufactured by where the event was put.
For each located event the system is replayed as it would have run live: dry,
nothing written, from 60 days before the fire (so promotion and baselines have a
realistic history) to the end of its window, over +-0.3 degrees around it.

Then one question: within ``match_m`` of the published point, while it burned,
did the system flag an industrial fire -- a Road A detection classified
industrial, or any Road C alert (provisional, confirmed, or a new source)? The
ladder below that records why it did not:

    routine         the heat was seen but called this plant's normal (Road B)
    misclassified   seen, but Road A called it agricultural, forest or other
    no signature    FIRMS has nothing there: the fire burned between passes,
                    was too small, or was hidden by cloud or smoke

A refinery's flare can be flagged on an ordinary day too, so every event also
gets a chance rate: the share of the 60 lead days on which the same test would
have fired. Summed over events it is the number of hits a system flagging at
random would score -- the recall has to be read against it.

The window is the published burning period, or 24 h from the published start
when no end is published; with no published time of day it is the start date
and the next (IST). Time-to-detect uses only events with a published time.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from run_inference import run  # noqa: E402

from firewatch.config import settings  # noqa: E402
from firewatch.db import fetch_all  # noqa: E402
from firewatch.grid import to_metres  # noqa: E402
from firewatch.inference import ROAD_A, ROAD_B, ROAD_C  # noqa: E402
from firewatch.inference.engine import read_window  # noqa: E402

OUT = REPO / "reports" / "validation"
EVENTS = REPO / "reference" / "verified_events.csv"
IST = "Asia/Kolkata"
HALF_DEG = 0.3
LEAD = pd.Timedelta(days=60)
DEFAULT_SPAN = pd.Timedelta(hours=24)

log = logging.getLogger("validate_events")


def window(row) -> tuple[pd.Timestamp, pd.Timestamp, bool]:
    """(start, end, time_known) in UTC, by the rule in the module docstring."""
    day = pd.Timestamp(row["start"]).tz_localize(IST)
    known = isinstance(row["start_time_ist"], str) and row["start_time_ist"].strip() != ""
    if known:
        hh, mm = (int(v) for v in row["start_time_ist"].split(":"))
        start = day + pd.Timedelta(hours=hh, minutes=mm)
        end = start + DEFAULT_SPAN
    else:
        start, end = day, day + pd.Timedelta(days=2)
    if isinstance(row["end"], str) and row["end"].strip():
        end = max(end, pd.Timestamp(row["end"]).tz_localize(IST) + pd.Timedelta(days=1))
    return start.tz_convert("UTC"), end.tz_convert("UTC"), known


def flagged(det: pd.DataFrame) -> pd.Series:
    """The test for 'the system flagged an industrial fire here'."""
    return ((det["road"] == ROAD_A) & (det["category"] == "industrial")) | \
        (det["road"] == ROAD_C)


def nearest_source(lat: float, lon: float) -> dict | None:
    rows = fetch_all(
        "SELECT source_id, cls, osm_name, provisional, "
        "ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) "
        "AS m FROM sources WHERE NOT coalesce(provisional, false) "
        "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) LIMIT 1",
        {"lon": lon, "lat": lat})
    if not rows:
        return None
    r = rows[0]
    return {"source_id": r["source_id"], "cls": r["cls"], "osm_name": r["osm_name"],
            "distance_m": round(float(r["m"]))}


def outcome(fire: pd.DataFrame) -> str:
    if not len(fire):
        return "no signature"
    if flagged(fire).any():
        return "flagged"
    if (fire["road"] == ROAD_B).any():
        return "routine"
    return "misclassified"


def hours(a: pd.Timestamp | None, b: pd.Timestamp) -> float | None:
    return None if a is None or pd.isna(a) else round((a - b) / pd.Timedelta(hours=1), 1)


def validate(row) -> dict:
    lat, lon = float(row["published_lat"]), float(row["published_lon"])
    radius = float(row["match_m"])
    start, end, known = window(row)
    replay = ((start - LEAD).floor("D"), end.ceil("D"))
    bbox = (lon - HALF_DEG, lat - HALF_DEG, lon + HALF_DEG, lat + HALF_DEG)
    result, stats = run(*replay, dry=True, bbox=bbox)
    det = read_window(*replay, bbox).merge(result.detections, on="detection_id")
    det["road"] = pd.to_numeric(det["road"]).astype(int)
    x, y = to_metres(det["latitude"].to_numpy(), det["longitude"].to_numpy())
    fx, fy = to_metres([lat], [lon])
    det["dist_m"] = np.hypot(x - fx[0], y - fy[0])
    near = det[det["dist_m"] <= radius].sort_values("acq_datetime")
    during = det[(det["acq_datetime"] >= start) & (det["acq_datetime"] < end)]
    fire = near[(near["acq_datetime"] >= start) & (near["acq_datetime"] < end)]
    lead = near[near["acq_datetime"] < start]

    # chance: how often the same test fires on an ordinary day at this spot
    lead_days = lead.loc[flagged(lead), "acq_datetime"].dt.tz_convert(IST).dt.date.nunique()
    per_day = lead_days / LEAD.days
    span_days = (end - start) / pd.Timedelta(days=1)
    chance = 1 - (1 - per_day) ** span_days

    hit = fire[flagged(fire)]
    promoted = [p for p in result.promotions if start <= p["promoted_at"] <= end + LEAD
                and (np.hypot(p["cells"]["x"].mean() - fx[0],
                              p["cells"]["y"].mean() - fy[0]) <= radius + 1000)]
    first_any = fire["acq_datetime"].min() if len(fire) else None
    first_hit = hit["acq_datetime"].min() if len(hit) else None
    example = hit.iloc[0] if len(hit) else (fire.iloc[0] if len(fire) else None)
    return {
        "event_id": row.name, "name": row["name"], "kind": row["kind"],
        "point": [lat, lon], "location_basis": row["location_basis"], "match_m": int(radius),
        "window_utc": [str(start), str(end)], "time_known": known,
        "outcome": outcome(fire),
        "detections": int(len(fire)),
        "by_road": {str(k): int(v) for k, v in fire["road"].value_counts().items()},
        "by_category": {str(k): int(v) for k, v in fire["category"].value_counts().items()},
        "alerts": {str(k): int(v) for k, v in fire["alert"].dropna().value_counts().items()},
        "watch": int(fire["reason"].fillna("").str.startswith("watch").sum()),
        "first_detection_h": hours(first_any, start) if known else None,
        "first_flag_h": hours(first_hit, start) if known else None,
        "first_detection_utc": str(first_any) if first_any is not None else None,
        "first_flag_utc": str(first_hit) if first_hit is not None else None,
        "flag_categories": {str(k): int(v) for k, v in hit["category"].value_counts().items()},
        # a location check, not a score: how far the nearest detection of the window was
        "nearest_in_window_m": round(float(during["dist_m"].min())) if len(during) else None,
        "max_frp": round(float(fire["frp"].max()), 1) if len(fire) else None,
        "instruments": sorted(fire["instrument"].astype(str).unique().tolist()),
        "reason": None if example is None else example["reason"],
        "promoted_after": [{"source_id": p["source_id"], "promoted_at": str(p["promoted_at"]),
                            "cls": p["cls"]} for p in promoted],
        "lead_flag_days": int(lead_days), "lead_detections": int(len(lead)),
        "chance": round(float(chance), 3),
        "nearest_source": nearest_source(lat, lon),
        "replay": {k: stats[k] for k in ("detections", "promotions") if k in stats},
    }


def summarise(results: list[dict], skipped: list[str]) -> dict:
    n = len(results)
    hits = [r for r in results if r["outcome"] == "flagged"]
    timed = [r["first_flag_h"] for r in hits if r["first_flag_h"] is not None]
    seen = [r["first_detection_h"] for r in results if r["first_detection_h"] is not None]
    by = pd.Series([r["outcome"] for r in results]).value_counts()
    return {
        "events_located": n, "events_not_located": skipped,
        "recall_flagged": round(len(hits) / n, 3) if n else None,
        "flagged": len(hits),
        "flagged_as_industrial": int(sum("industrial" in r["flag_categories"] for r in hits)),
        "expected_by_chance": round(sum(r["chance"] for r in results), 2),
        "signature": int(sum(r["detections"] > 0 for r in results)),
        "outcomes": {str(k): int(v) for k, v in by.items()},
        "time_to_flag_h": {"n": len(timed),
                           "mean": round(float(np.mean(timed)), 1) if timed else None,
                           "median": round(float(np.median(timed)), 1) if timed else None},
        "time_to_first_detection_h": {
            "n": len(seen), "mean": round(float(np.mean(seen)), 1) if seen else None,
            "median": round(float(np.median(seen)), 1) if seen else None},
    }


def table(results: list[dict]) -> str:
    lines = ["| Event | Kind | Outcome | Detections | Roads | First flag (h) | Chance |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        roads = ", ".join(f"{'ABC'[int(k) - 1]} {v}" for k, v in sorted(r["by_road"].items()))
        lines.append(f"| {r['event_id']} | {r['kind'].replace('_', ' ')} | {r['outcome']} | "
                     f"{r['detections']} | {roads or '-'} | "
                     f"{'' if r['first_flag_h'] is None else r['first_flag_h']} | "
                     f"{r['chance']:.2f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 10: per-fire recall.")
    ap.add_argument("--only", nargs="*", help="event ids to run (default: all located)")
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    events = pd.read_csv(EVENTS, dtype={"start_time_ist": str}).set_index("event_id")
    located = events[events["published_lat"].notna() & events["match_m"].notna()]
    skipped = sorted(set(events.index) - set(located.index))
    if args.only:
        located = located.loc[args.only]
    results = []
    for eid, row in located.iterrows():
        log.info("%s ...", eid)
        results.append(validate(row))
        r = results[-1]
        log.info("%s: %s (%d detections, chance %.2f)", eid, r["outcome"], r["detections"],
                 r["chance"])
    summary = summarise(results, skipped)
    OUT.mkdir(parents=True, exist_ok=True)
    if not args.only:
        (OUT / "events.json").write_text(
            json.dumps({"summary": summary, "events": results}, indent=2, default=str),
            encoding="utf-8")
        (OUT / "events_table.md").write_text(table(results), encoding="utf-8")
    print(table(results))
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
