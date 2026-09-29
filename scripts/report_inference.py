"""Stage 5 figure: a year of fires over India, segregated, with the alerts on top.

    python scripts/report_inference.py      # after scripts/run_inference.py

Reads the replayed year from the database and writes reports/stage5/fig_2024.png.
Three categorical colours only (the reference palette's all-pairs limit for a
scatter): industrial, agricultural, forest; the rest folds into grey.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import spike_cluster as sc  # noqa: E402  (shared figure style)
from check_registry import frame  # noqa: E402

OUT = REPO / "reports" / "stage5"
COLOURS = {"industrial": "#2a78d6", "agricultural": "#eb6834", "forest": "#1baf7a"}
OTHER = "#c3c2b7"
NAMES = {"industrial": "industrial", "agricultural": "agricultural (cropland)",
         "forest": "forest"}


def main() -> int:
    import matplotlib.pyplot as plt

    run = frame("SELECT window_start, window_end FROM inference_runs ORDER BY run_id DESC "
                "LIMIT 1")
    if run.empty:
        print("no inference run: run scripts/run_inference.py first")
        return 1
    start, end = run["window_start"].iat[0], run["window_end"].iat[0]
    det = frame("SELECT latitude, longitude, category FROM detections "
                "WHERE acq_datetime >= :s AND acq_datetime < :e AND road IS NOT NULL",
                {"s": start, "e": end})
    counts = det["category"].value_counts()
    alerts = frame("SELECT s.source_id, ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon "
                   "FROM source_passes p JOIN sources s USING (source_id) "
                   "WHERE p.alert = 'confirmed' AND p.pass_time >= :s AND p.pass_time < :e",
                   {"s": start, "e": end})
    provisional = frame("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM sources "
                        "WHERE origin = 'promotion' AND promoted_at >= :s AND promoted_at < :e",
                        {"s": start, "e": end})

    fig, ax = plt.subplots(figsize=(8.5, 9.5), facecolor=sc.SURFACE)
    rng = np.random.default_rng(0)
    natural = det[det["category"] != "industrial"]
    natural = natural.iloc[rng.permutation(len(natural))]   # no category painted last
    ax.scatter(natural["longitude"], natural["latitude"], s=0.08, linewidths=0,
               c=natural["category"].map(COLOURS).fillna(OTHER).to_numpy(), rasterized=True)
    # Industry last and larger: it is a few hundred compact sites under a million
    # landscape fires, and it is what the deliverable is about.
    industrial = det[det["category"] == "industrial"]
    ax.scatter(industrial["longitude"], industrial["latitude"], s=1.2, linewidths=0,
               c=COLOURS["industrial"], rasterized=True)
    for cat, col in COLOURS.items():
        ax.scatter([], [], s=30, c=col, label=f"{NAMES[cat]}: {counts.get(cat, 0):,}")
    rest = int(len(det) - sum(counts.get(c, 0) for c in COLOURS))
    ax.scatter([], [], s=30, c=OTHER, label=f"other vegetation, unclassified: {rest:,}")
    ax.scatter(provisional["lon"], provisional["lat"], s=40, facecolors="none",
               edgecolors=sc.INK, linewidths=1.2, zorder=4,
               label=f"new persistent site, still alerting: {len(provisional)}")
    ax.scatter(alerts["lon"], alerts["lat"], s=70, marker="^", c=sc.INK,
               edgecolors=sc.SURFACE, linewidths=0.8, zorder=5,
               label=f"confirmed anomaly at a known site: {len(alerts)} passes")
    ax.set_aspect(1 / np.cos(np.radians(22)))
    ax.set_xlim(68, 98)
    ax.set_ylim(6, 37)
    sc._style(ax, f"{len(det):,} fire detections, {start.year}: every one routed and "
                  "explained")
    ax.legend(frameon=False, fontsize=8, labelcolor=sc.INK2, loc="lower right",
              markerscale=1)
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"fig_{start.year}.png", dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)
    print("wrote", OUT / f"fig_{start.year}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
