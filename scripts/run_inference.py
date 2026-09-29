"""Stage 5: route detections through Roads A, B and C, and write the result.

    python scripts/run_inference.py                         # replay the latest year
    python scripts/run_inference.py --start 2024-06-01 --end 2024-07-01
    python scripts/run_inference.py --dry                   # compute, write nothing

Live, this runs every three hours on the newest NRT batch. Without a FIRMS key
there is no live feed, so the default replays the latest full year of the
archive as if it were arriving: in time order, baselines as of the window's
start, promotion in simulated time. A rerun of the same window replaces the last.

The registry was built from every year, the replayed one included; with a
three-year gate few sources depend on it, but the replay is not blind to it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.db import fetch_all  # noqa: E402
from firewatch.inference import engine as eng  # noqa: E402
from firewatch.inference.anomaly import Thresholds  # noqa: E402
from firewatch.inference.road_a import Context  # noqa: E402
from firewatch.inference.router import Router  # noqa: E402
from firewatch.registry.cells import utc_day  # noqa: E402

log = logging.getLogger("run_inference")


def latest_year() -> tuple[pd.Timestamp, pd.Timestamp]:
    last = fetch_all("SELECT max(acq_datetime) AS t FROM detections")[0]["t"]
    year = pd.Timestamp(last).year
    if pd.Timestamp(last).month < 12:
        year -= 1
    return pd.Timestamp(year, 1, 1, tz="UTC"), pd.Timestamp(year + 1, 1, 1, tz="UTC")


def run(start: pd.Timestamp, end: pd.Timestamp, dry: bool = False,
        bbox: tuple[float, float, float, float] | None = None) -> tuple[eng.Result, dict]:
    t0 = time.time()
    if not dry:
        eng.clear_window(start, end)
    router = Router.from_db(as_of=start)
    base = eng.baselines_as_of(start)
    meta = eng.source_meta()
    context = Context.from_db()
    th = Thresholds.from_settings()
    new_id = eng.dry_id_allocator() if dry else eng.db_id_allocator()
    engine = eng.Engine(router, base, meta, context, th, new_id,
                        start_day=int(utc_day(pd.DatetimeIndex([start]))[0]),
                        clear=eng.ClearSky.from_db(start, end))
    det = eng.read_window(start, end, bbox)
    log.info("%d detections in %s .. %s; %d sources routed, %d with baselines; %.0fs",
             len(det), start.date(), end.date(), router.cells["source_id"].nunique(),
             len(base), time.time() - t0)
    result = engine.run(det)
    stats = eng.summarise(result)
    log.info("inference done in %.0fs", time.time() - t0)
    if not dry:
        stats["run_id"] = eng.write(result, start, end, th)
        log.info("written in %.0fs", time.time() - t0)
    return result, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 5 inference.")
    ap.add_argument("--start", help="UTC date, inclusive (default: latest full year)")
    ap.add_argument("--end", help="UTC date, exclusive")
    ap.add_argument("--dry", action="store_true", help="compute and report; write nothing")
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    start, end = latest_year()
    if args.start:
        start = pd.Timestamp(args.start, tz="UTC")
    if args.end:
        end = pd.Timestamp(args.end, tz="UTC")
    _, stats = run(start, end, dry=args.dry)
    print(json.dumps(stats, indent=2, default=str))
    if stats["road_a_without_reason"]:
        print(f"FAIL: {stats['road_a_without_reason']} Road A detections without a reason")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
