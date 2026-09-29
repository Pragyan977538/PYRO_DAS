"""Stage 5: Road C against spikes injected into real source histories.

    python scripts/check_injected.py

For a test year Y, every registry source's baseline is built from its passes
before Y, exactly as a live run would have it. Then:

* **False positives**: Y's real passes, untouched, go through the anomaly test.
  Any alert counts against us -- including real fires, which this cannot tell
  apart; the rates are upper bounds.
* **Recall**: up to 3 events per source with >= 40 passes in Y are injected into
  its real pass sequence (``fixture.inject_spikes``): 6-12x the source's median,
  on 2 consecutive real passes, with the source's own cloud gaps. An event is
  caught (confirmed) when its second pass raises a confirmed alert.

Also reported: how many events the breach test sees on the first pass, and how
many the p99 condition alone misses (heavy-tailed sources, where 6-12x the median
can sit below 1.5x p99 -- the condition is non-negotiable, so that is reported,
not tuned away).

Calibration, fixed in docs/ROADMAP.md before this ran: the extreme tier starts at
(z 7, 3x p99); on 2023 it is kept if its false positives are < 0.01% of passes,
else the grid setting that meets that with the best single-pass recall is taken.
All targets are then checked on 2024. Exits non-zero if one fails.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.inference import anomaly as an  # noqa: E402
from firewatch.inference.engine import _copy_frame  # noqa: E402
from firewatch.ingest.fixture import inject_spikes  # noqa: E402
from firewatch.registry.baseline import baselines, pass_table  # noqa: E402

OUT = REPO / "reports" / "stage5"
CALIBRATE, EVALUATE = 2023, 2024
GRID = list(itertools.product((7, 10, 15, 20), (3, 4, 5, 6)))
MIN_TEST_PASSES = 40
EVENTS_PER_SOURCE = 3
TARGETS = {"confirmed_recall": 0.90, "confirmed_fp": 0.001, "provisional_fp": 0.0001}


def registry_passes() -> pd.DataFrame:
    det = _copy_frame("source_id IN (SELECT source_id FROM sources WHERE NOT provisional)",
                      (), extra=", source_id AS source")
    return pass_table(det)


def year_split(passes: pd.DataFrame, year: int):
    t = passes["acq_datetime"]
    start = pd.Timestamp(year, 1, 1, tz="UTC")
    end = pd.Timestamp(year + 1, 1, 1, tz="UTC")
    return passes[t < start], passes[(t >= start) & (t < end)].reset_index(drop=True)


def injected(passes: pd.DataFrame, year: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The year's passes with spikes, and one truth row per event."""
    start = pd.Timestamp(year, 1, 1, tz="UTC")
    end = pd.Timestamp(year + 1, 1, 1, tz="UTC")
    rng = np.random.default_rng(seed)
    spiked_parts, truth = [], []
    for src, hist in passes[passes["acq_datetime"] < end].groupby("source"):
        hist = hist.sort_values("acq_datetime").reset_index(drop=True)
        n_test = int((hist["acq_datetime"] >= start).sum())
        if n_test < MIN_TEST_PASSES:
            continue
        holdout = 1 - n_test / len(hist)
        try:
            spiked, events = inject_spikes(hist, EVENTS_PER_SOURCE, passes=2, holdout=holdout,
                                           seed=rng)
        except ValueError:
            continue
        spiked_parts.append(spiked[spiked["acq_datetime"] >= start].assign(source=src))
        for ev in events.itertuples():
            truth.append({"source": src, "first": ev.rows[0], "second": ev.rows[1],
                          "multiplier": ev.multiplier,
                          "t_first": hist.loc[ev.rows[0], "acq_datetime"],
                          "t_second": hist.loc[ev.rows[1], "acq_datetime"]})
    return pd.concat(spiked_parts, ignore_index=False), pd.DataFrame(truth)


