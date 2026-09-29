"""Shared test machinery: throwaway databases in mock mode.

Each database-backed module gets its own database, created fresh, migrated twice
(every migration must be safe to re-run), pointed at a freshly written fixture,
and dropped afterwards. The real archive in the main database is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from firewatch import config, db
from firewatch.ingest.fixture import generate

REPO = Path(__file__).resolve().parent.parent


def _admin_connect(url):
    import psycopg2
    return psycopg2.connect(dbname="postgres", user=url.username, password=url.password,
                            host=url.host, port=url.port or 5432)


def postgres_reachable() -> bool:
    try:
        from sqlalchemy.engine import make_url
        url = make_url(config.load_settings().database_url)
        _admin_connect(url).close()
        return True
    except Exception:
        return False


def fresh_database(name: str, tmp_path_factory) -> Iterator[Path]:
    """Create database ``name``, write the fixture, switch to mock mode, migrate
    twice; yield the data directory; drop everything afterwards."""
    from sqlalchemy.engine import make_url

    base = make_url(config.load_settings().database_url)
    admin = _admin_connect(base)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {name}")

    data_dir = tmp_path_factory.mktemp(name)
    generate().write(data_dir / "mock")
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", base.set(database=name).render_as_string(hide_password=False))
    mp.setenv("MOCK_MODE", "1")
    mp.setenv("DATA_DIR", str(data_dir))
    config._cached = None
    db._engine = None
    try:
        for _ in range(2):   # twice: every migration must be safe to re-run
            for path in sorted((REPO / "sql").glob("[0-9][0-9][0-9]_*.sql")):
                db.run_sql_file(path)
        yield data_dir
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        config._cached = None
        mp.undo()
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
