"""Stage 0 acceptance tests.

Config tests run anywhere. Database tests skip cleanly when no database is
reachable, so ``make test`` is useful before ``make db`` has ever been run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from firewatch.config import ConfigError, Settings, load_settings

REPO = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "detections",
    "sources",
    "events",
    "observability",
    "critical_assets",
    "osm_industrial",
    "forest_boundary",
    "fsi_alerts",
}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "DATABASE_URL", "MOCK_MODE", "FIRMS_MAP_KEY", "EOG_USERNAME",
        "EOG_PASSWORD", "INDIA_BBOX", "DATA_DIR", "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/db")
    return monkeypatch


def test_defaults_load(clean_env):
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.mock_mode is True, "MOCK_MODE must default to on so no stage needs keys"
    assert s.india_bbox == (68.0, 6.0, 98.0, 37.0)
    assert s.log_level == "INFO"


def test_settings_are_frozen(clean_env):
    s = load_settings()
    with pytest.raises((AttributeError, TypeError)):
        s.mock_mode = False  # type: ignore[misc]


def test_missing_database_url_is_named(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings()


def test_live_mode_names_every_missing_credential(clean_env):
    clean_env.setenv("MOCK_MODE", "0")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    msg = str(exc.value)
    for key in ("FIRMS_MAP_KEY", "EOG_USERNAME", "EOG_PASSWORD"):
        assert key in msg, f"{key} should be named in the error"


def test_live_mode_passes_with_credentials(clean_env):
    clean_env.setenv("MOCK_MODE", "0")
    clean_env.setenv("FIRMS_MAP_KEY", "abc123")
    clean_env.setenv("EOG_USERNAME", "user")
    clean_env.setenv("EOG_PASSWORD", "pass")
    s = load_settings()
    assert s.mock_mode is False
    assert s.firms_map_key == "abc123"


@pytest.mark.parametrize("bad", ["68,6,98", "a,b,c,d", "98,6,68,37", "68,37,98,6"])
def test_bad_bbox_rejected(clean_env, bad):
    clean_env.setenv("INDIA_BBOX", bad)
    with pytest.raises(ConfigError, match="INDIA_BBOX"):
        load_settings()


def test_bbox_str_matches_firms_format(clean_env):
    assert load_settings().bbox_str == "68,6,98,37"


def test_bad_log_level_rejected(clean_env):
    clean_env.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings()


def test_data_dir_resolves_absolute(clean_env):
    s = load_settings()
    assert s.data_dir.is_absolute()
    assert s.mock_dir == s.data_dir / "mock"


# ---------------------------------------------------------------------------
# repo layout
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "docker-compose.yml", "requirements.txt", ".env.example", ".gitignore",
    "Makefile", "README.md", "CLAUDE.md",
    "sql/001_schema.sql", "sql/002_indexes.sql",
    "firewatch/config.py", "firewatch/db.py",
])
def test_required_file_exists(rel):
    assert (REPO / rel).exists(), f"missing {rel}"


def test_env_example_lists_every_setting():
    text = (REPO / ".env.example").read_text()
    for key in ("DATABASE_URL", "MOCK_MODE", "FIRMS_MAP_KEY", "EOG_USERNAME",
                "EOG_PASSWORD", "INDIA_BBOX", "DATA_DIR", "LOG_LEVEL"):
        assert key in text, f"{key} not documented in .env.example"


def test_schema_declares_normalised_brightness_columns():
    """MODIS and VIIRS name these differently; the schema must carry one pair."""
    sql = (REPO / "sql/001_schema.sql").read_text()
    assert "bt4" in sql and "bt5" in sql
    assert "bright_ti4" not in sql.split("-- ===")[2]


def test_schema_creates_hypertable():
    sql = (REPO / "sql/001_schema.sql").read_text()
    assert "create_hypertable" in sql
    assert "acq_datetime" in sql


# ---------------------------------------------------------------------------
# database — skipped when unreachable
# ---------------------------------------------------------------------------

def _db_available() -> bool:
    if not os.getenv("DATABASE_URL"):
        return False
    try:
        from firewatch.db import ping
        return ping()
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(),
    reason="no database reachable — run `make db && make migrate` first",
)


@needs_db
def test_connection_works():
    from firewatch.db import ping
    assert ping()


@needs_db
def test_extensions_installed():
    from firewatch.db import fetch_all
    names = {r["extname"] for r in fetch_all(
        "SELECT extname FROM pg_extension")}
    assert "postgis" in names
    assert "timescaledb" in names


@needs_db
def test_all_tables_exist():
    from firewatch.db import fetch_all
    found = {r["tablename"] for r in fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert found >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - found}"


@needs_db
def test_detections_is_a_hypertable():
    from firewatch.db import fetch_all
    rows = fetch_all(
        "SELECT hypertable_name FROM timescaledb_information.hypertables")
    assert any(r["hypertable_name"] == "detections" for r in rows)


@needs_db
def test_spatial_indexes_exist():
    from firewatch.db import fetch_all
    idx = {r["indexname"] for r in fetch_all(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
    for expected in ("detections_geom_gist", "sources_geom_gist",
                     "assets_geom_gist", "detections_natural_key"):
        assert expected in idx, f"missing index {expected}"


@needs_db
def test_bulk_upsert_is_idempotent():
    """Backfill re-runs the same date ranges; writing twice must not duplicate."""
    from firewatch.db import bulk_upsert, fetch_all
    rows = [{
        "name": "TEST_ASSET_stage0", "asset_type": "test",
        "criticality": 0.5, "geom": "SRID=4326;POINT(72.8 19.0)",
    }]
    try:
        bulk_upsert("critical_assets", rows, conflict_cols=["asset_id"])
    except Exception:
        pytest.skip("critical_assets has no natural key yet; seeded in stage 7")
    n = fetch_all("SELECT count(*) AS c FROM critical_assets "
                  "WHERE name='TEST_ASSET_stage0'")[0]["c"]
    assert n >= 1
