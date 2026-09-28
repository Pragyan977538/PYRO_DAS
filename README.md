# FireWatch

Satellite-based detection and classification of industrial fires and persistent
thermal sources over India. Built for **SIH 2026, problem statement 26162** (NTRO).

NASA FIRMS tells you a pixel is hot. It does not tell you *what* is hot. FireWatch
learns each location's thermal fingerprint from a decade of history and answers the
question FIRMS cannot: **is this normal for this place?**

## Prerequisites

- Docker and Docker Compose
- Python 3.11+

## Quickstart

```bash
make install     # deps, and creates .env from .env.example
make db          # start postgis + timescaledb (large first pull, be patient)
make migrate     # apply schema and indexes
make test        # verify
```

`MOCK_MODE=1` is the default, so nothing needs API keys. The whole pipeline runs
against a synthetic generator until you have credentials.

## Where to look

| File | What it is |
|---|---|
| `CLAUDE.md` | Design decisions that must not be undone. Read before changing anything. |
| `docs/ROADMAP.md` | The ten build stages, each with an acceptance command. |
| `docs/plan.md` | Verified feasibility results and measured numbers. |
| `docs/PS26162_Blueprint.md` | Full technical reference, data source URLs, field lists. |
| `reference/verify_pipeline.py` | Runnable synthetic benchmark. |

## Status

Stage 0 complete: schema, config, database layer, tooling.
Next: Stage 1, the mock data generator.
