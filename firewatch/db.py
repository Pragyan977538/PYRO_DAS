"""Database access.

One pooled engine for the process, a context manager that commits or rolls back,
and a bulk upsert built on ``psycopg2.extras.execute_values``.

The upsert matters more than it looks. Backfilling ten years of FIRMS means
re-running the same date ranges after a crash or a rate-limit pause, so every
write has to be idempotent. ``ON CONFLICT DO UPDATE`` on a natural key makes a
re-run cheap instead of producing duplicates that would corrupt every downstream
baseline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from firewatch.config import settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


def engine() -> Engine:
    """Pooled engine, created once per process."""
    global _engine
    if _engine is None:
        cfg = settings()
        _engine = create_engine(
            cfg.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,  # survive the DB container restarting under us
            future=True,
            # Every date here is a UTC date. The database default is set too
            # (003_ingest.sql), but that only reaches new sessions; pinning it per
            # connection holds on any server, whatever zone it inherited.
            connect_args={"options": "-c timezone=UTC"},
        )
        log.debug("engine created for %s", cfg.database_url.split("@")[-1])
    return _engine


@contextmanager
def get_conn() -> Iterator[Any]:
    """A raw DBAPI connection, committed on success and rolled back on error.

    Yields the psycopg2 connection rather than a SQLAlchemy one because
    ``execute_values`` and ``COPY`` both need the DBAPI object directly, and
    those are the two operations that carry the bulk of the ingest load.
    """
    conn = engine().raw_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bulk_upsert(
    table: str,
    rows: Sequence[dict[str, Any]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    page_size: int = 5_000,
) -> int:
    """Insert ``rows`` into ``table``, updating on conflict. Returns rows sent.

    ``update_cols`` defaults to every column that is not part of the conflict
    key. Pass an empty sequence to get ``DO NOTHING`` semantics instead.
    """
    if not rows:
        return 0

    cols = list(rows[0].keys())
    missing = [c for c in conflict_cols if c not in cols]
    if missing:
        raise ValueError(f"conflict columns absent from rows: {missing}")

    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]

    col_sql = ", ".join(f'"{c}"' for c in cols)
    conflict_sql = ", ".join(f'"{c}"' for c in conflict_cols)

    if update_cols:
        assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
        action = f"DO UPDATE SET {assignments}"
    else:
        action = "DO NOTHING"

    sql = (
        f'INSERT INTO {table} ({col_sql}) VALUES %s '
        f"ON CONFLICT ({conflict_sql}) {action}"
    )
    values = [tuple(r.get(c) for c in cols) for r in rows]

    with get_conn() as conn, conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=page_size)

    log.info("upserted %d rows into %s", len(values), table)
    return len(values)


def run_sql_file(path: str | Path) -> None:
    """Execute a .sql file as a single transaction.

    Sent whole rather than split on semicolons: the schema contains function
    bodies and dollar-quoted strings that a naive split would mangle.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"SQL file not found: {path}")

    sql = path.read_text(encoding="utf-8")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
    log.info("applied %s", path.name)


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a SELECT and return a list of dicts. For small result sets only."""
    with engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()]


def ping() -> bool:
    """True if the database answers. Used by health checks and tests."""
    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - diagnostic, not control flow
        log.warning("database unreachable: %s", exc)
        return False
