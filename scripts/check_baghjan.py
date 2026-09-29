"""Stage 5 regression test: the Baghjan blowout must never be "normal".

    python scripts/check_baghjan.py

Baghjan well No. 5 (Tinsukia, Assam) blew out on 27 May 2020, burned from 9 June
and was killed on 15 November. Replays April to December 2020 around it --
dry, nothing written -- with baselines as of 1 April, and asserts that no
detection of the fire is ever routed to Road B. Also reports when it was
promoted to a provisional source and what the fire's detections were called
before that.

The event comes from reference/verified_events.csv; the fire is located from
the satellite record (the published point is a neighbouring persistent flare).
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
from firewatch.inference import ROAD_B, ROAD_C  # noqa: E402
from firewatch.inference.engine import read_window  # noqa: E402

OUT = REPO / "reports" / "stage5"
EVENTS = REPO / "reference" / "verified_events.csv"
REPLAY = (pd.Timestamp("2020-04-01", tz="UTC"), pd.Timestamp("2021-01-01", tz="UTC"))
HALF_DEG = 0.3


def main() -> int:
    ev = pd.read_csv(EVENTS).set_index("event_id").loc["BAGHJAN-2020"]
    lat, lon = float(ev["fire_lat"]), float(ev["fire_lon"])
    bbox = (lon - HALF_DEG, lat - HALF_DEG, lon + HALF_DEG, lat + HALF_DEG)
    result, stats = run(*REPLAY, dry=True, bbox=bbox)
    det = read_window(*REPLAY, bbox).merge(result.detections, on="detection_id")
    x, y = to_metres(det["latitude"].to_numpy(), det["longitude"].to_numpy())
    fx, fy = to_metres([lat], [lon])
    near = np.hypot(x - fx[0], y - fy[0]) <= float(ev["radius_m"])
    day = det["acq_datetime"].dt.tz_convert("UTC").dt.normalize()
    burning = (day >= pd.Timestamp(ev["start"], tz="UTC")) & \
              (day <= pd.Timestamp(ev["end"], tz="UTC"))
    fire = det[near & burning].sort_values("acq_datetime")

    promoted = [p for p in result.promotions
                if np.hypot(*(np.array(to_metres([lat], [lon])).ravel()
                              - np.array([p["cells"]["x"].mean(), p["cells"]["y"].mean()]))) < 3000]
    first_promotion = min((p["promoted_at"] for p in promoted), default=None)
    after = fire[fire["acq_datetime"] >= first_promotion] if first_promotion is not None \
        else fire.iloc[0:0]
    report = {
        "event": ev["name"], "fire_point": [lat, lon], "radius_m": int(ev["radius_m"]),
        "burning": [ev["start"], ev["end"]],
        "fire_detections": int(len(fire)),
        "by_road": {str(k): int(v) for k, v in fire["road"].value_counts().items()},
        "by_alert": {str(k): int(v) for k, v in fire["alert"].value_counts().items()},
        "road_a_classes": {str(k): int(v) for k, v in
                           fire.loc[fire["road"] == 1, "pred_class"].value_counts().items()},
        "first_detection": str(fire["acq_datetime"].min()),
        "promoted_at": str(first_promotion),
        "days_fire_to_promotion": (first_promotion - pd.Timestamp(ev["start"], tz="UTC")).days
        if first_promotion is not None else None,
        "road_c_after_promotion": round(float((after["road"] == ROAD_C).mean()), 4)
        if len(after) else None,
        "road_b": int((fire["road"] == ROAD_B).sum()),
        "example_reasons": fire.drop_duplicates("road")["reason"].tolist(),
        "replay": stats,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "baghjan.json").write_text(json.dumps(report, indent=2, default=str),
                                      encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "replay"}, indent=2, default=str))
    failures = []
    if not len(fire):
        failures.append("no detections of the fire: is 2020 loaded?")
    if report["road_b"]:
        failures.append(f"{report['road_b']} fire detections routed to Road B")
    if first_promotion is None:
        failures.append("the fire was never promoted to a provisional source")
    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
