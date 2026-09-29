"""Stage 7: score every event of the latest inference run for risk.

    python scripts/score_risk.py                  # the latest run's events
    python scripts/score_risk.py --rebuild-assets # regenerate the asset register first

Writes ``events.risk_score`` and ``events.risk_breakdown`` (the decomposition, with
the raw values behind every term). Builds the critical asset register if it is
empty, and loads NASA POWER wind for the years it needs.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.db import engine, fetch_all, get_conn  # noqa: E402
from firewatch.grid import to_metres  # noqa: E402
from firewatch.registry.baseline import PASS_GAP  # noqa: E402
from firewatch.risk import assets as ra  # noqa: E402
from firewatch.risk import score as rs  # noqa: E402

log = logging.getLogger("score_risk")
REFERENCE_SAMPLE = 3.0      # percent of historical detections, for the reference ranks


def frame(sql: str, params: dict | None = None) -> pd.DataFrame:
    from sqlalchemy import text
    with engine().connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})


def read_events() -> tuple[pd.DataFrame, pd.Series]:
    run = frame("SELECT run_id, window_start, window_end FROM inference_runs "
                "ORDER BY run_id DESC LIMIT 1").iloc[0]
    events = frame("""
        SELECT e.event_id, ST_Y(e.centroid) AS lat, ST_X(e.centroid) AS lon, e.last_seen,
               e.kind, e.event_class, e.category, e.source_id, s.cls AS source_cls
          FROM events e LEFT JOIN sources s USING (source_id)
         WHERE e.first_seen >= :s AND e.first_seen < :e""",
                   {"s": run["window_start"], "e": run["window_end"]})
    return events, run


def pass_stats(run) -> pd.DataFrame:
    """Per event: peak pixel FRP, pixels in its biggest pass, and the last two
    passes' total FRP (for growth)."""
    det = frame("""
        SELECT event_id, sensor, acq_datetime, frp FROM detections
         WHERE event_id IS NOT NULL AND acq_datetime >= :s AND acq_datetime < :e""",
                {"s": run["window_start"], "e": run["window_end"]})
    det["acq_datetime"] = pd.to_datetime(det["acq_datetime"], utc=True)
    det = det.sort_values(["event_id", "sensor", "acq_datetime"], kind="stable")
    t = det["acq_datetime"]
    new = ((det["event_id"] != det["event_id"].shift()) | (det["sensor"] != det["sensor"].shift())
           | (t - t.shift() > PASS_GAP))
    det["pass"] = new.cumsum()
    passes = det.groupby("pass").agg(event_id=("event_id", "first"), t=("acq_datetime", "min"),
                                     n=("frp", "size"), total=("frp", "sum"))
    passes = passes.sort_values(["event_id", "t"])
    last2 = passes.groupby("event_id").tail(2)
    last = last2.groupby("event_id")["total"].last()
    first_of_two = last2.groupby("event_id")["total"].first()
    count = last2.groupby("event_id").size()
    return pd.DataFrame({
        "peak_frp": det.groupby("event_id")["frp"].max(),
        "pixels": passes.groupby("event_id")["n"].max(),
        "last_total": last,
        "previous_total": first_of_two.where(count == 2),
    })


def references(before: pd.Timestamp, pop) -> dict[str, np.ndarray]:
    """National history the ranks are taken against: FRP, and population around
    historical fire locations (downwind in a random direction)."""
    sample = frame(f"""
        SELECT latitude, longitude, frp FROM detections
          TABLESAMPLE SYSTEM ({REFERENCE_SAMPLE}) REPEATABLE (26162)
         WHERE acq_datetime < :b AND frp IS NOT NULL""", {"b": before})
    rng = np.random.default_rng(26162)
    lat, lon = sample["latitude"].to_numpy(), sample["longitude"].to_numpy()
    return {"frp": np.sort(sample["frp"].to_numpy(dtype=float)),
            "pop5": np.sort(pop.within_5km(lat, lon)),
            "downwind": np.sort(pop.downwind(lat, lon, rng.uniform(0, 360, len(sample)))),
            "n": len(sample)}


