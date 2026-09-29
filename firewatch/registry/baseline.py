"""Per-source FRP baselines: median, MAD and p99, keyed instrument|daynight|season.

Per source, never per source type: a large furnace and a small one differ by an
order of magnitude in normal FRP, so a pooled envelope misses real fires at the
big one and fires constantly at the small one. A site is compared only with itself.

Median and MAD, never mean and standard deviation: FRP is heavily right-skewed,
and one past explosion would inflate sigma until the detector stopped firing at
that site. p99 is kept alongside because the anomaly test needs both conditions --
a z-score on MAD and an excess over p99 -- and they fail differently.

Keys use the instrument, never the satellite. Suomi NPP stops delivering on
1 Nov 2026 and NOAA-21 has little history; the three VIIRS units share one 375 m
algorithm, so their history pools. A bucket needs n >= 30, otherwise lookups fall
back to instrument|daynight, then to the source-wide ``*`` baseline, which is
always present.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from firewatch.registry.cells import utc

MIN_N = 30
SOURCE_WIDE = "*"
#: IMD's four seasons.
SEASON_OF_MONTH = {1: "winter", 2: "winter",
                   3: "pre_monsoon", 4: "pre_monsoon", 5: "pre_monsoon",
                   6: "monsoon", 7: "monsoon", 8: "monsoon", 9: "monsoon",
                   10: "post_monsoon", 11: "post_monsoon", 12: "post_monsoon"}


def season(month: int) -> str:
    return SEASON_OF_MONTH[int(month)]


def stats(frp: np.ndarray) -> dict[str, float]:
    """med, mad (unscaled), p99 and n of an FRP sample, NaN-safe."""
    frp = frp[np.isfinite(frp)]
    med = float(np.median(frp))
    return {"med": round(med, 3),
            "mad": round(float(np.median(np.abs(frp - med))), 3),
            "p99": round(float(np.quantile(frp, 0.99)), 3),
            "n": int(frp.size)}


def baselines(det: pd.DataFrame) -> dict[int, dict[str, dict[str, float]]]:
    """Every source's baselines. ``det`` carries ``source`` (>= 0 when assigned),
    ``instrument``, ``daynight``, ``acq_datetime`` and ``frp``.

    Only buckets with n >= ``MIN_N`` are stored, except ``*``, which is stored for
    every source whatever its size so that a lookup always finds something.
    """
    det = det[(det["source"] >= 0) & np.isfinite(det["frp"].to_numpy(dtype=float))]
    frame = pd.DataFrame({
        "source": det["source"].to_numpy(), "instrument": det["instrument"].to_numpy(),
        "dn": det["daynight"].to_numpy(),
        "season": utc(det["acq_datetime"]).month.map(SEASON_OF_MONTH).to_numpy(),
        "frp": det["frp"].to_numpy(dtype=float),
    })
    out: dict[int, dict[str, dict[str, float]]] = {}
    for source, g in frame.groupby("source", sort=True):
        buckets = {SOURCE_WIDE: stats(g["frp"].to_numpy())}
        for (inst, dn), gi in g.groupby(["instrument", "dn"]):
            if len(gi) >= MIN_N:
                buckets[f"{inst}|{dn}"] = stats(gi["frp"].to_numpy())
            for ssn, gs in gi.groupby("season"):
                if len(gs) >= MIN_N:
                    buckets[f"{inst}|{dn}|{ssn}"] = stats(gs["frp"].to_numpy())
        out[int(source)] = buckets
    return out


def lookup(buckets: dict[str, dict[str, float]], instrument: str, daynight: str,
           month: int) -> tuple[str, dict[str, float]]:
    """The baseline a new detection is judged against, and which key supplied it.

    instrument|daynight|season, else instrument|daynight, else source-wide.
    """
    for key in (f"{instrument}|{daynight}|{season(month)}", f"{instrument}|{daynight}",
                SOURCE_WIDE):
        if key in buckets:
            return key, buckets[key]
    raise KeyError("baselines carry no source-wide '*' bucket")
