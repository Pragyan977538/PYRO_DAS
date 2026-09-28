"""Stage 2: load everything into the database.

    python scripts/backfill.py                   # MOCK_MODE decides real vs fixture
    python scripts/backfill.py --years 2021 2023 # a range of archive years
    python scripts/backfill.py --only archive observability

Steps, in order; each skips what it has already loaded (see ingest_log):

  archive        FIRMS yearly India CSVs (public, no key), downloaded if missing
  api            FIRMS API for what the archive lacks: needs FIRMS_MAP_KEY
  vnf            VIIRS Nightfire temperatures: optional, licence-gated
  gihs           GIHS reference sources, evaluation only (real data only)
  osm            labelled OSM industry from the Geofabrik extract
  observability  daily cloud amount (NASA POWER), or the fixture's cloud table
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import ConfigError, settings  # noqa: E402

STEPS = ["archive", "api", "vnf", "gihs", "osm", "observability"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 2 backfill.")
    ap.add_argument("--years", type=int, nargs=2, metavar=("FIRST", "LAST"),
                    help="archive years, inclusive (default: 2012 to last year)")
    ap.add_argument("--only", nargs="+", choices=STEPS, default=STEPS)
    ap.add_argument("--skip", nargs="+", choices=STEPS, default=[])
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")

    cfg = settings()
    first, last = args.years or (2012, date.today().year - 1)
    years = list(range(first, last + 1))
    steps = [s for s in args.only if s not in args.skip]
    summary: dict = {"mock": cfg.mock_mode, "years": [first, last]}

    if "archive" in steps:
        from firewatch.ingest.firms_archive import load_archive
        loaded = load_archive(years)
        summary["archive"] = {"files": len(loaded), "new_rows": sum(loaded.values())}

    if "api" in steps:
        from firewatch.ingest.firms_api import FirmsApi, MockFirmsApi
        try:
            if cfg.mock_mode:   # the fixture's own NRT window, per product
                mock = MockFirmsApi()
                summary["api"] = {p: mock.backfill(p, *mock.window(p))
                                  for p in MockFirmsApi.FILES}
            else:               # everything after the newest archive year
                client = FirmsApi()
                start = date(last + 1, 1, 1)
                summary["api"] = {p: client.backfill(p, start, date.today())
                                  for p in ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT",
                                            "MODIS_NRT")}
        except ConfigError as exc:
            summary["api"] = f"skipped: {exc}"

    if "vnf" in steps:
        from firewatch.ingest.vnf import load_vnf
        summary["vnf"] = load_vnf() or "skipped: no VNF data"

    if "gihs" in steps and not cfg.mock_mode:
        from firewatch.ingest.gihs import load_gihs
        summary["gihs"] = load_gihs()

    if "osm" in steps:
        from firewatch.ingest.osm import load_osm
        summary["osm"] = load_osm()

    if "observability" in steps:
        from firewatch.ingest.observability import load_observability
        summary["observability"] = load_observability(years)

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
