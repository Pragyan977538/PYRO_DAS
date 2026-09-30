"""Road C: is this pass abnormal for this site?

Each pass at a registry source -- its hottest pixel -- is judged against that
source's own baseline, never a type's, with median and MAD (docs/DESIGN.md §3.3-3.4):

    z       = 0.6745 * (frp - med) / max(mad, 1e-6)
    breach  = z > 3.5  and frp > 1.5 * p99          fixed: they fail differently
    extreme = z > Z_EX and frp > P99_EX * p99       config; starting values 7 and 3

- **Confirmed** alert: a breach on two consecutive passes at the source -- this
  pass and the previous detection pass there, from any sensor, with no normal
  pass between. No clock: a weak source is not detected on every overpass, and on
  real 2024 histories a 24 h limit cut recall from 90% to 55% while false
  positives barely moved (0.059% -> 0.047%). The protection is "consecutive".
- A pass is judged only against **its own instrument's** baseline
  (``lookup(..., source_wide=False)``); with none, it cannot breach.
- **Provisional** alert: one extreme pass. Short blasts, the deadliest events,
  often burn out between passes.

Everything else is Road B: "neither alert tier fired", never "below the median".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from firewatch.registry.baseline import lookup

Z_BREACH = 3.5
P99_BREACH = 1.5


@dataclass(frozen=True)
class Thresholds:
    z_extreme: float = 7.0
    p99_extreme: float = 3.0

    @classmethod
    def from_settings(cls) -> Thresholds:
        from firewatch.config import settings
        z, mult = settings().anomaly_extreme
        return cls(z_extreme=z, p99_extreme=mult)


def score(passes: pd.DataFrame, baselines: dict[int, dict]) -> pd.DataFrame:
    """Add each pass's baseline and its z-score and p99 ratio.

    ``passes``: ``source``, ``instrument``, ``daynight``, ``acq_datetime``, ``frp``
    (the pass's hottest pixel). A source with no baseline at all gets NaNs and can
    never breach -- it has nothing to be abnormal against.
    """
    out = passes.copy()
    months = pd.DatetimeIndex(out["acq_datetime"]).month
    keys, med, mad, p99 = [], [], [], []
    for src, inst, dn, month in zip(out["source"], out["instrument"], out["daynight"],
                                    months, strict=True):
        buckets = baselines.get(int(src))
        if not buckets:
            keys.append(None)
            med.append(np.nan)
            mad.append(np.nan)
            p99.append(np.nan)
            continue
        key, b = lookup(buckets, str(inst), str(dn), int(month), source_wide=False)
        if b is None:
            keys.append(None)
            med.append(np.nan)
            mad.append(np.nan)
            p99.append(np.nan)
            continue
        keys.append(key)
        med.append(b["med"])
        mad.append(b["mad"])
        p99.append(b["p99"])
    out["baseline_key"] = keys
    out["med"] = np.asarray(med, dtype=float)
    out["mad"] = np.asarray(mad, dtype=float)
    out["p99"] = np.asarray(p99, dtype=float)
    out["z"] = 0.6745 * (out["frp"] - out["med"]) / np.maximum(out["mad"], 1e-6)
    out["ratio_p99"] = out["frp"] / out["p99"].where(out["p99"] > 0)
    return out


def tiers(scored: pd.DataFrame, thresholds: Thresholds,
          last: dict[int, tuple[pd.Timestamp, bool]] | None = None) -> pd.DataFrame:
    """Breach, extreme and the alert tier of every pass, in time order per source.

    ``last`` carries each source's previous pass (time, breached) across batches,
    so a breach on the last pass of one batch confirms with the first of the next.
    It is updated in place.
    """
    last = {} if last is None else last
    out = scored.sort_values(["source", "acq_datetime"], kind="stable").copy()
    z, frp, p99 = out["z"], out["frp"], out["p99"]
    out["breach"] = ((z > Z_BREACH) & (frp > P99_BREACH * p99)).fillna(False).astype(bool)
    out["extreme"] = ((z > thresholds.z_extreme)
                      & (frp > thresholds.p99_extreme * p99)).fillna(False).astype(bool)

    # Only judged passes form the sequence: a pass with no baseline for its
    # instrument is unknown, not normal, so it neither confirms nor breaks a chain.
    out["alert"] = None
    judged = out["baseline_key"].notna().to_numpy()
    j = out[judged]
    if len(j):
        g = j.groupby("source")
        prev = g["breach"].shift(fill_value=False).astype(bool)
        first = (g.cumcount() == 0).to_numpy()
        if first.any():
            prev.loc[first] = [last.get(int(s), (None, False))[1]
                               for s in j.loc[first, "source"]]
        confirmed = j["breach"].to_numpy() & prev.to_numpy(dtype=bool)
        out.loc[judged, "alert"] = np.where(confirmed, "confirmed",
                                            np.where(j["extreme"], "provisional", None))
        for src, gg in g:
            row = gg.iloc[-1]
            last[int(src)] = (row["acq_datetime"], bool(row["breach"]))
    return out


def reason(row) -> str:
    """The map's words for a pass at a registry source."""
    if not row["baseline_key"]:
        return (f"at a registered source, but no {row['instrument']} baseline here yet: "
                "this pass is not judged")
    head = (f"hottest pixel {row['frp']:.1f} MW against this site's "
            f"{row['baseline_key']} baseline (median {row['med']:.1f}, p99 {row['p99']:.1f})")
    if row["alert"] == "confirmed":
        return (f"CONFIRMED anomaly: {head}; z = {row['z']:.1f}, {row['ratio_p99']:.1f}x p99, "
                "second consecutive breaching pass")
    if row["alert"] == "provisional":
        return (f"PROVISIONAL anomaly: {head}; z = {row['z']:.1f}, {row['ratio_p99']:.1f}x p99 "
                "on a single pass")
    if row["breach"]:
        return (f"watch: {head}; z = {row['z']:.1f} breaches, but one pass does not confirm "
                "-- a second consecutive breach will")
    return f"normal for this site: {head}"
