"""375 m cells and the recurrence gate.

The registry is built from cells, never from raw detections. Raw DBSCAN over
detections chains crop and forest landscapes into "sources" -- on 2021-2023 it
merged the whole Punjab paddy belt into one 369 km cluster -- and its memory grows
with the square of cluster size. A cell is one VIIRS pixel; a cell that burns in
many months of many years is infrastructure, while a field burns in one season.

The gate counts distinct days and distinct months per cell per year, then asks in
how many years a cell cleared both. Requiring two years keeps a single long
accident (a five-month blowout) out of the registry by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from firewatch.grid import cell_key, to_metres


@dataclass(frozen=True)
class Gate:
    """Within one year: >= ``months`` distinct months on >= ``days`` distinct days.
    A cell passes when that holds in >= ``years`` years.

    No defaults: the production gate comes from config (REGISTRY_GATE), and a
    test states the gate it uses."""

    months: int
    days: int
    years: int

    @classmethod
    def from_settings(cls) -> Gate:
        from firewatch.config import settings
        months, days, years = settings().registry_gate
        return cls(months=months, days=days, years=years)

    def __str__(self) -> str:
        return f">= {self.months} months, >= {self.days} days, in >= {self.years} years"


@dataclass
class Recurrence:
    """Per-cell totals, and per cell-year distinct days and months."""

    cells: pd.DataFrame     # index cell: n_det, x, y (mean position, metres)
    days: pd.DataFrame      # cell x year -> distinct days
    months: pd.DataFrame    # cell x year -> distinct months


def add_metres(det: pd.DataFrame) -> pd.DataFrame:
    """Add EPSG:7755 ``x``, ``y`` and the registry ``cell`` key, in place."""
    x, y = to_metres(det["latitude"].to_numpy(), det["longitude"].to_numpy())
    det["x"] = x
    det["y"] = y
    det["cell"] = cell_key(x, y)
    return det


def recurrence(det: pd.DataFrame) -> Recurrence:
    """Aggregate detections (with ``x``, ``y``, ``cell``, ``acq_datetime``) into cells.

    Days are UTC days, the convention of every date in the system.
    """
    t = utc(det["acq_datetime"])
    frame = pd.DataFrame({
        "cell": det["cell"].to_numpy(), "year": t.year.to_numpy(),
        "month": t.month.to_numpy(), "day": utc_day(t),
    })
    per_year = frame.groupby(["cell", "year"]).agg(days=("day", "nunique"),
                                                   months=("month", "nunique"))
    cells = pd.DataFrame({"cell": det["cell"].to_numpy(), "x": det["x"].to_numpy(),
                          "y": det["y"].to_numpy()}).groupby("cell").agg(
        n_det=("x", "size"), x=("x", "mean"), y=("y", "mean"))
    return Recurrence(cells=cells,
                      days=per_year["days"].unstack(fill_value=0),
                      months=per_year["months"].unstack(fill_value=0))


def years_passing(rec: Recurrence, gate: Gate) -> pd.Series:
    """How many years each cell cleared the within-year test."""
    ok = (rec.months >= gate.months) & (rec.days >= gate.days)
    return ok.sum(axis=1)


def gated_cells(rec: Recurrence, gate: Gate) -> pd.DataFrame:
    """The cells that pass the gate, with how many years each passed."""
    years = years_passing(rec, gate)
    keep = years.index[years >= gate.years]
    out = rec.cells.loc[keep].copy()
    out["years_passed"] = years.loc[keep].astype(int)
    return out


def utc(times) -> pd.DatetimeIndex:
    """Timestamps as a UTC DatetimeIndex; naive input is taken to be UTC already."""
    t = pd.DatetimeIndex(times)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def utc_day(t: pd.DatetimeIndex) -> np.ndarray:
    """UTC day number (days since 1970-01-01) per timestamp, whatever the unit."""
    return np.asarray((utc(t) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(days=1),
                      dtype=np.int64)
