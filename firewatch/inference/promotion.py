"""Promotion: a Road A site that keeps burning becomes a provisional source.

Provisional means *still alerting*. Every detection at a provisional source is
Road C (``new_source``) and never Road B: the Baghjan blowout burned for five
months, and a rule that let a month of burning define "normal" would have
silenced it within weeks. Nothing automatic ever clears ``provisional`` -- not the
weekly registry rebuild, not time. Only an analyst does (``confirm``), once the
site is known infrastructure.

The rule, on Road A detections of the last 60 days snapped to 375 m cells:
cells burning on >= 3 distinct days seed clusters (500 m apart at most). A
cluster no wider than 2 km is promoted when it burned on >= 10 distinct days and
on at least half of the days it could have been seen since its first fire --
persistence over *observable* days (docs/DESIGN.md §3.7), from the same cloud record as
the registry. Counting calendar days instead (">= 20 days") promoted Baghjan 66
days after it caught fire: the monsoon hid it for weeks. Where there is no cloud
record, 20 calendar days stands in. Crop fields burn once a season and forest
fronts move on, so neither survives. A provisional source quiet for 90 days is
retired and leaves the router.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from firewatch.grid import CELL_M

WINDOW_DAYS = 60
SEED_DAYS = 3
PROMOTE_DAYS = 20              # calendar-day fallback, without a cloud record
MIN_DAYS = 10
MIN_PERSISTENCE = 0.5
MAX_WIDTH_KM = 2.0
RETIRE_DAYS = 90
LINK_M = 500.0


@dataclass
class Promotion:
    """A site promoted to a provisional source."""

    cells: pd.DataFrame            # cell, x, y, n_det
    first_day: int
    last_day: int
    days: int
    persistence: float             # detected days / observable days
    cls: str                       # the commonest Road A class there
    width_km: float


@dataclass
class Promoter:
    #: One row per (cell, day, class): detections compacted, since only distinct
    #: days count and the check runs every day of a replay.
    history: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["cell", "day", "pred_class", "x", "y", "n"]))

    def add(self, det: pd.DataFrame) -> None:
        """Remember Road A detections (``cell``, ``x``, ``y``, ``day``, ``pred_class``)."""
        if len(det):
            part = det.groupby(["cell", "day", "pred_class"], as_index=False).agg(
                x=("x", "mean"), y=("y", "mean"), n=("x", "size"))
            self.history = part if self.history.empty else pd.concat(
                [self.history, part], ignore_index=True)

    def check(self, today: int, clear=None) -> list[Promotion]:
        """Sites that qualify as of UTC day ``today``; their history is consumed.

        ``clear(x, y)`` returns the clear-sky fraction by UTC day at a point in
        metres (a Series), or None without a cloud record.
        """
        from sklearn.cluster import DBSCAN

        h = self.history[self.history["day"] > today - WINDOW_DAYS]
        self.history = h
        if h.empty:
            return []
        cells = h.groupby("cell").agg(x=("x", "mean"), y=("y", "mean"),
                                      days=("day", "nunique"), n_det=("n", "sum"))
        seeds = cells[cells["days"] >= SEED_DAYS]
        if seeds.empty:
            return []
        seeds = seeds.assign(cluster=DBSCAN(eps=LINK_M, min_samples=1).fit_predict(
            seeds[["x", "y"]].to_numpy()))
        # Size and days per cluster in one pass: in the stubble season the paddy belt
        # alone has thousands of seed clusters, and almost none get past this filter.
        geo = seeds.groupby("cluster").agg(x0=("x", "min"), x1=("x", "max"),
                                           y0=("y", "min"), y1=("y", "max"))
        geo["width_km"] = np.hypot(geo.x1 - geo.x0 + CELL_M, geo.y1 - geo.y0 + CELL_M) / 1000
        seeded = h[h["cell"].isin(seeds.index)]
        seeded = seeded.assign(cluster=seeded["cell"].map(seeds["cluster"]).to_numpy())
        geo["days"] = seeded.groupby("cluster")["day"].nunique()
        candidates = geo[(geo["width_km"] <= MAX_WIDTH_KM) & (geo["days"] >= MIN_DAYS)]
        found, used = [], []
        for cluster, c in candidates.iterrows():
            members = seeds[seeds["cluster"] == cluster]
            rows = seeded[seeded["cluster"] == cluster]
            days, width = int(c["days"]), float(c["width_km"])
            sky = clear(members["x"].mean(), members["y"].mean()) if clear else None
            persistence = observed_persistence(rows["day"].unique(), sky,
                                               int(rows["day"].min()), today)
            if not (persistence >= MIN_PERSISTENCE if np.isfinite(persistence)
                    else days >= PROMOTE_DAYS):
                continue
            found.append(Promotion(
                cells=members[["x", "y", "n_det"]].rename_axis("cell").reset_index(),
                first_day=int(rows["day"].min()), last_day=int(rows["day"].max()),
                days=days, persistence=round(float(persistence), 3),
                cls=str(rows["pred_class"].mode().iat[0]), width_km=round(width, 2)))
            used.extend(members.index)
        if used:
            self.history = h[~h["cell"].isin(used)]
        return found


def observed_persistence(detected: np.ndarray, sky: pd.Series | None, first: int,
                         today: int) -> float:
    """Detected days over observable days since the first fire; NaN without sky.

    The registry's persistence estimator (``registry.fingerprint.persistence``): a
    day with a detection was observable by definition; any other day counts its
    clear fraction.
    """
    from firewatch.registry.fingerprint import persistence

    if sky is None or sky.empty:
        return float("nan")
    return persistence(np.asarray(detected), sky, first, today)


def confirm(source_id: int) -> None:
    """An analyst confirms a provisional source as infrastructure. The only path by
    which ``provisional`` is ever cleared; its baseline then applies (Road B / C)."""
    from firewatch.db import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sources SET provisional = FALSE, confirmed_at = now() "
                    "WHERE source_id = %s AND provisional", (source_id,))
        if cur.rowcount != 1:
            raise ValueError(f"source {source_id} is not a provisional source")
