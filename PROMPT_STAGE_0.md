# Stage 0 — prompt for Claude Code

Open a terminal in the project folder and start Claude Code:

```bash
cd ~/dev/firewatch
claude
```

Select the Opus model (`/model`), then paste everything below the line.

---

Read `CLAUDE.md` and `docs/ROADMAP.md` before writing any code. `CLAUDE.md` contains
design decisions that were derived from actual testing — treat them as constraints, not
suggestions. If something in it seems wrong, say so rather than silently working around it.

Build **Stage 0 only**. Do not start Stage 1, and do not stub out later stages.

## Deliverables

**1. `docker-compose.yml`**

One service, `db`: PostgreSQL 16 with PostGIS 3.4 and TimescaleDB. Use the
`timescale/timescaledb-ha:pg16` image, which ships PostGIS. Named volume for persistence,
port 5432 exposed, healthcheck on `pg_isready`, env vars read from `.env`.

**2. `requirements.txt`**

Pinned versions. Needs: pandas, numpy, scikit-learn, scipy, xgboost, requests, geopandas,
shapely, pyproj, psycopg2-binary, SQLAlchemy, python-dotenv, fastapi, uvicorn, pytest.

**3. `.env.example`**

Every variable the project will need, with safe defaults and a comment per line:
`DATABASE_URL`, `MOCK_MODE=1`, `FIRMS_MAP_KEY=`, `EOG_USERNAME=`, `EOG_PASSWORD=`,
`INDIA_BBOX=68,6,98,37`, `DATA_DIR=./data`, `LOG_LEVEL=INFO`.

**4. `firewatch/config.py`**

Loads `.env` via python-dotenv into a frozen dataclass. Typed attributes. Raises a clear
error naming the missing variable if a required one is absent when `MOCK_MODE=0`. No
module-level side effects beyond constructing the settings object.

**5. `firewatch/db.py`**

SQLAlchemy engine with a connection pool, a `get_conn()` context manager, a
`bulk_upsert(table, rows, conflict_cols)` helper using `execute_values` with
`ON CONFLICT DO UPDATE`, and a `run_sql_file(path)` used by the migration target.

**6. `sql/001_schema.sql`**

The full schema from `docs/PS26162_Blueprint.md` §5. All six tables: `detections`
(TimescaleDB hypertable on `acq_datetime`), `sources`, `events`, `observability`,
`critical_assets`, `osm_industrial`. Use the exact column names in the blueprint — later
stages depend on them. Add `bt4` and `bt5` as the normalised brightness columns, plus
`forest_boundary`.

**7. `sql/002_indexes.sql`**

GiST indexes on every geometry column. B-tree on `detections(source_id)`,
`detections(event_id)`, `sources(cls)`, `events(status)`. Composite on
`observability(cell_id, obs_date)`.

**8. `Makefile`**

Targets: `db` (compose up + wait for healthy), `migrate` (run both SQL files in order),
`psql` (open a shell), `down`, `clean` (down + remove volume), `install`, `test`, `lint`.
Each target echoes what it is doing.

**9. `README.md`**

Short. What the project is in two sentences, prerequisites, the four commands to get from
clone to a migrated database, and a pointer to `docs/ROADMAP.md`.

**10. `tests/test_stage0.py`**

pytest: config loads with defaults; `get_conn()` connects; all expected tables exist;
PostGIS and TimescaleDB extensions are present; `detections` is a hypertable.

**11. `.gitignore`**

`.env`, `data/`, `models/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `reports/*.json`.

## Acceptance

This must pass end to end from a clean checkout:

```bash
make install
make db
make migrate
make test
```

Then verify by hand:

```bash
make psql
\dt
SELECT PostGIS_Version();
SELECT extname FROM pg_extension WHERE extname='timescaledb';
SELECT * FROM timescaledb_information.hypertables;
```

## How to work

Build it, then actually run the acceptance commands yourself and fix what breaks. Do not
report Stage 0 complete until `make test` passes on your machine.

When you are done, give me a one-paragraph summary of what exists and anything you had to
decide that the spec did not cover.
