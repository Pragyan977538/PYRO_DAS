"""Write the synthetic test fixture to disk (MOCK_MODE=1's data).

    python scripts/generate_fixture.py [--start 2022-01-01] [--end 2023-12-31]
                                       [--seed 7] [--out data/mock]

Files are laid out like the real data: FIRMS archive CSVs under firms/, NRT API
CSVs under firms_nrt/, VNF under vnf/, the ERA5-shaped cloud table under
observability/, and the ground truth under truth/.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.ingest.fixture import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    generate,
    summarise,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Write the synthetic test fixture.")
    ap.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    ap.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None,
                    help="output directory (default: MOCK dir from config)")
    args = ap.parse_args()

    out = args.out or settings().mock_dir
    fx = generate(args.start, args.end, args.seed)
    written = fx.write(out)
    print(json.dumps(summarise(fx), indent=2, default=str))
    print(f">> wrote {len(written)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
