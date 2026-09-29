"""The risk score of an event, and the decomposition that answers "why that number?".

    Risk = 100 x H^0.40 x E^0.35 x V^0.25,     H, E, V each floored at 0.05

H, hazard = 0.50 f + 0.30 n + 0.20 g
    f  percentile of the event's peak pixel FRP in the national history (a rank,
       not min-max: one 5,000 MW outlier would squash every other fire)
    n  pixels in its biggest pass, as n / (n + 3): FSI's three-pixel "large fire"
       is the midpoint
    g  growth: 1 if its last pass burned more than the one before (> +10%), 0 if
       less (< -10%), 0.5 if flat or seen once. A shrinking fire is being handled.

E, exposure = 0.45 p5 + 0.30 pw + 0.25 a
    p5 percentile of the population within 5 km, against the same population at
       historical fire locations: "more people nearby than x% of Indian fires"
    pw the same for the 30-degree sector downwind to 10 km, on the day of the
       event's last detection (NASA POWER wind); where there is no wind record its
       weight moves to p5
    a  1 - exp(-A/2), A the criticality-weighted count of other critical assets
       within 10 km. Weighted, because 41,000 of the register's 48,000 entries are
       mines and industrial estates: an unweighted count would make every coal-belt
       fire look as exposed as one beside a refinery.

V, vulnerability = what is burning
    the nearest critical asset within 2 km, criticality x exp(-d / 2 km); or, at a
    registry or provisional source, the criticality of the source's own class --
    whichever is higher.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

WEIGHTS = {"hazard": 0.40, "exposure": 0.35, "vulnerability": 0.25}
FLOOR = 0.05
PIXEL_MIDPOINT = 3.0
GROWTH_BAND = 0.10
V_RADIUS_M = 2000.0
ASSET_RADIUS_KM = 10.0
ASSET_SCALE = 2.0


def percentile(values, reference: np.ndarray) -> np.ndarray:
    """Share of ``reference`` (sorted) at or below each value."""
    ref = np.asarray(reference, dtype=float)
    return np.searchsorted(ref, np.asarray(values, dtype=float), side="right") / max(len(ref), 1)


def growth_score(last: np.ndarray, previous: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(score, word) from the last two passes' total FRP; NaN previous = seen once."""
    last, previous = np.asarray(last, dtype=float), np.asarray(previous, dtype=float)
    ratio = last / np.where(previous > 0, previous, np.nan)
    score = np.where(ratio > 1 + GROWTH_BAND, 1.0, np.where(ratio < 1 - GROWTH_BAND, 0.0, 0.5))
    word = np.where(~np.isfinite(ratio), "seen once",
                    np.where(score == 1.0, "growing", np.where(score == 0.0, "shrinking", "flat")))
    return np.where(np.isfinite(ratio), score, 0.5), word


def hazard(peak_frp, pixels, growth, frp_reference) -> dict[str, np.ndarray]:
    f = percentile(peak_frp, frp_reference)
    n = np.asarray(pixels, dtype=float)
    n_hat = n / (n + PIXEL_MIDPOINT)
    h = 0.50 * f + 0.30 * n_hat + 0.20 * np.asarray(growth, dtype=float)
    return {"score": np.clip(h, FLOOR, 1.0), "frp_percentile": f, "pixels_score": n_hat}


def exposure(pop5, pop_downwind, asset_weight, pop5_reference,
             downwind_reference) -> dict[str, np.ndarray]:
    p5 = percentile(np.log1p(pop5), np.log1p(pop5_reference))
    has_wind = np.isfinite(np.asarray(pop_downwind, dtype=float))
    pw = np.where(has_wind, percentile(np.log1p(np.nan_to_num(pop_downwind)),
                                       np.log1p(downwind_reference)), np.nan)
    a = 1 - np.exp(-np.asarray(asset_weight, dtype=float) / ASSET_SCALE)
    e = np.where(has_wind, 0.45 * p5 + 0.30 * np.nan_to_num(pw) + 0.25 * a,
                 0.75 * p5 + 0.25 * a)
    return {"score": np.clip(e, FLOOR, 1.0), "pop_5km_percentile": p5,
            "pop_downwind_percentile": pw, "assets_score": a}


def vulnerability(nearest_criticality, distance_m, own_criticality) -> np.ndarray:
    near = np.nan_to_num(np.asarray(nearest_criticality, dtype=float)) * \
        np.exp(-np.nan_to_num(np.asarray(distance_m, dtype=float), nan=np.inf) / V_RADIUS_M)
    own = np.nan_to_num(np.asarray(own_criticality, dtype=float))
    return np.clip(np.maximum(near, own), FLOOR, 1.0)


def risk(h, e, v) -> np.ndarray:
    return 100.0 * (np.asarray(h) ** WEIGHTS["hazard"] * np.asarray(e) ** WEIGHTS["exposure"]
                    * np.asarray(v) ** WEIGHTS["vulnerability"])


def breakdowns(frame: pd.DataFrame) -> list[str]:
    """The JSON decomposition for each scored row."""
    out = []
    for r in frame.itertuples():
        out.append(json.dumps({
            "risk": round(float(r.risk), 1),
            "formula": "100 x H^0.40 x E^0.35 x V^0.25, each floored at 0.05",
            "hazard": {"score": round(float(r.h), 3), "peak_frp_mw": round(float(r.peak_frp), 1),
                       "frp_percentile": round(float(r.frp_percentile), 3),
                       "pixels": int(r.pixels), "growth": r.growth_word},
            "exposure": {"score": round(float(r.e), 3), "pop_5km": int(round(r.pop5)),
                         "pop_5km_percentile": round(float(r.pop_5km_percentile), 3),
                         "pop_downwind_10km": None if not np.isfinite(r.pop_downwind)
                         else int(round(r.pop_downwind)),
                         "downwind_bearing_deg": None if not np.isfinite(r.bearing)
                         else int(round(r.bearing)),
                         "wind_date": None if pd.isna(r.wind_date) else str(r.wind_date),
                         "assets_10km": int(r.assets_10km),
                         "assets_weighted": round(float(r.asset_weight), 2)},
            "vulnerability": {"score": round(float(r.v), 3),
                              "nearest": None if pd.isna(r.nearest_name) else r.nearest_name,
                              "nearest_type": None if pd.isna(r.nearest_type)
                              else r.nearest_type,
                              "distance_m": None if not np.isfinite(r.nearest_m)
                              else int(round(r.nearest_m)),
                              "own_class": None if pd.isna(r.own_class) else r.own_class},
        }))
    return out
