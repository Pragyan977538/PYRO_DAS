"""Stage 6: assemble the latest inference run's detections into events.

    python scripts/build_events.py            # the latest run's window
    python scripts/build_events.py --run 2    # a given run
    python scripts/build_events.py --dry      # assemble and report; write nothing

Replaces the window's events and points each routed detection (Road A and C) at
its event. Lifecycle is judged as of the window's end.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.inference import events as ev  # noqa: E402

log = logging.getLogger("build_events")


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 6 event assembly.")
    ap.add_argument("--run", type=int, help="inference run id (default: the latest)")
    ap.add_argument("--dry", action="store_true", help="assemble and report; write nothing")
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    t0 = time.time()
    det, provisional, promotions, cells, run = ev.read_run(args.run)
    log.info("run %s: %d routed detections (Roads A and C), %d provisional sources; %.0fs",
             run["run_id"], len(det), len(provisional), time.time() - t0)
    events, assigned = ev.assemble(det, provisional, promotions, cells,
                                   as_of=run["window_end"])
    stats = ev.summarise(events)
    log.info("assembled in %.0fs", time.time() - t0)
    if not args.dry:
        stats.update(ev.write(events, assigned, run))
        log.info("written in %.0fs", time.time() - t0)
    print(ev.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
