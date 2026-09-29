"""Gated cells into sources, and detections onto sources.

DBSCAN runs on the gated cells, not on detections, with each cell weighted by its
detection count. That is what bounds memory -- scikit-learn materialises every
point's neighbour list whatever the tree algorithm -- and a cell that burned on
hundreds of days counts as heavily as its detections say it should.

A cluster wider than the footprint cap is a landscape, not a source: it is flagged
and left unregistered, so its detections stay on Road A. The cap is a backstop; the
widest gated source on three real years was 5.8 km.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from firewatch.grid import CELL_M

EPS_M = 500.0           # VIIRS pixels are 375 m
MIN_SAMPLES = 5         # by weight: detections, not cells
CAP_KM = 20.0
#: How far a detection may sit from a source cell and still belong to it. VIIRS
#: is the router's 500 m. MODIS pixels are 1 km at nadir and grow off-nadir, so a
#: MODIS pixel centre over the same flare routinely lands 500-1000 m away.
ASSIGN_M = {"VIIRS": 500.0, "MODIS": 1000.0}


def cluster_cells(gated: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Label gated cells with a cluster; drop clusters wider than the cap.

    Returns the cells with ``cluster`` (-1 = not registered) and the per-cluster
    table (``n_cells``, ``n_det``, ``width_km``, ``capped``), capped ones included
    so the report can say what the cap removed.
    """
    from sklearn.cluster import DBSCAN

    cells = gated.copy()
    if cells.empty:
        cells["cluster"] = pd.Series(dtype=np.int64)
        return cells, pd.DataFrame(columns=["n_cells", "n_det", "width_km", "capped"])
    labels = DBSCAN(eps=EPS_M, min_samples=MIN_SAMPLES, algorithm="ball_tree").fit_predict(
        cells[["x", "y"]].to_numpy(), sample_weight=cells["n_det"].to_numpy())
    cells["cluster"] = labels
    member = cells[labels >= 0]
    table = member.groupby("cluster").agg(
        n_cells=("x", "size"), n_det=("n_det", "sum"),
        x0=("x", "min"), x1=("x", "max"), y0=("y", "min"), y1=("y", "max"))
    # Corner to corner of the cells' mean positions, plus one cell: the same
    # measure the Stage 1.5 spikes reported.
    table["width_km"] = np.hypot(table.x1 - table.x0 + CELL_M,
                                 table.y1 - table.y0 + CELL_M) / 1000
    table["capped"] = table["width_km"] > CAP_KM
    capped = table.index[table["capped"]]
    cells.loc[cells["cluster"].isin(capped), "cluster"] = -1
    return cells, table[["n_cells", "n_det", "width_km", "capped"]]


def assign(x: np.ndarray, y: np.ndarray, cells: pd.DataFrame, radius_m: float,
           column: str = "cluster") -> np.ndarray:
    """The ``column`` label of the nearest registered cell within ``radius_m`` of
    each point, or -1.

    Registered means ``column >= 0``. Nearest cell, not nearest source centre: a
    source can be kilometres wide, and the router asks about its footprint.
    """
    from scipy.spatial import cKDTree

    reg = cells[cells[column] >= 0]
    out = np.full(len(x), -1, dtype=np.int64)
    if reg.empty or not len(x):
        return out
    dist, idx = cKDTree(reg[["x", "y"]].to_numpy()).query(
        np.column_stack([x, y]), distance_upper_bound=radius_m, workers=-1)
    hit = np.isfinite(dist)
    out[hit] = reg[column].to_numpy()[idx[hit]]
    return out
