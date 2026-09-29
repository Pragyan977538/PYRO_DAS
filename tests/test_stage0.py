"""Stage 0 acceptance tests.

Config tests run anywhere. Database tests skip cleanly when no database is
reachable, so ``make test`` is useful before ``make db`` has ever been run.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
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
        "EOG_PASSWORD", "INDIA_BBOX", "DATA_DIR", "LOG_LEVEL", "REGISTRY_GATE",
        "ANOMALY_EXTREME",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/db")
    return monkeypatch


def test_defaults_load(clean_env):
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.mock_mode is False, "real data is the default; MOCK_MODE=1 is the fixture"
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


def test_real_data_needs_no_credentials_to_start(clean_env):
    """The FIRMS archive is public, so nothing may demand a key at startup."""
    clean_env.setenv("MOCK_MODE", "0")
    s = load_settings()
    assert s.mock_mode is False
    assert s.firms_map_key is None


def test_firms_key_demanded_only_when_used(clean_env):
    s = load_settings()
    with pytest.raises(ConfigError, match="FIRMS_MAP_KEY"):
        s.require_firms_key()


def test_firms_key_returned_when_set(clean_env):
    clean_env.setenv("FIRMS_MAP_KEY", "abc123")
    assert load_settings().require_firms_key() == "abc123"


def test_vnf_is_optional(clean_env):
    assert load_settings().vnf_enabled is False
    clean_env.setenv("EOG_USERNAME", "user")
    clean_env.setenv("EOG_PASSWORD", "pass")
    assert load_settings().vnf_enabled is True


@pytest.mark.parametrize(("present", "missing"), [
    ("EOG_USERNAME", "EOG_PASSWORD"),
    ("EOG_PASSWORD", "EOG_USERNAME"),
])
def test_half_an_eog_login_is_named(clean_env, present, missing):
    """Half a login is a typo, not a choice to skip VNF."""
    clean_env.setenv(present, "x")
    with pytest.raises(ConfigError, match=missing):
        load_settings()


@pytest.mark.parametrize("bad", ["68,6,98", "a,b,c,d", "98,6,68,37", "68,37,98,6"])
def test_bad_bbox_rejected(clean_env, bad):
    clean_env.setenv("INDIA_BBOX", bad)
    with pytest.raises(ConfigError, match="INDIA_BBOX"):
        load_settings()


def test_registry_gate_default_and_override(clean_env):
    assert load_settings().registry_gate == (3, 10, 3)
    clean_env.setenv("REGISTRY_GATE", "4, 20, 3")
    assert load_settings().registry_gate == (4, 20, 3)


@pytest.mark.parametrize("bad", ["3,10", "a,b,c", "13,10,2", "3,10,0"])
def test_bad_registry_gate_rejected(clean_env, bad):
    clean_env.setenv("REGISTRY_GATE", bad)
    with pytest.raises(ConfigError, match="REGISTRY_GATE"):
        load_settings()


def test_anomaly_extreme_default_and_guard(clean_env):
    assert load_settings().anomaly_extreme == (7.0, 6.0)
    for bad in ("3,2", "8", "x,y"):
        clean_env.setenv("ANOMALY_EXTREME", bad)
        with pytest.raises(ConfigError, match="ANOMALY_EXTREME"):
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
    "Makefile", "make.ps1", "README.md", "CLAUDE.md",
    "sql/001_schema.sql", "sql/002_indexes.sql", "scripts/migrate.py",
    "firewatch/config.py", "firewatch/db.py", "firewatch/ingest/normalize.py",
])
def test_required_file_exists(rel):
    assert (REPO / rel).exists(), f"missing {rel}"


@pytest.mark.parametrize("rel", ["make.ps1", "scripts/local_postgres.ps1"])
def test_powershell_scripts_are_plain_ascii(rel):
    """PowerShell 5.1 reads a BOM-less script as ANSI; non-ASCII can break parsing.
    Control characters are refused too: a stray backspace from an escaped '\\b'
    once turned 'scripts\\backfill.py' into a path that does not exist."""
    data = (REPO / rel).read_bytes()
    bad = [i for i, b in enumerate(data) if b > 127 or (b < 32 and b not in (9, 10, 13))]
    assert not bad, f"non-printable byte {data[bad[0]]:#x} in {rel} at offset {bad[0]}"


def test_task_runners_have_the_same_targets():
    make_targets = set(re.findall(r"^([a-z]+):", (REPO / "Makefile").read_text(), re.M))
    ps_targets = set(re.findall(r"^\s+'([a-z]+)' \{", (REPO / "make.ps1").read_text(), re.M))
    assert make_targets == ps_targets, make_targets ^ ps_targets


def test_env_example_lists_every_setting():
    text = (REPO / ".env.example").read_text()
    for key in ("DATABASE_URL", "MOCK_MODE", "FIRMS_MAP_KEY", "EOG_USERNAME",
                "EOG_PASSWORD", "INDIA_BBOX", "DATA_DIR", "LOG_LEVEL", "REGISTRY_GATE",
                "ANOMALY_EXTREME"):
        assert key in text, f"{key} not documented in .env.example"


def _detections_ddl() -> str:
    sql = (REPO / "sql/001_schema.sql").read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE IF NOT EXISTS detections \((.*?)\n\);", sql, re.S)
    assert match, "detections table not found in 001_schema.sql"
    return match.group(1)


def test_schema_declares_normalised_brightness_columns():
    """MODIS and VIIRS name these differently; the schema must carry one pair."""
    ddl = _detections_ddl()
    assert re.search(r"^\s+bt4\s", ddl, re.M) and re.search(r"^\s+bt5\s", ddl, re.M)
    for raw in ("bright_ti4", "bright_ti5", "brightness", "bright_t31"):
        assert raw not in ddl


@pytest.mark.parametrize("col", ["instrument", "product", "version", "firms_type"])
def test_schema_declares_provenance_columns(col):
    """Needed to key baselines by instrument, dedupe SP/NRT, and evaluate vs FIRMS."""
    assert re.search(rf"^\s+{col}\s", _detections_ddl(), re.M), f"detections.{col}"


def test_timescaledb_is_optional():
    """Native Postgres + PostGIS must migrate cleanly without the extension."""
    sql = (REPO / "sql/001_schema.sql").read_text(encoding="utf-8")
    assert "create_hypertable" in sql
    assert not re.search(r"^CREATE EXTENSION[^;]*timescaledb", sql, re.M), \
        "timescaledb must be created inside a guarded DO block, not at top level"
    assert "EXCEPTION WHEN OTHERS" in sql


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


def _has_timescaledb() -> bool:
    from firewatch.db import fetch_all
    return bool(fetch_all("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))


@needs_db
def test_extensions_installed():
    """PostGIS is required; TimescaleDB is optional."""
    from firewatch.db import fetch_all
    names = {r["extname"] for r in fetch_all(
        "SELECT extname FROM pg_extension")}
    assert "postgis" in names


@needs_db
def test_all_tables_exist():
    from firewatch.db import fetch_all
    found = {r["tablename"] for r in fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert found >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - found}"


@needs_db
def test_detections_is_a_hypertable_when_timescaledb_present():
    if not _has_timescaledb():
        pytest.skip("TimescaleDB not installed; detections is a plain table by design")
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


TEST_SENSOR = "TEST_STAGE0"


def _detection(**overrides) -> dict:
    row = {
        "acq_datetime": datetime(2023, 1, 1, 7, 45, tzinfo=UTC),
        "latitude": 22.3, "longitude": 69.9,
        "geom": "SRID=4326;POINT(69.9 22.3)",
        "sensor": TEST_SENSOR, "instrument": "VIIRS", "satellite": "N",
        "product": "SP", "version": "2", "daynight": "D", "frp": 10.0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def clean_test_rows():
    from firewatch.db import get_conn

    def purge():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM detections WHERE sensor = %s", (TEST_SENSOR,))

    purge()
    yield
    purge()


@needs_db
def test_bulk_upsert_is_idempotent(clean_test_rows):
    """Backfill re-runs the same date ranges; writing twice must not duplicate.

    Uses the detections natural key -- the idempotency the backfill depends on.
    The second write carries a new FRP, so the update path is exercised too.
    """
    from firewatch.db import bulk_upsert, fetch_all
    key = ["sensor", "acq_datetime", "latitude", "longitude"]
    bulk_upsert("detections", [_detection()], conflict_cols=key)
    bulk_upsert("detections", [_detection(frp=99.0)], conflict_cols=key)
    rows = fetch_all("SELECT frp FROM detections WHERE sensor = :s", {"s": TEST_SENSOR})
    assert len(rows) == 1
    assert rows[0]["frp"] == pytest.approx(99.0)


@needs_db
def test_supersede_nrt_deletes_only_covered_nrt(clean_test_rows):
    from firewatch.db import bulk_upsert, fetch_all
    from firewatch.ingest.normalize import supersede_nrt
    key = ["sensor", "acq_datetime", "latitude", "longitude"]
    bulk_upsert("detections", [
        _detection(),
        _detection(product="NRT", version="2.0NRT", latitude=22.31),
        _detection(product="NRT", version="2.0NRT",
                   acq_datetime=datetime(2023, 1, 2, 7, 45, tzinfo=UTC)),
    ], conflict_cols=key)
    deleted = supersede_nrt(TEST_SENSOR, datetime(2023, 1, 1, tzinfo=UTC),
                            datetime(2023, 1, 2, tzinfo=UTC))
    assert deleted == 1
    left = fetch_all("SELECT product, acq_datetime::date AS d FROM detections "
                     "WHERE sensor = :s ORDER BY 2, 1", {"s": TEST_SENSOR})
    assert [(r["product"], str(r["d"])) for r in left] == [
        ("SP", "2023-01-01"), ("NRT", "2023-01-02")]