def evaluate(passes: pd.DataFrame, year: int, th: an.Thresholds, seed: int = 26162) -> dict:
    before, test = year_split(passes, year)
    base = baselines(before)
    clean = an.tiers(an.score(test, base), th)
    spiked, truth = injected(passes, year, seed)
    # Keep each source's own row labels, so the truth can find its passes.
    spiked = spiked.rename_axis("row").reset_index()
    tiered = an.tiers(an.score(spiked, base), th)
    key = tiered.set_index(["source", "row"])
    first = key.loc[list(zip(truth["source"], truth["first"], strict=True))]
    second = key.loc[list(zip(truth["source"], truth["second"], strict=True))]
    gap_h = (truth["t_second"] - truth["t_first"]).dt.total_seconds() / 3600
    caught = second["alert"].eq("confirmed").to_numpy()
    n = len(clean)
    return {
        "year": year, "passes": int(n), "sources_with_baseline": len(base),
        "confirmed_fp": round(float(clean["alert"].eq("confirmed").mean()), 6),
        "provisional_fp": round(float(clean["extreme"].mean()), 6),
        "confirmed_alerts": int(clean["alert"].eq("confirmed").sum()),
        "provisional_alerts": int(clean["extreme"].sum()),
        "events": int(len(truth)),
        "confirmed_recall": round(float(caught.mean()), 4),
        "provisional_recall_single_pass": round(float(first["extreme"].mean()), 4),
        "any_alert_recall": round(float((caught | first["extreme"].to_numpy()
                                         | second["extreme"].to_numpy()).mean()), 4),
        "breach_first_pass": round(float(first["breach"].mean()), 4),
        "events_gap_over_24h": int((gap_h > 24).sum()),
        "recall_within_24h": round(float(caught[(gap_h <= 24).to_numpy()].mean()), 4)
        if (gap_h <= 24).any() else None,
        "missed_by_p99_rule": int(((first["z"] > an.Z_BREACH)
                                   & ~(first["frp"] > an.P99_BREACH * first["p99"])).sum()),
    }


def calibrate(passes: pd.DataFrame) -> dict:
    rows = []
    for z, m in GRID:
        r = evaluate(passes, CALIBRATE, an.Thresholds(z, m))
        rows.append({"z": z, "p99": m, "provisional_fp": r["provisional_fp"],
                     "provisional_recall": r["provisional_recall_single_pass"]})
        print(f"   z>{z:>2} p99x{m}: provisional FP {r['provisional_fp']:.4%}, "
              f"single-pass recall {r['provisional_recall_single_pass']:.1%}")
    start = next(r for r in rows if (r["z"], r["p99"]) == (7, 3))
    ok = [r for r in rows if r["provisional_fp"] < TARGETS["provisional_fp"]]
    if start["provisional_fp"] < TARGETS["provisional_fp"]:
        chosen, why = start, "starting values meet the target: kept"
    elif ok:
        chosen = max(ok, key=lambda r: (r["provisional_recall"], -r["z"], -r["p99"]))
        why = "starting values miss the target; best-recall passing setting taken"
    else:
        chosen = min(rows, key=lambda r: r["provisional_fp"])
        why = "no setting meets the target; lowest false-positive setting taken"
    return {"grid": rows, "chosen": {"z": chosen["z"], "p99": chosen["p99"]}, "rule": why}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    passes = registry_passes()
    print(f">> {len(passes):,} passes at {passes['source'].nunique()} registry sources")
    print(f">> calibrating the extreme tier on {CALIBRATE}")
    cal = calibrate(passes)
    th = an.Thresholds(float(cal["chosen"]["z"]), float(cal["chosen"]["p99"]))
    print(f">> chosen: z > {th.z_extreme:g}, {th.p99_extreme:g}x p99 ({cal['rule']})")
    result = {"calibration": cal,
              "calibration_year": evaluate(passes, CALIBRATE, th),
              "evaluation_year": evaluate(passes, EVALUATE, th),
              "targets": TARGETS}
    ev = result["evaluation_year"]
    failures = []
    if ev["confirmed_recall"] <= TARGETS["confirmed_recall"]:
        failures.append(f"confirmed recall {ev['confirmed_recall']:.1%} <= 90%")
    if ev["confirmed_fp"] >= TARGETS["confirmed_fp"]:
        failures.append(f"confirmed FP {ev['confirmed_fp']:.3%} >= 0.1%")
    if ev["provisional_fp"] >= TARGETS["provisional_fp"]:
        failures.append(f"provisional FP {ev['provisional_fp']:.4%} >= 0.01%")
    result["failures"] = failures
    (OUT / "injected.json").write_text(json.dumps(result, indent=2, default=str),
                                       encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("calibration_year", "evaluation_year")}, indent=2,
                     default=str))
    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} target(s) missed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
