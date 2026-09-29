"""Stage 3 figures: the registry over India, and the demo refineries up close.

    python scripts/report_registry.py

Reads the built registry from the database and writes reports/stage3/fig*.png.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import spike_cluster as sc  # noqa: E402  (shared figure style)
from check_registry import frame  # noqa: E402

from firewatch.registry.evaluate import SITES  # noqa: E402

OUT = REPO / "reports" / "stage3"
SOURCE_BLUE = "#2a78d6"
VIEWS = {
    "Jamnagar: Reliance and Nayara refineries": (22.25, 22.43, 69.66, 69.96),
    "Bathinda: HMEL refinery inside the paddy belt": (29.86, 30.00, 74.86, 75.04),
    "Jharia coalfield": (23.66, 23.84, 86.28, 86.48),
}


def figure_india(sources: pd.DataFrame, sample: pd.DataFrame, n_total: int,
                 path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 9), facecolor=sc.SURFACE)
    ax.scatter(sample["longitude"], sample["latitude"], s=0.05, c=sc.ROAD_A,
               linewidths=0, rasterized=True)
    ax.scatter(sources["lon"], sources["lat"],
               s=np.clip(np.sqrt(sources["n_detections"]) * 0.8, 6, 90),
               c=SOURCE_BLUE, edgecolors=sc.SURFACE, linewidths=0.6, zorder=3)
    ax.set_aspect(1 / np.cos(np.radians(22)))
    ax.set_xlim(68, 98)
    ax.set_ylim(6, 37)
    sc._style(ax, f"{n_total:,} VIIRS detections, 2012-2024 (grey, sampled) -> "
                  f"{len(sources):,} persistent sources (blue, sized by detections)")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)


def figure_sites(sources: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt
    import shapely

    fig, axes = plt.subplots(1, len(VIEWS), figsize=(6 * len(VIEWS), 5.6),
                             facecolor=sc.SURFACE)
    for ax, (name, (la0, la1, lo0, lo1)) in zip(axes, VIEWS.items(), strict=True):
        det = frame("SELECT latitude, longitude, source_id IS NOT NULL AS hit "
                    "FROM detections WHERE instrument = 'VIIRS' "
                    "AND latitude BETWEEN :la0 AND :la1 AND longitude BETWEEN :lo0 AND :lo1",
                    {"la0": la0, "la1": la1, "lo0": lo0, "lo1": lo1})
        other, hit = det[~det["hit"]], det[det["hit"]]
        ax.scatter(other["longitude"], other["latitude"], s=3, c=sc.ROAD_A, linewidths=0,
                   rasterized=True, label=f"no source: Road A ({len(other):,})")
        ax.scatter(hit["longitude"], hit["latitude"], s=3, c=SOURCE_BLUE, linewidths=0,
                   rasterized=True, label=f"on a source ({len(hit):,})")
        inside = sources[sources["lat"].between(la0, la1) & sources["lon"].between(lo0, lo1)]
        for wkt in inside["footprint"]:
            xs, ys = shapely.from_wkt(wkt).exterior.xy
            ax.plot(xs, ys, color=sc.INK2, lw=0.9)
        for site, (slat, slon) in SITES.items():
            if la0 <= slat <= la1 and lo0 <= slon <= lo1:
                ax.plot(slon, slat, marker="+", ms=14, mew=2, color=sc.INK, ls="")
                ax.annotate(f"{site}\n(published location)", (slon, slat), xytext=(8, 8),
                            textcoords="offset points", fontsize=8, color=sc.INK2)
        ax.set_xlim(lo0, lo1)
        ax.set_ylim(la0, la1)
        ax.set_aspect(1 / np.cos(np.radians((la0 + la1) / 2)))
        noun = "source" if len(inside) == 1 else "sources"
        sc._style(ax, f"{name}: {len(inside)} {noun} (outlines)")
        ax.legend(frameon=False, fontsize=8, labelcolor=sc.INK2, loc="lower left",
                  markerscale=3)
    fig.suptitle("VIIRS 2012-2024: detections within 500 m of a registered source's cells",
                 x=0.01, ha="left", fontsize=11, color=sc.INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=sc.SURFACE)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = frame("SELECT source_id, ST_Y(geom) AS lat, ST_X(geom) AS lon, n_detections, "
                    "ST_AsText(footprint) AS footprint FROM sources WHERE NOT provisional")
    n_total = int(frame("SELECT count(*) AS n FROM detections "
                        "WHERE instrument = 'VIIRS'")["n"].iat[0])
    sample = frame("SELECT latitude, longitude FROM detections TABLESAMPLE SYSTEM (20) "
                   "WHERE instrument = 'VIIRS'")
    figure_india(sources, sample, n_total, OUT / "fig1_india.png")
    figure_sites(sources, OUT / "fig2_sites.png")
    print("wrote", OUT / "fig1_india.png", OUT / "fig2_sites.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
