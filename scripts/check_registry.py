"""Stage 3 acceptance: score the registry in the database against its floors.

    python scripts/check_registry.py

Reads what ``build_registry.py`` wrote -- sources, their cells, and each
detection's ``source_id`` -- so it checks the stored registry, not an in-memory
copy. Prints the numbers, writes ``reports/stage3/check.json`` and exits non-zero
if any floor in docs/ROADMAP.md (Stage 3) fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.db import fetch_all  # noqa: E402
from firewatch.grid import to_metres  # noqa: E402
from firewatch.registry import evaluate as ev  # noqa: E402
from firewatch.registry.build import read_gihs  # noqa: E402
from firewatch.registry.cluster import CAP_KM  # noqa: E402

OUT = REPO / "reports" / "stage3"


def frame(sql: str, params: dict | None = None) -> pd.DataFrame:
    from sqlalchemy import text

    from firewatch.db import engine
    with engine().connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})


def main() -> int:
    sources = frame("SELECT source_id, ST_Y(geom) AS lat, ST_X(geom) AS lon, provisional, "
                    "fingerprint, baselines FROM sources WHERE NOT provisional "
                    "ORDER BY source_id")
    if sources.empty:
        print("FAIL: the registry is empty; run `make registry`")
        return 1
    cells = frame("SELECT c.source_id AS cluster, c.x_m AS x, c.y_m AS y "
                  "FROM source_cells c JOIN sources s USING (source_id) WHERE NOT s.provisional")
    fp = pd.DataFrame(list(sources["fingerprint"]), index=sources["source_id"])
    sx, sy = to_metres(sources["lat"].to_numpy(), sources["lon"].to_numpy())

    la0, la1, lo0, lo1 = ev.PUNJAB_BOX
    belt = frame("SELECT latitude, longitude, source_id FROM detections "
                 "WHERE instrument = 'VIIRS' AND latitude BETWEEN :la0 AND :la1 "
                 "AND longitude BETWEEN :lo0 AND :lo1",
                 {"la0": la0, "la1": la1, "lo0": lo0, "lo1": lo1})
    bx, by = to_metres(belt["latitude"].to_numpy(), belt["longitude"].to_numpy())
    static = fetch_all("SELECT count(*) AS n, count(source_id) AS hit FROM detections "
                       "WHERE instrument = 'VIIRS' AND firms_type = 2")[0]
    assigned = fetch_all("SELECT instrument, count(*) AS n, count(source_id) AS hit "
                         "FROM detections GROUP BY 1 ORDER BY 1")
    run = fetch_all("SELECT run_id, built_at::text AS built_at, gate, stats "
                    "FROM registry_runs ORDER BY run_id DESC LIMIT 1")

    # Both ratios in [0, 1], and every source positive on at least one of them: a
    # registered source recurs, but a plant working day shifts is never seen at night
    # (source 93 of the first build: Bajaj Auto, Waluj -- on a confirmed GIHS site,
    # 258 VIIRS detections, all on the morning pass).
    persistence = fp["persistence_night"].astype(float)
    day_persistence = fp["persistence_day"].astype(float)
    in_range = persistence.between(0, 1) & day_persistence.between(0, 1)
    bad_persistence = persistence[~in_range | ~((persistence > 0) | (day_persistence > 0))]
    day_only = fp.index[(persistence == 0) & (day_persistence > 0)].tolist()
    empty_baselines = [sid for sid, b in zip(sources["source_id"], sources["baselines"],
                                             strict=True)
                       if not b or "*" not in b]
    too_wide = fp.index[fp["width_km"].astype(float) > CAP_KM].tolist()

    metrics = {
        "run": run[0] if run else None,
        "sources": int(len(sources)),
        "source_cells": int(len(cells)),
        "widest_km": round(float(fp["width_km"].max()), 2),
        "punjab_misrouted": ev.punjab_misrouted(
            belt["latitude"].to_numpy(), belt["longitude"].to_numpy(), bx, by,
            belt["source_id"].notna().to_numpy()),
        "punjab_detections": int(len(belt)),
        "type2_recall": round(static["hit"] / static["n"], 4) if static["n"] else None,
        "assigned_by_instrument": {r["instrument"]: round(r["hit"] / r["n"], 4)
                                   for r in assigned},
        **ev.gihs_scores(np.column_stack([sx, sy]), read_gihs()),
        "sites": ev.site_hits(cells),
        "persistence_night": {
            "min": round(float(persistence.min()), 4), "median": round(float(persistence.median()), 4),
            "max": round(float(persistence.max()), 4), "failing": int(len(bad_persistence)),
            "nan": int(persistence.isna().sum()), "day_only_sources": day_only},
        "persistence_day_median": round(float(fp["persistence_day"].astype(float).median()), 4),
        "empty_baselines": len(empty_baselines),
        "wider_than_cap": len(too_wide),
    }
    metrics["f1"] = round(ev.f1(metrics["gihs_recall"], metrics["on_gihs"]), 4)
    failures = ev.failures(metrics)
    if len(bad_persistence):
        failures.append(f"{len(bad_persistence)} sources with persistence outside [0, 1] "
                        f"or zero both night and day: {bad_persistence.index.tolist()[:10]}")
    if empty_baselines:
        failures.append(f"{len(empty_baselines)} sources without a source-wide baseline")
    if too_wide:
        failures.append(f"{len(too_wide)} sources wider than the {CAP_KM:g} km cap")
    metrics["failures"] = failures

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "check.json").write_text(json.dumps(metrics, indent=2, default=str),
                                    encoding="utf-8")
    print(json.dumps(metrics, indent=2, default=str))
    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
