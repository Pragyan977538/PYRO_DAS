"""Stage 2 acceptance: the ROADMAP's checks, run against the database.

    python scripts/check_ingest.py

Prints each check and exits non-zero if a hard one fails. The day/night split and
VNF coverage are recorded, not asserted: they are facts about the data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.db import fetch_all  # noqa: E402

CHECKS = {
    "by_instrument_product": """
        SELECT instrument, product, count(*) AS n,
               min(acq_datetime)::text AS first, max(acq_datetime)::text AS last
          FROM detections GROUP BY 1, 2 ORDER BY 1, 2""",
    # No NRT row may share a UTC sensor-day with SP.
    "nrt_on_sp_days": """
        SELECT count(*) AS n FROM detections n
         WHERE n.product = 'NRT' AND EXISTS (
               SELECT 1 FROM detections s
                WHERE s.product = 'SP' AND s.sensor = n.sensor
                  AND s.acq_datetime::date = n.acq_datetime::date)""",
    "daynight": "SELECT daynight, count(*) AS n FROM detections GROUP BY 1 ORDER BY 1",
    "vnf_coverage": """
        SELECT count(*) FILTER (WHERE vnf_temp_k IS NOT NULL) AS with_temp,
               count(*) AS total FROM detections""",
    "observability": "SELECT source, count(*) AS n FROM observability GROUP BY 1",
    "osm_groups": "SELECT label_group, count(*) AS n FROM osm_industrial GROUP BY 1 ORDER BY 1",
    "gihs": "SELECT count(*) AS n, count(*) FILTER (WHERE confirmed) AS confirmed "
            "FROM gihs_reference",
    "timezone": "SELECT current_setting('TimeZone') AS tz",
}


def main() -> int:
    results = {name: fetch_all(sql) for name, sql in CHECKS.items()}
    print(json.dumps(results, indent=2, default=str))
    failures = []
    if not results["by_instrument_product"]:
        failures.append("no detections loaded")
    if results["nrt_on_sp_days"][0]["n"]:
        failures.append(f"{results['nrt_on_sp_days'][0]['n']} NRT rows on SP-covered days")
    if not results["observability"]:
        failures.append("observability is empty")
    if results["timezone"][0]["tz"] not in ("UTC", "Etc/UTC"):
        failures.append(f"database timezone is {results['timezone'][0]['tz']}, not UTC")
    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
