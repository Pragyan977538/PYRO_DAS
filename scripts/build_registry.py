"""Stage 3: build the registry from the detections in the database.

    python scripts/build_registry.py            # build with REGISTRY_GATE and write it
    python scripts/build_registry.py --sweep    # recalibrate on the archive; writes nothing

The sweep scores each candidate gate against GIHS, FIRMS' static flag, the paddy
belt and the three demo refineries, then applies the rule fixed in docs/ROADMAP.md
(Stage 3) before it ran: keep the approved gate if it meets every floor, otherwise
take the passing setting with the best F1.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.registry import evaluate as ev  # noqa: E402
from firewatch.registry.build import (  # noqa: E402
    build,
    read_detections,
    read_gihs,
    write,
)
from firewatch.registry.cells import (  # noqa: E402
    Gate,
    add_metres,
    gated_cells,
    recurrence,
)
from firewatch.registry.cluster import ASSIGN_M, assign, cluster_cells  # noqa: E402

OUT = REPO / "reports" / "stage3"
SWEEP = {"years": (2, 3, 4), "months": (3, 4), "days": (10, 20)}
log = logging.getLogger("build_registry")


def centres(cells: pd.DataFrame) -> np.ndarray:
    """Detection-weighted centre of each registered cluster, in metres."""
    reg = cells[cells["cluster"] >= 0]
    w = reg["n_det"].to_numpy(dtype=float)
    g = pd.DataFrame({"c": reg["cluster"], "wx": reg["x"] * w, "wy": reg["y"] * w,
                      "w": w}).groupby("c").sum()
    return np.column_stack([g["wx"] / g["w"], g["wy"] / g["w"]])


def score(cells: pd.DataFrame, clusters: pd.DataFrame, viirs: pd.DataFrame,
          assigned: np.ndarray, gihs) -> dict:
    ok = clusters[~clusters["capped"]] if len(clusters) else clusters
    metrics = {
        "sources": int(len(ok)),
        "capped": int(clusters["capped"].sum()) if len(clusters) else 0,
        "widest_km": round(float(ok["width_km"].max()), 2) if len(ok) else None,
        "punjab_misrouted": ev.punjab_misrouted(
            viirs["latitude"].to_numpy(), viirs["longitude"].to_numpy(),
            viirs["x"].to_numpy(), viirs["y"].to_numpy(), assigned),
        "type2_recall": ev.type2_recall(viirs["firms_type"].to_numpy(), assigned),
        "viirs_assigned": round(float(assigned.mean()), 4),
        **ev.gihs_scores(centres(cells), gihs),
        "sites": ev.site_hits(cells),
    }
    metrics["f1"] = round(ev.f1(metrics["gihs_recall"], metrics["on_gihs"]), 4)
    metrics["failures"] = ev.failures(metrics)
    return metrics


def sweep(viirs: pd.DataFrame, gihs, approved: Gate) -> dict:
    rec = recurrence(viirs)
    rows = []
    for y, m, d in itertools.product(SWEEP["years"], SWEEP["months"], SWEEP["days"]):
        gate = Gate(months=m, days=d, years=y)
        cells, clusters = cluster_cells(gated_cells(rec, gate))
        assigned = assign(viirs["x"].to_numpy(), viirs["y"].to_numpy(), cells,
                          ASSIGN_M["VIIRS"]) >= 0
        row = {"years": y, "months": m, "days": d, **score(cells, clusters, viirs,
                                                           assigned, gihs)}
        rows.append(row)
        log.info("y>=%d m>=%d d>=%d: %4d sources, punjab %.3f%%, gihs recall %s, "
                 "on-gihs %s, type2 %s, fails %d", y, m, d, row["sources"],
                 100 * (row["punjab_misrouted"] or 0), row["gihs_recall"], row["on_gihs"],
                 row["type2_recall"], len(row["failures"]))
    key = (approved.years, approved.months, approved.days)
    current = next(r for r in rows if (r["years"], r["months"], r["days"]) == key)
    passing = [r for r in rows if not r["failures"]]
    if not current["failures"]:
        chosen, why = current, "the approved gate meets every floor: kept"
    elif passing:
        chosen = max(passing, key=lambda r: (r["f1"], -r["sources"]))
        why = "the approved gate misses a floor; best-F1 passing setting taken"
    else:
        chosen, why = current, "no setting meets every floor; approved gate kept"
    return {"rows": rows, "chosen": {k: chosen[k] for k in ("years", "months", "days")},
            "rule_outcome": why, "approved_failures": current["failures"]}


def write_sweep(result: dict, path: Path) -> None:
    lines = ["| years | months | days | sources | Punjab | type=2 | GIHS recall | "
             "on GIHS | F1 | Jamnagar km | Vadinar km | HMEL km | fails |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        s = r["sites"]
        lines.append(
            f"| {r['years']} | {r['months']} | {r['days']} | {r['sources']} | "
            f"{100 * r['punjab_misrouted']:.3f}% | {100 * r['type2_recall']:.1f}% | "
            f"{100 * r['gihs_recall']:.1f}% | {100 * r['on_gihs']:.1f}% | {100 * r['f1']:.1f} | "
            f"{s['Reliance Jamnagar']['km']} | {s['Nayara Vadinar']['km']} | "
            f"{s['HMEL Bathinda']['km']} | {len(r['failures'])} |")
    lines += ["", f"Chosen: years >= {result['chosen']['years']}, months >= "
              f"{result['chosen']['months']}, days >= {result['chosen']['days']} "
              f"({result['rule_outcome']})."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 3 registry build.")
    ap.add_argument("--sweep", action="store_true",
                    help="score candidate gates on the archive; write nothing")
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    gate = Gate.from_settings()

    t0 = time.time()
    viirs = read_detections("VIIRS")
    log.info("VIIRS: %d detections read in %.0fs", len(viirs), time.time() - t0)
    if viirs.empty:
        print("no VIIRS detections: run the backfill first")
        return 1

    if args.sweep:
        add_metres(viirs)
        result = sweep(viirs, read_gihs(), gate)
        (OUT / "sweep.json").write_text(json.dumps(result, indent=2, default=str),
                                        encoding="utf-8")
        write_sweep(result, OUT / "sweep.md")
        print(json.dumps({k: result[k] for k in ("chosen", "rule_outcome",
                                                  "approved_failures")}, indent=2))
        return 0

    modis = read_detections("MODIS")
    log.info("MODIS: %d detections read", len(modis))
    reg = build(viirs, modis, gate)
    log.info("built in %.0fs: %s", time.time() - t0, reg.stats)
    write(reg)
    print(json.dumps({"gate": str(gate), **reg.stats}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
