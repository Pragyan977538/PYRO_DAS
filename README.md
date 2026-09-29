# FireWatch

Satellite-based detection and classification of industrial fires and persistent
thermal sources over India. Built for **SIH 2026, problem statement 26162** (NTRO).

NASA FIRMS tells you a pixel is hot. It does not tell you *what* is hot. FireWatch
learns each location's thermal fingerprint from a decade of history and answers the
question FIRMS cannot: **is this normal for this place?**

## Prerequisites

- Python **3.13** (every pinned package has a Windows wheel for 3.13; several have
  none for 3.14 yet)
- Docker Desktop (Windows) or Docker + Compose. A native PostgreSQL 16 + PostGIS
  install also works: TimescaleDB is optional.

## Quickstart

Linux / macOS / Git Bash:

```bash
make install     # .venv with Python 3.13, deps, and .env from .env.example
make db          # start postgis + timescaledb (large first pull, be patient)
make migrate     # apply schema and indexes
make test        # verify - the DB tests must run, not skip
```

Windows PowerShell (5.1 or 7):

```powershell
.\make.ps1 install
.\make.ps1 db
.\make.ps1 migrate
.\make.ps1 test
```

Then the data (the same targets work with `make`):

```powershell
.\make.ps1 backfill   # FIRMS archive 2012-2024, OSM, GIHS, cloud cover (~1 GB download)
.\make.ps1 registry   # persistent sources, fingerprints and baselines, then the checks
```

**No Docker?** `.\scripts\local_postgres.ps1 install` sets up a portable PostgreSQL 16
+ PostGIS under `%LOCALAPPDATA%\firewatch`. It needs no admin rights and no service,
downloads ~450 MB, and uses the credentials in `.env`. After that, `.\make.ps1 db`
starts it whenever Docker is absent.

Real data is the default. The FIRMS archive for India (2012-2024) is public, so
nothing needs a key to start. A `FIRMS_MAP_KEY` is needed only for 2025 onward and
the live feed. VIIRS Nightfire is optional enrichment. `MOCK_MODE=1` switches to
the synthetic test fixture.

## Where to look

| File | What it is |
|---|---|
| `CLAUDE.md` | Design decisions that must not be undone, and the log of those that changed. Read before changing anything. |
| `docs/ROADMAP.md` | The build stages, each with an acceptance command. |
| `reports/stage3_registry.md` | The registry on the full archive: 557 sources and how they were checked. |
| `reports/stage1_5_spike.md` | Real-data clustering test: one year of FIRMS over India. |
| `docs/plan.md` | Feasibility results from the synthetic benchmark (partly superseded). |
| `docs/PS26162_Blueprint.md` | Full technical reference, data source URLs, field lists (partly superseded). |
| `reference/verify_pipeline.py` | Runnable synthetic benchmark. |

## Status

Stages 0, 1, 1.5, 1.5b and 2 done. The real 2012–2024 archive is loaded (12.55M
detections). Stage 3 done: the registry has 557 persistent sources and passes every
acceptance floor. Stage 4 (weak labels and Model 1) is next.