def wind_bearings(events: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Downwind bearing (degrees, the way the wind blows) on each event's last day."""
    from scipy.spatial import cKDTree

    from firewatch.ingest.wind import load_wind, read_wind

    days = events["last_seen"].dt.tz_convert("UTC").dt.date
    have = {r["y"] for r in fetch_all("SELECT DISTINCT extract(year FROM obs_date)::int AS y "
                                      "FROM wind_daily")}
    missing = sorted({d.year for d in days} - have)
    if missing:
        log.info("loading NASA POWER wind for %s", missing)
        load_wind(missing)
    wind = read_wind(days.unique())
    bearing = np.full(len(events), np.nan)
    if wind.empty:
        return bearing, days.to_numpy()
    grid = wind[["latitude", "longitude"]].drop_duplicates().reset_index(drop=True)
    _, idx = cKDTree(grid.to_numpy()).query(events[["lat", "lon"]].to_numpy())
    grid["point"] = np.arange(len(grid))
    wind = wind.merge(grid, on=["latitude", "longitude"])
    key = pd.DataFrame({"point": idx, "obs_date": days.to_numpy()})
    joined = key.merge(wind[["point", "obs_date", "u10m", "v10m"]], how="left",
                       on=["point", "obs_date"])
    b = np.degrees(np.arctan2(joined["u10m"].to_numpy(dtype=float),
                              joined["v10m"].to_numpy(dtype=float))) % 360
    return b, days.to_numpy()


def asset_terms(events: pd.DataFrame, pop) -> pd.DataFrame:
    """Nearest asset within 2 km, and the weighted count within 10 km (the nearest
    one excluded when it is what is burning)."""
    import shapely

    reg = frame("SELECT asset_id, name, asset_type, criticality, ST_Y(geom) AS lat, "
                "ST_X(geom) AS lon, ST_AsBinary(footprint) AS wkb FROM critical_assets")
    shapes = shapely.from_wkb(reg["wkb"].map(bytes).to_numpy())

    def metres(coords):
        x, y = to_metres(coords[:, 1], coords[:, 0])
        return np.column_stack([x, y])
    shapes = shapely.transform(shapes, metres)
    ex, ey = to_metres(events["lat"].to_numpy(), events["lon"].to_numpy())
    pts = shapely.points(ex, ey)
    (i_pt, i_asset), dist = shapely.STRtree(shapes).query_nearest(
        pts, max_distance=rs.V_RADIUS_M, return_distance=True, all_matches=False)
    out = pd.DataFrame(index=events.index)
    out["nearest_m"] = np.nan
    out["nearest_crit"] = np.nan
    out["nearest_name"] = None
    out["nearest_type"] = None
    pos = events.index[i_pt]
    out.loc[pos, "nearest_m"] = dist
    out.loc[pos, "nearest_crit"] = reg["criticality"].to_numpy()[i_asset]
    out.loc[pos, "nearest_name"] = reg["name"].to_numpy()[i_asset]
    out.loc[pos, "nearest_type"] = reg["asset_type"].to_numpy()[i_asset]
    weight = pop.disk_sum(pop.rasterise(reg["lat"], reg["lon"], reg["criticality"]),
                          rs.ASSET_RADIUS_KM)
    count = pop.disk_sum(pop.rasterise(reg["lat"], reg["lon"], np.ones(len(reg))),
                         rs.ASSET_RADIUS_KM)
    lat, lon = events["lat"].to_numpy(), events["lon"].to_numpy()
    burning = out["nearest_m"].notna().to_numpy()
    out["asset_weight"] = np.clip(pop.sample(weight, lat, lon)
                                  - np.where(burning, out["nearest_crit"].fillna(0), 0), 0, None)
    out["assets_10km"] = np.clip(np.round(pop.sample(count, lat, lon)) - burning, 0, None)
    return out


def write(events: pd.DataFrame) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE risk_stage (event_id int, risk real, breakdown jsonb) "
                    "ON COMMIT DROP")
        buf = io.StringIO()
        events[["event_id", "risk", "breakdown"]].to_csv(buf, index=False, header=False)
        buf.seek(0)
        cur.copy_expert("COPY risk_stage FROM STDIN WITH (FORMAT csv)", buf)
        cur.execute("UPDATE events e SET risk_score = r.risk, risk_breakdown = r.breakdown, "
                    "updated_at = now() FROM risk_stage r WHERE e.event_id = r.event_id")
        return cur.rowcount


def main() -> int:
    from firewatch.risk.population import population

    ap = argparse.ArgumentParser(description="Stage 7 risk scoring.")
    ap.add_argument("--rebuild-assets", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    t0 = time.time()
    if args.rebuild_assets or not fetch_all("SELECT 1 FROM critical_assets LIMIT 1"):
        log.info("asset register: %s", ra.build())
    events, run = read_events()
    events["last_seen"] = pd.to_datetime(events["last_seen"], utc=True)
    log.info("%d events in run %s", len(events), run["run_id"])
    pop = population()
    ref = references(run["window_start"], pop)
    stats = pass_stats(run).reindex(events["event_id"]).reset_index(drop=True)
    events = pd.concat([events, stats], axis=1)
    events["bearing"], events["wind_date"] = wind_bearings(events)
    events = pd.concat([events, asset_terms(events, pop)], axis=1)
    log.info("inputs ready in %.0fs", time.time() - t0)

    g, events["growth_word"] = rs.growth_score(events["last_total"], events["previous_total"])
    H = rs.hazard(events["peak_frp"], events["pixels"], g, ref["frp"])
    events["pop5"] = pop.within_5km(events["lat"], events["lon"])
    events["pop_downwind"] = pop.downwind(events["lat"], events["lon"], events["bearing"])
    E = rs.exposure(events["pop5"], events["pop_downwind"], events["asset_weight"],
                    ref["pop5"], ref["downwind"])
    events["own_class"] = events["source_cls"].where(events["source_id"].notna())
    own = events["own_class"].map(ra.CLASS_CRITICALITY)
    V = rs.vulnerability(events["nearest_crit"], events["nearest_m"], own)
    events["h"], events["e"], events["v"] = H["score"], E["score"], V
    events["frp_percentile"] = H["frp_percentile"]
    events["pop_5km_percentile"] = E["pop_5km_percentile"]
    events["risk"] = rs.risk(events["h"], events["e"], events["v"])
    events["breakdown"] = rs.breakdowns(events)
    summary = {
        "events": int(len(events)), "reference_detections": ref["n"],
        "risk_quantiles": {q: round(float(events["risk"].quantile(q)), 1)
                           for q in (0.5, 0.9, 0.99, 1.0)},
        "with_wind": int(np.isfinite(events["bearing"]).sum()),
        "near_asset_2km": int(events["nearest_m"].notna().sum()),
        "top": events.nlargest(10, "risk")[["event_id", "kind", "category", "risk",
                                            "nearest_name"]].to_dict("records"),
    }
    if not args.dry:
        summary["written"] = write(events)
    log.info("done in %.0fs", time.time() - t0)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
