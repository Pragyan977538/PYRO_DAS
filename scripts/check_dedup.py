"""Stage 6 acceptance: one real fire => exactly one event row.

    python scripts/check_dedup.py

For every event in reference/verified_events.csv: replay inference over its
window around it (dry, nothing written), assemble events, and check that the
fire's detections -- within the event's radius, between its published dates --
all belong to exactly one event. Two events for one fire would be two alerts for
one incident; a detection with no event would be a fire called normal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from run_inference import run  # noqa: E402

from firewatch.grid import to_metres  # noqa: E402
from firewatch.inference import events as ev  # noqa: E402
from firewatch.inference.engine import read_window  # noqa: E402
from firewatch.registry.cells import add_metres  # noqa: E402

OUT = REPO / "reports" / "stage6"
EVENTS = REPO / "reference" / "verified_events.csv"
HALF_DEG = 0.3
LEAD = pd.Timedelta(days=60)      # replay from before the fire, so history is realistic


def check(row) -> dict:
    lat, lon = float(row["fire_lat"]), float(row["fire_lon"])
    start = pd.Timestamp(row["start"], tz="UTC")
    end = pd.Timestamp(row["end"], tz="UTC") + pd.Timedelta(days=1)
    window = ((start - LEAD).normalize(), (end + pd.Timedelta(days=30)).normalize())
    bbox = (lon - HALF_DEG, lat - HALF_DEG, lon + HALF_DEG, lat + HALF_DEG)
    result, _ = run(*window, dry=True, bbox=bbox)
    det = read_window(*window, bbox).merge(result.detections, on="detection_id")
    det["source_id"] = pd.to_numeric(det["source_id"], errors="coerce")
    det["road"] = det["road"].astype(int)
    add_metres(det)
    promotions = pd.DataFrame([{"source_id": p["source_id"], "first_seen": p["first_day"],
                                "promoted_at": p["promoted_at"]} for p in result.promotions],
                              columns=["source_id", "first_seen", "promoted_at"])
    cells = pd.concat([p["cells"].assign(source_id=p["source_id"]) for p in result.promotions],
                      ignore_index=True) if result.promotions else \
        pd.DataFrame(columns=["source_id", "x", "y"])
    retired = {r["source_id"]: r["retired_at"] for r in result.retirements}
    provisional = {p["source_id"]: retired.get(p["source_id"]) for p in result.promotions}
    events, assigned = ev.assemble(det, provisional, promotions, cells, as_of=window[1])
    det = det.merge(assigned, on="detection_id", how="left")

    fx, fy = to_metres([lat], [lon])
    near = np.hypot(det["x"] - fx[0], det["y"] - fy[0]) <= float(row["radius_m"])
    burning = (det["acq_datetime"] >= start) & (det["acq_datetime"] < end)
    fire = det[near & burning]
    ids = fire["event"].dropna().astype(int).unique()
    main = events.set_index("event").loc[ids] if len(ids) else events.iloc[0:0]
    return {
        "event_id": row.name, "name": row["name"],
        "fire_detections": int(len(fire)),
        "without_event": int(fire["event"].isna().sum()),
        "events": int(len(ids)),
        "event_rows": main.reset_index()[["event", "kind", "status", "first_seen", "last_seen",
                                           "n_detections", "reason"]].to_dict("records"),
        "passed": bool(len(ids) == 1 and not fire["event"].isna().any()),
    }


def main() -> int:
    verified = pd.read_csv(EVENTS).set_index("event_id")
    results = [check(row) for _, row in verified.iterrows()]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dedup.json").write_text(json.dumps(results, indent=2, default=str),
                                    encoding="utf-8")
    print(json.dumps(results, indent=2, default=str))
    failed = [r["event_id"] for r in results if not r["passed"]]
    for f in failed:
        print(f"FAIL: {f} is not exactly one event")
    print("PASS" if not failed else f"{len(failed)} of {len(results)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
