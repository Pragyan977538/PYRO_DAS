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


#: Detections of one sensor at one source this close in time are one overpass.
PASS_GAP = pd.Timedelta(minutes=20)


def season(month: int) -> str:
    return SEASON_OF_MONTH[int(month)]


def pass_ids(det: pd.DataFrame) -> np.ndarray:
    """A pass id per row of ``det``, in its own order; -1 off a source.

    A pass is one sensor's detections at one source within ``PASS_GAP``: one
    overpass. Ids increase with (source, sensor, time).
    """
    d = det.loc[det["source"] >= 0].sort_values(["source", "sensor", "acq_datetime"],
                                                kind="stable")
    out = pd.Series(-1, index=det.index, dtype=np.int64)
    if d.empty:
        return out.to_numpy()
    t = pd.Series(utc(d["acq_datetime"]), index=d.index)
    src, sensor = d["source"], d["sensor"].astype(str)
    new = (src != src.shift()) | (sensor != sensor.shift()) | (t - t.shift() > PASS_GAP)
    out.loc[d.index] = new.cumsum().to_numpy()
    return out.to_numpy()


def pass_table(det: pd.DataFrame) -> pd.DataFrame:
    """Detections at sources, grouped into passes.

    The pass's ``frp`` is its hottest pixel. Baselines and the anomaly test both
    work on this unit: a pixel-level baseline would flag every large multi-pixel
    site on most passes (the most extreme of k pixels is not one pixel), and a pass
    total would drown a one-pixel fire at a site with many pixels.

    ``det`` needs ``source`` (>= 0 at a source), ``sensor``, ``instrument``,
    ``daynight``, ``acq_datetime`` and ``frp``. Returns one row per pass:
    ``pass_id`` (as ``pass_ids``), ``source``, ``sensor``, ``instrument``,
    ``daynight``, ``acq_datetime`` (first pixel), ``frp`` (max), ``frp_sum``, ``n_det``.
    """
    pid = pass_ids(det)
    on = pid >= 0
    if not on.any():
        return pd.DataFrame(columns=["pass_id", "source", "sensor", "instrument", "daynight",
                                     "acq_datetime", "frp", "frp_sum", "n_det"])
    d = det.loc[on]
    frame = pd.DataFrame({"pass_id": pid[on], "source": d["source"].to_numpy(),
                          "sensor": d["sensor"].astype(str).to_numpy(),
                          "instrument": d["instrument"].to_numpy(),
                          "daynight": d["daynight"].astype(str).to_numpy(),
                          "acq_datetime": utc(d["acq_datetime"]),
                          "frp": d["frp"].to_numpy(dtype=float)})
    passes = frame.groupby("pass_id", sort=True).agg(
        source=("source", "first"), sensor=("sensor", "first"),
        instrument=("instrument", "first"), daynight=("daynight", "first"),
        acq_datetime=("acq_datetime", "min"), frp=("frp", "max"),
        frp_sum=("frp", "sum"), n_det=("frp", "size")).reset_index()
    passes["acq_datetime"] = utc(passes["acq_datetime"])
    return passes


def stats(frp: np.ndarray) -> dict[str, float]:
    """med, mad (unscaled), p99 and n of an FRP sample, NaN-safe."""
    frp = frp[np.isfinite(frp)]
    med = float(np.median(frp))
    return {"med": round(med, 3),
            "mad": round(float(np.median(np.abs(frp - med))), 3),
            "p99": round(float(np.quantile(frp, 0.99)), 3),
            "n": int(frp.size)}


def baselines(det: pd.DataFrame) -> dict[int, dict[str, dict[str, float]]]:
    """Every source's baselines, one sample per row of ``det`` -- in the registry,
    the rows are passes (``pass_table``). ``det`` carries ``source`` (>= 0 when
    assigned), ``instrument``, ``daynight``, ``acq_datetime`` and ``frp``.

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
           month: int, source_wide: bool = True) -> tuple[str | None, dict[str, float] | None]:
    """The baseline a new pass is judged against, and which key supplied it.

    instrument|daynight|season, else instrument|daynight, else source-wide.
    ``source_wide=False`` stops at the instrument: the anomaly test uses that,
    because the source-wide pool is mostly VIIRS and MODIS detects only the bigger
    fires, so a MODIS pass judged against it looks abnormal by construction. On
    2023 that one bias made up most of the confirmed false positives. Returns
    ``(None, None)`` when nothing qualifies.
    """
    keys = [f"{instrument}|{daynight}|{season(month)}", f"{instrument}|{daynight}"]
    for key in keys + ([SOURCE_WIDE] if source_wide else []):
        if key in buckets:
            return key, buckets[key]
    return None, None
