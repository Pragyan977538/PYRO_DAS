"""Per-source fingerprints: what Model 1 classifies on.

Thermal and temporal features only. Nothing here may encode *where* a source is --
no coordinates, no land cover, no distance to industry -- because every label is
built from location, and a model that sees location learns the labelling rule.
FIRMS' own ``type`` is likewise never read here: it is derived from recurrence,
which is what persistence measures.

Built from VIIRS only. MODIS history starts earlier but its 1 km pixels see a
different slice of the same fire; mixing the two would make a source's fingerprint
depend on which years it happened to be active.

Every statistic is NaN-safe. Temperature (VNF) is missing for most detections and
may be missing entirely; ``temp_cov`` records how much of it there was, and nothing
is imputed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from firewatch.registry.cells import utc, utc_day

#: The features written for every source. Stage 4 picks Model 1's FEATS from these
#: and asserts none of them is a label input.
FEATURES = [
    "n_det", "n_cells", "width_km", "years_active", "det_per_year",
    "persistence_night", "persistence_day", "night_share",
    "frp_med", "frp_mad", "frp_p90", "frp_p99", "frp_robust_cv",
    "frp_night_med", "frp_day_med",
    "month_entropy", "peak_month_share", "monsoon_share",
    "bt4_night_med", "bt4_day_med", "bt5_night_med", "bt45_night_med", "bt45_day_med",
    "temp_med", "temp_p90", "temp_cov", "area_med",
]
MONSOON = (6, 7, 8, 9)
#: Below this share of a source's life covered by cloud data, persistence is NaN
#: rather than a number computed on the few days that happen to have a value.
MIN_OBS_COVERAGE = 0.5


def _nan(fn, values: np.ndarray, *args) -> float:
    values = values[np.isfinite(values)]
    return float(fn(values, *args)) if values.size else float("nan")


def mad(values: np.ndarray) -> float:
    """Median absolute deviation, unscaled. NaN-safe."""
    values = values[np.isfinite(values)]
    if not values.size:
        return float("nan")
    return float(np.median(np.abs(values - np.median(values))))


def persistence(detected_days: np.ndarray, clear: pd.Series,
                first_day: int, last_day: int) -> float:
    """Nights (or days) with a detection over nights observable, in (0, 1].

    ``clear`` maps UTC day number -> expected clear fraction (1 - cloud). A day with
    a detection was observable by definition, so it counts 1 on both sides; any
    other day counts its clear fraction in the denominator. Days with no cloud
    value are left out unless something was detected on them. The ratio is
    therefore at most 1, and a source seen on every clear night scores ~1 whatever
    the monsoon did.
    """
    span = np.arange(first_day, last_day + 1)
    if not span.size:
        return float("nan")
    frac = clear.reindex(span).to_numpy(dtype=float)
    if np.isfinite(frac).mean() < MIN_OBS_COVERAGE:
        return float("nan")
    hit = np.isin(span, detected_days)
    denominator = hit.sum() + np.nansum(np.where(hit, 0.0, frac))
    return float(hit.sum() / denominator) if denominator > 0 else float("nan")


def month_stats(days: np.ndarray, months: np.ndarray) -> tuple[float, float, float]:
    """Normalised entropy, peak share and monsoon share of detection-days by month.

    Counted on distinct days, not detections: a big flare lighting three pixels on
    one night is one night. A furnace running all year scores entropy ~1; a
    stubble-burning cell with two seasons scores low.
    """
    frame = pd.DataFrame({"d": days, "m": months}).drop_duplicates("d")
    counts = np.bincount(frame["m"].to_numpy(), minlength=13)[1:].astype(float)
    total = counts.sum()
    if total == 0:
        return float("nan"), float("nan"), float("nan")
    p = counts / total
    nz = p[p > 0]
    entropy = float(-(nz * np.log(nz)).sum() / np.log(12))
    monsoon = float(p[[m - 1 for m in MONSOON]].sum())
    return entropy, float(p.max()), monsoon


def fingerprints(det: pd.DataFrame, clusters: pd.DataFrame,
                 clear: dict[tuple[int, str], pd.Series]) -> pd.DataFrame:
    """One row of ``FEATURES`` per source.

    ``det``: VIIRS detections with a ``source`` column (>= 0 for assigned rows),
    ``acq_datetime``, ``daynight``, ``frp``, ``bt4``, ``bt5`` and, if present,
    ``vnf_temp_k`` / ``vnf_area_m2``.
    ``clusters``: per source ``n_cells`` and ``width_km``.
    ``clear``: (source, 'N' | 'D') -> clear fraction by UTC day number.
    """
    det = det[det["source"] >= 0]
    t = utc(det["acq_datetime"])
    frame = pd.DataFrame({
        "source": det["source"].to_numpy(), "day": utc_day(t), "year": t.year,
        "month": t.month, "night": (det["daynight"] == "N").to_numpy(),
        "frp": det["frp"].to_numpy(dtype=float), "bt4": det["bt4"].to_numpy(dtype=float),
        "bt5": det["bt5"].to_numpy(dtype=float),
        "temp": (det["vnf_temp_k"].to_numpy(dtype=float) if "vnf_temp_k" in det
                 else np.full(len(det), np.nan)),
        "area": (det["vnf_area_m2"].to_numpy(dtype=float) if "vnf_area_m2" in det
                 else np.full(len(det), np.nan)),
    })
    rows = []
    empty = pd.Series(dtype=float)
    for source, g in frame.groupby("source", sort=True):
        night, day = g[g["night"]], g[~g["night"]]
        first, last = int(g["day"].min()), int(g["day"].max())
        years_active = int(g["year"].nunique())
        entropy, peak, monsoon = month_stats(g["day"].to_numpy(), g["month"].to_numpy())
        frp = g["frp"].to_numpy()
        frp_med = _nan(np.median, frp)
        frp_mad = mad(frp)
        span_years = (last - first + 1) / 365.25
        rows.append({
            "source": int(source),
            "n_det": int(len(g)),
            "n_cells": int(clusters.loc[source, "n_cells"]),
            "width_km": float(clusters.loc[source, "width_km"]),
            "years_active": years_active,
            "det_per_year": float(len(g) / max(span_years, 1.0)),
            "persistence_night": persistence(night["day"].unique(),
                                             clear.get((source, "N"), empty), first, last),
            "persistence_day": persistence(day["day"].unique(),
                                           clear.get((source, "D"), empty), first, last),
            "night_share": float(g["night"].mean()),
            "frp_med": frp_med,
            "frp_mad": frp_mad,
            "frp_p90": _nan(np.quantile, frp, 0.90),
            "frp_p99": _nan(np.quantile, frp, 0.99),
            "frp_robust_cv": frp_mad / frp_med if frp_med and np.isfinite(frp_med)
            else float("nan"),
            "frp_night_med": _nan(np.median, night["frp"].to_numpy()),
            "frp_day_med": _nan(np.median, day["frp"].to_numpy()),
            "month_entropy": entropy,
            "peak_month_share": peak,
            "monsoon_share": monsoon,
            "bt4_night_med": _nan(np.median, night["bt4"].to_numpy()),
            "bt4_day_med": _nan(np.median, day["bt4"].to_numpy()),
            "bt5_night_med": _nan(np.median, night["bt5"].to_numpy()),
            "bt45_night_med": _nan(np.median, (night["bt4"] - night["bt5"]).to_numpy()),
            "bt45_day_med": _nan(np.median, (day["bt4"] - day["bt5"]).to_numpy()),
            "temp_med": _nan(np.median, g["temp"].to_numpy()),
            "temp_p90": _nan(np.quantile, g["temp"].to_numpy(), 0.90),
            "temp_cov": float(np.isfinite(g["temp"].to_numpy()).mean()),
            "area_med": _nan(np.median, g["area"].to_numpy()),
            "first_day": first,
            "last_day": last,
        })
    out = pd.DataFrame(rows)
    return out.set_index("source") if len(out) else out
