"""Score a registry against references it was never built from.

* **GIHS** (Ma et al. 2024): imagery-verified industrial heat sources. Recall of
  confirmed India objects still active in 2021, and the share of our sources that
  sit on any confirmed object -- a lower bound on precision, since GIHS stops in
  2021 and does not try to be complete.
* **FIRMS type=2**: the share of static-flagged VIIRS detections that land on a
  source. FIRMS derives the flag from recurrence too, so this is a sanity check,
  not independent truth -- and never a label.
* **The Punjab paddy belt**: the share of its detections that would skip Road A,
  outside a zone around the HMEL refinery that really is a source.
* **Three demo refineries** at their published coordinates.

The floors are the Stage 3 acceptance criteria (docs/ROADMAP.md), set from three
years in Stage 1.5b.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from firewatch.grid import to_metres

#: Published coordinates (Wikipedia infoboxes). The demo leans on these three.
SITES = {
    "Reliance Jamnagar": (22.34806, 69.86889),
    "Nayara Vadinar": (22.33167, 69.74722),
    "HMEL Bathinda": (29.93056, 74.94583),
}
SITE_RADIUS_M = 3000      # refinery complexes span kilometres; published points are nominal
GIHS_RADIUS_M = 1000
PUNJAB_BOX = (29.9, 31.7, 74.0, 76.5)   # lat0, lat1, lon0, lon1
HMEL_ZONE_M = 3000

FLOORS = {
    "gihs_recall": 0.75,        # confirmed objects active in 2021, within 1 km
    "on_gihs": 0.85,            # sources on a confirmed object, within 1 km
    "punjab_misrouted": 0.01,   # a ceiling
    "type2_recall": 0.98,
}


def f1(recall: float | None, precision: float | None) -> float:
    r, p = recall or 0.0, precision or 0.0
    return 2 * r * p / (r + p) if r + p else 0.0


def _within(left_xy: np.ndarray, right, radius_m: float) -> np.ndarray:
    """For each point in ``left_xy``, the right-hand geometries within radius."""
    import shapely

    if right is None or not len(right) or not len(left_xy):
        return np.zeros((2, 0), dtype=np.int64)
    tree = shapely.STRtree(np.asarray(right))
    points = shapely.points(left_xy[:, 0], left_xy[:, 1])
    return tree.query(points, predicate="dwithin", distance=radius_m)


def gihs_scores(src_xy: np.ndarray, gihs: pd.DataFrame | None) -> dict:
    """Recall and on-GIHS share at 1 km, and how many GIHS objects share a source.

    ``gihs`` carries ``geometry`` in EPSG:7755 metres plus ``confirmed`` and
    ``active_2021``. Sources are measured from their centres, as in Stage 1.5b.
    """
    if gihs is None or gihs.empty:
        return {"gihs_recall": None, "gihs_recall_all": None, "on_gihs": None,
                "sources_merging_gihs": None, "gihs_in_shared_sources": None}
    confirmed = gihs[gihs["confirmed"]].reset_index(drop=True)
    pairs = _within(src_xy, confirmed.geometry.to_numpy(), GIHS_RADIUS_M)
    src_i, gihs_i = pairs
    hit = np.zeros(len(confirmed), dtype=bool)
    hit[gihs_i] = True
    active = confirmed["active_2021"].to_numpy(dtype=bool)
    per_source = pd.Series(gihs_i).groupby(src_i).nunique()
    shared = per_source[per_source >= 2]
    return {
        "gihs_recall": round(float(hit[active].mean()), 4) if active.any() else None,
        "gihs_recall_all": round(float(hit.mean()), 4) if len(hit) else None,
        "on_gihs": round(float(np.isin(np.arange(len(src_xy)), src_i).mean()), 4)
        if len(src_xy) else None,
        "sources_merging_gihs": int(len(shared)),
        "gihs_in_shared_sources": int(shared.sum()),
    }


def site_hits(cells: pd.DataFrame, column: str = "cluster") -> dict[str, dict]:
    """Distance from each published refinery to the nearest registered cell."""
    reg = cells[cells[column] >= 0]
    out = {}
    for name, (lat, lon) in SITES.items():
        sx, sy = to_metres([lat], [lon])
        d = (float(np.hypot(reg["x"] - sx[0], reg["y"] - sy[0]).min())
             if len(reg) else float("inf"))
        out[name] = {"km": round(d / 1000, 2), "found": bool(d < SITE_RADIUS_M)}
    return out


def punjab_misrouted(lat: np.ndarray, lon: np.ndarray, x: np.ndarray, y: np.ndarray,
                     assigned: np.ndarray) -> float | None:
    """Share of paddy-belt detections outside the HMEL zone that land on a source."""
    la0, la1, lo0, lo1 = PUNJAB_BOX
    hx, hy = to_metres([SITES["HMEL Bathinda"][0]], [SITES["HMEL Bathinda"][1]])
    belt = (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
    belt &= np.hypot(x - hx[0], y - hy[0]) >= HMEL_ZONE_M
    return round(float(assigned[belt].mean()), 5) if belt.any() else None


def type2_recall(firms_type: np.ndarray, assigned: np.ndarray) -> float | None:
    static = np.asarray(firms_type) == 2
    return round(float(assigned[static].mean()), 4) if static.any() else None


def failures(metrics: dict) -> list[str]:
    """Which acceptance floors a set of metrics misses. Missing references count
    as failures: an unmeasured floor is not a pass."""
    out = []
    for key, floor in FLOORS.items():
        value = metrics.get(key)
        if value is None:
            out.append(f"{key} not measured")
        elif key == "punjab_misrouted" and value > floor:
            out.append(f"{key} {value:.2%} > {floor:.0%}")
        elif key != "punjab_misrouted" and value < floor:
            out.append(f"{key} {value:.1%} < {floor:.0%}")
    for name, hit in metrics.get("sites", {}).items():
        if not hit["found"]:
            out.append(f"no source within {SITE_RADIUS_M / 1000:g} km of {name} "
                       f"(nearest {hit['km']} km)")
    return out
