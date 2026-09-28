"""Apply the SQL migrations in sql/ in order.

Both task runners call this rather than an inline ``python -c``, so quoting is
identical under bash, Windows PowerShell 5.1 and PowerShell 7.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.db import run_sql_file  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=">> %(message)s")
    files = sorted((REPO / "sql").glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        logging.error("no migrations found in %s", REPO / "sql")
        return 1
    for path in files:
        run_sql_file(path)
    logging.info("migration complete: %d files", len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
