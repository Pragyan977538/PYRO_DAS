"""Stage 9: build the map's offline basemap (Natural Earth, India point of view).

    python scripts/build_basemap.py            # once; ~20 MB download, ~2 MB result
    python scripts/build_basemap.py --force    # rebuild

Writes DATA_DIR/basemap/{countries,states,rivers,places}.geojson, which the API
serves at /basemap/. After this, the map needs no network at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.ingest.basemap import build  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the offline basemap.")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format=">> %(message)s")
    print(json.dumps(build(force=args.force)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
