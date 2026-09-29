"""Stage 7 acceptance: every score explains itself, and multiplication does its job.

    python scripts/check_risk.py

1. Every event of the latest run has a risk score and a decomposition carrying
   hazard, exposure and vulnerability sub-scores with the raw values behind each.
2. **A large fire far from people and assets scores low.** Among the run's
   high-hazard events (H >= 0.8), those with under 2,000 people within 5 km *and*
   under 2,000 in the downwind sector (smoke reaches people further than 5 km),
   no critical asset within 10 km and vulnerability at its floor must all score
   below 25 -- and far below high-hazard events at critical assets. The largest such
   fire is printed with what an additive score would have given it.
Exits non-zero if either fails; writes reports/stage7/check.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.db import fetch_all  # noqa: E402
from firewatch.risk import score as rs  # noqa: E402

OUT = REPO / "reports" / "stage7"
REMOTE_CEILING = 25.0
BIG = 0.8

SCORES = """
    WITH run AS (SELECT window_start, window_end FROM inference_runs
                  ORDER BY run_id DESC LIMIT 1)
    SELECT e.event_id, e.kind, e.category, e.reason, e.risk_score AS risk,
           (e.risk_breakdown->'hazard'->>'score')::float AS h,
           (e.risk_breakdown->'exposure'->>'score')::float AS e,
           (e.risk_breakdown->'vulnerability'->>'score')::float AS v,
           (e.risk_breakdown->'hazard'->>'peak_frp_mw')::float AS frp,
           (e.risk_breakdown->'exposure'->>'pop_5km')::int AS pop,
           (e.risk_breakdown->'exposure'->>'assets_10km')::int AS assets,
           coalesce((e.risk_breakdown->'exposure'->>'pop_downwind_10km')::int, 0)
               AS downwind,
           e.risk_breakdown AS breakdown
      FROM events e, run
     WHERE e.first_seen >= run.window_start AND e.first_seen < run.window_end
"""


def main() -> int:
    failures = []
    missing = fetch_all(f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE risk_score IS NULL OR risk_breakdown IS NULL) AS unscored,
               count(*) FILTER (WHERE NOT (risk_breakdown->'hazard' ? 'frp_percentile'
                                       AND risk_breakdown->'hazard' ? 'pixels'
                                       AND risk_breakdown->'exposure' ? 'pop_5km'
                                       AND risk_breakdown->'exposure' ? 'assets_10km'
                                       AND risk_breakdown->'vulnerability' ? 'distance_m'))
                   AS incomplete
          FROM ({SCORES}) s JOIN events USING (event_id)""")[0]
    if missing["unscored"] or missing["incomplete"]:
        failures.append(f"{missing['unscored']} unscored and {missing['incomplete']} "
                        "incomplete decompositions")

    groups = fetch_all(f"""
        SELECT CASE WHEN h >= {BIG} AND pop < 2000 AND downwind < 2000 AND assets = 0
                         AND v <= {rs.FLOOR}
                    THEN 'remote' WHEN h >= {BIG} AND v >= 0.5 THEN 'at_asset' END AS grp,
               count(*) AS n, max(risk) AS max_risk,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY risk) AS median_risk
          FROM ({SCORES}) s GROUP BY 1""")
    by = {g["grp"]: g for g in groups if g["grp"]}
    remote, at_asset = by.get("remote"), by.get("at_asset")
    biggest = fetch_all(f"""
        SELECT * FROM ({SCORES}) s
         WHERE h >= {BIG} AND pop < 2000 AND downwind < 2000 AND assets = 0
               AND v <= {rs.FLOOR}
         ORDER BY frp DESC LIMIT 1""")
    if not remote:
        failures.append("no large remote fire in the run to check")
    elif remote["max_risk"] >= REMOTE_CEILING:
        failures.append(f"a large remote fire scored {remote['max_risk']:.1f} >= "
                        f"{REMOTE_CEILING:g}")
    if remote and at_asset and not at_asset["median_risk"] > 3 * remote["median_risk"]:
        failures.append("fires at critical assets do not clearly outrank remote ones")

    example = None
    if biggest:
        b = biggest[0]
        additive = 100 * (rs.WEIGHTS["hazard"] * b["h"] + rs.WEIGHTS["exposure"] * b["e"]
                          + rs.WEIGHTS["vulnerability"] * b["v"])
        example = {"event_id": b["event_id"], "reason": b["reason"],
                   "peak_frp_mw": b["frp"], "risk": round(b["risk"], 1),
                   "additive_would_give": round(additive, 1), "breakdown": b["breakdown"]}
    result = {"events": missing["n"], "unscored": missing["unscored"],
              "incomplete": missing["incomplete"], "groups": by,
              "largest_remote_fire": example, "failures": failures}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "check.json").write_text(json.dumps(result, indent=2, default=str),
                                    encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
