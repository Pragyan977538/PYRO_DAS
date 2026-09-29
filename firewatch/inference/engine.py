"""Run the router, Road A, Road C and promotion over detections, in time order.

The same engine serves the live three-hourly batch and a replay of history. A
replay walks UTC day by UTC day, so promotion happens in simulated time: a site
is promoted on the day it qualified, and its later fires alert, exactly as they
would have live. Baselines for a replay are computed from history before the
window (``baselines_as_of``): a pass is never judged against a baseline that
already contains it.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from firewatch.db import fetch_all, get_conn
from firewatch.inference import ROAD_A, ROAD_B, ROAD_C
from firewatch.inference import anomaly as an
from firewatch.inference.promotion import RETIRE_DAYS, Promoter
from firewatch.inference.road_a import CATEGORY, Context, classify
from firewatch.inference.router import Router
from firewatch.registry.baseline import baselines as compute_baselines
from firewatch.registry.baseline import pass_ids, pass_table
from firewatch.registry.cells import add_metres, utc_day

log = logging.getLogger(__name__)
EPOCH = pd.Timestamp(0, tz="UTC")
OUT_COLUMNS = ["detection_id", "source_id", "road", "pred_class", "pred_conf", "category",
               "reason", "alert"]


def day_to_ts(day: int) -> pd.Timestamp:
    return EPOCH + pd.Timedelta(days=int(day))


class ClearSky:
    """Clear-sky fraction by UTC day near a point, from the observability table.

    A day is observable if either overpass was clear: with separate day and night
    values (the fixture) that is 1 - (1 - clear_D)(1 - clear_N); NASA POWER's daily
    value is used as it is. The same grids, in the same order, as the registry's
    persistence (``registry.build.CLOUD_GRIDS``).
    """

    def __init__(self, rows: pd.DataFrame) -> None:
        from firewatch.registry.build import CLOUD_GRIDS

        self.grids = CLOUD_GRIDS
        self.series: dict[tuple[str, int], pd.Series] = {}
        if rows.empty:
            return
        rows = rows.assign(day=utc_day(pd.to_datetime(rows["obs_date"])),
                           clear=1.0 - rows["cloud_frac"].astype(float))
        for (grid, cell), g in rows.groupby(["source", "cell_id"]):
            wide = g.pivot_table(index="day", columns="daynight", values="clear")
            if "A" in wide:
                s = wide["A"]
            else:
                s = 1 - (1 - wide.get("D", 0.0)) * (1 - wide.get("N", 0.0))
            self.series[(grid, int(cell))] = s.dropna().sort_index()

    def __call__(self, x: float, y: float) -> pd.Series | None:
        from firewatch.grid import from_metres, grid_cell

        lat, lon = (float(v[0]) for v in from_metres([x], [y]))
        for grid, deg in self.grids.items():
            for dy, dx in sorted(((a, b) for a in (-1, 0, 1) for b in (-1, 0, 1)),
                                 key=lambda s: abs(s[0]) + abs(s[1])):
                cell = int(grid_cell(lat + dy * deg, lon + dx * deg, deg))
                if (grid, cell) in self.series:
                    return self.series[(grid, cell)]
        return None

    @classmethod
    def from_db(cls, start: pd.Timestamp, end: pd.Timestamp) -> ClearSky:
        from sqlalchemy import text

        from firewatch.db import engine

        with engine().connect() as conn:
            rows = pd.read_sql_query(text(
                "SELECT source, cell_id, obs_date, daynight, cloud_frac FROM observability "
                "WHERE obs_date >= :s AND obs_date < :e"), conn,
                params={"s": (start - pd.Timedelta(days=70)).date(), "e": end.date()})
        return cls(rows)


@dataclass
class Result:
    detections: pd.DataFrame
    passes: pd.DataFrame
    promotions: list[dict] = field(default_factory=list)
    retirements: list[dict] = field(default_factory=list)


class Engine:
    def __init__(self, router: Router, baselines: dict[int, dict], sources: pd.DataFrame,
                 context: Context, thresholds: an.Thresholds,
                 new_id: Callable[[], int], start_day: int | None = None,
                 clear: ClearSky | None = None) -> None:
        """``sources``: ``cls`` and ``cls_conf`` (and ``promoted_at`` for live
        provisional ones) by source id. ``new_id`` hands out ids for promoted
        sources. Provisional sources already live start their 90-day retirement
        clock at ``start_day``."""
        self.router, self.baselines, self.context = router, baselines, context
        self.thresholds, self.new_id, self.clear = thresholds, new_id, clear
        self.meta = sources[["cls", "cls_conf"]].copy()
        self.last_pass: dict[int, tuple[pd.Timestamp, bool]] = {}
        self.promoter = Promoter()
        self.last_seen: dict[int, int] = {}
        self.promoted_day: dict[int, int] = {}
        if "promoted_at" in sources and start_day is not None:
            for sid in router.provisional:
                at = sources["promoted_at"].get(sid)
                self.promoted_day[sid] = (int(utc_day(pd.DatetimeIndex([at]))[0])
                                          if pd.notna(at) else start_day)
                self.last_seen[sid] = start_day

    # ---------------------------------------------------------------- one batch

    def _batch(self, b: pd.DataFrame, parts: list, pass_parts: list) -> None:
        src = self.router.route(b["x"].to_numpy(), b["y"].to_numpy(), b["instrument"].to_numpy())
        out = pd.DataFrame(index=b.index, columns=OUT_COLUMNS[1:], dtype=object)
        out["source_id"] = np.where(src >= 0, src, None)
        prov = np.isin(src, list(self.router.provisional))
        reg = (src >= 0) & ~prov

        if prov.any():
            ids = src[prov]
            cls = self.meta["cls"].reindex(ids).to_numpy()
            since = [day_to_ts(self.promoted_day.get(int(i), 0)).date() for i in ids]
            out.loc[prov, "road"] = ROAD_C
            out.loc[prov, "alert"] = "new_source"
            out.loc[prov, "pred_class"] = cls
            out.loc[prov, "category"] = [CATEGORY.get(c, "unclassified") for c in cls]
            out.loc[prov, "reason"] = [
                f"provisional source since {d}: this site had no fire history before, and "
                "it alerts on every pass until an analyst confirms it" for d in since]
            for i in np.unique(ids):
                self.last_seen[int(i)] = int(b["day"].iat[0])

        if reg.any():
            r = b.loc[reg].assign(source=src[reg])
            pid = pass_ids(r)
            passes = pass_table(r)
            scored = an.tiers(an.score(passes, self.baselines), self.thresholds,
                              self.last_pass)
            scored["reason"] = [an.reason(row) for _, row in scored.iterrows()]
            by_pass = scored.set_index("pass_id")
            alert = by_pass["alert"].reindex(pid).to_numpy()
            ids = src[reg]
            out.loc[reg, "road"] = np.where(pd.notna(alert), ROAD_C, ROAD_B)
            out.loc[reg, "alert"] = alert
            out.loc[reg, "reason"] = by_pass["reason"].reindex(pid).to_numpy()
            out.loc[reg, "pred_class"] = self.meta["cls"].reindex(ids).to_numpy()
            out.loc[reg, "pred_conf"] = self.meta["cls_conf"].reindex(ids).to_numpy()
            out.loc[reg, "category"] = "industrial"
            pass_parts.append(scored)

        road_a = src < 0
        if road_a.any():
            a = b.loc[road_a]
            ra = classify(a, self.context)
            out.loc[road_a, "road"] = ROAD_A
            out.loc[road_a, ["pred_class", "category", "reason"]] = ra[
                ["pred_class", "category", "reason"]].to_numpy()
            self.promoter.add(a.assign(pred_class=ra["pred_class"].to_numpy()))

        out.insert(0, "detection_id", b["detection_id"].to_numpy())
        parts.append(out)

    # ---------------------------------------------------------------- the run

    def run(self, det: pd.DataFrame) -> Result:
        """Process ``det`` (``detection_id``, ``acq_datetime``, ``latitude``,
        ``longitude``, ``sensor``, ``instrument``, ``daynight``, ``frp``) day by day."""
        det = add_metres(det.copy())
        det["day"] = utc_day(det["acq_datetime"])
        parts, pass_parts, promotions, retirements = [], [], [], []
        for day, b in det.sort_values("acq_datetime", kind="stable").groupby("day", sort=True):
            self._batch(b, parts, pass_parts)
            for p in self.promoter.check(int(day), self.clear):
                sid = self.new_id()
                self.router.add(sid, p.cells)
                self.meta.loc[sid] = [p.cls, np.nan]
                self.promoted_day[sid] = int(day)
                self.last_seen[sid] = p.last_day
                promotions.append({"source_id": sid, "promoted_at": day_to_ts(day + 1),
                                   "first_day": day_to_ts(p.first_day).date(), "days": p.days,
                                   "persistence": p.persistence,
                                   "cls": p.cls, "width_km": p.width_km, "cells": p.cells})
            for sid, seen in list(self.last_seen.items()):
                if day - seen > RETIRE_DAYS and sid in self.router.provisional:
                    self.router.remove(sid)
                    retirements.append({"source_id": sid, "retired_at": day_to_ts(day + 1)})
                    del self.last_seen[sid]
        detections = pd.concat(parts) if parts else pd.DataFrame(columns=OUT_COLUMNS)
        passes = pd.concat(pass_parts, ignore_index=True) if pass_parts else pd.DataFrame()
        return Result(detections=detections.reset_index(drop=True), passes=passes,
                      promotions=promotions, retirements=retirements)


# ------------------------------------------------------------------------ reads

_COLUMNS = ("detection_id, extract(epoch FROM acq_datetime)::bigint AS t, latitude, "
            "longitude, sensor, instrument, daynight, frp")
_DTYPES = {"detection_id": "int64", "t": "int64", "latitude": "float64",
           "longitude": "float64", "sensor": "category", "instrument": "category",
           "daynight": "category", "frp": "float64"}


def _copy_frame(where: str, params: tuple, extra: str = "") -> pd.DataFrame:
    with get_conn() as conn, conn.cursor() as cur:
        sql = cur.mogrify(f"COPY (SELECT {_COLUMNS}{extra} FROM detections WHERE {where}) "
                          "TO STDOUT WITH (FORMAT csv, HEADER)", params)
        buf = io.BytesIO()
        cur.copy_expert(sql.decode(), buf)
    buf.seek(0)
    frame = pd.read_csv(buf, dtype=_DTYPES)
    frame["acq_datetime"] = pd.to_datetime(frame.pop("t"), unit="s", utc=True)
    return frame


def read_window(start: pd.Timestamp, end: pd.Timestamp,
                bbox: tuple[float, float, float, float] | None = None) -> pd.DataFrame:
    where = "acq_datetime >= %s AND acq_datetime < %s"
    params: tuple = (start.to_pydatetime(), end.to_pydatetime())
    if bbox:
        where += " AND longitude BETWEEN %s AND %s AND latitude BETWEEN %s AND %s"
        params += (bbox[0], bbox[2], bbox[1], bbox[3])
    return _copy_frame(where, params)


def baselines_as_of(start: pd.Timestamp) -> dict[int, dict]:
    """Every registry source's baselines from its passes before ``start``."""
    hist = _copy_frame(
        "source_id IN (SELECT source_id FROM sources WHERE NOT provisional) "
        "AND acq_datetime < %s", (start.to_pydatetime(),), extra=", source_id AS source")
    return compute_baselines(pass_table(hist))


def source_meta() -> pd.DataFrame:
    rows = fetch_all("SELECT source_id, cls, cls_conf, promoted_at FROM sources")
    return pd.DataFrame(rows, columns=["source_id", "cls", "cls_conf",
                                       "promoted_at"]).set_index("source_id")


# ------------------------------------------------------------------------ write

def db_id_allocator() -> Callable[[], int]:
    def new_id() -> int:
        return fetch_all("SELECT nextval(pg_get_serial_sequence('sources', 'source_id')) "
                         "AS id")[0]["id"]
    return new_id


def dry_id_allocator(start: int = 10_000_000) -> Callable[[], int]:
    counter = iter(range(start, start + 1_000_000))
    return lambda: next(counter)


def clear_window(start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Undo an earlier run over the same window, so a replay starts clean."""
    s, e = start.to_pydatetime(), end.to_pydatetime()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_id FROM sources WHERE origin = 'promotion' "
                    "AND promoted_at >= %s AND promoted_at < %s", (s, e))
        stale = [r[0] for r in cur.fetchall()]
        if stale:
            cur.execute("UPDATE detections SET source_id = NULL WHERE source_id = ANY(%s)",
                        (stale,))
            cur.execute("DELETE FROM sources WHERE source_id = ANY(%s)", (stale,))
        cur.execute("UPDATE sources SET retired_at = NULL WHERE retired_at >= %s "
                    "AND retired_at < %s", (s, e))
        cur.execute("DELETE FROM source_passes WHERE pass_time >= %s AND pass_time < %s", (s, e))


def write(result: Result, start: pd.Timestamp, end: pd.Timestamp,
          thresholds: an.Thresholds) -> int:
    """Persist a run: promoted sources, scored passes, and every detection's road."""
    from firewatch.grid import from_metres

    with get_conn() as conn, conn.cursor() as cur:
        for p in result.promotions:
            cells = p["cells"]
            w = cells["n_det"].to_numpy(dtype=float)
            lat, lon = from_metres([np.average(cells["x"], weights=w)],
                                   [np.average(cells["y"], weights=w)])
            cur.execute("""
                INSERT INTO sources (source_id, geom, n_detections, first_seen, cls,
                                     label_source, provisional, origin, promoted_at, baselines)
                VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s,
                        'promotion', TRUE, 'promotion', %s, '{}'::jsonb)""",
                        (p["source_id"], float(lon[0]), float(lat[0]), int(w.sum()),
                         p["first_day"], p["cls"], p["promoted_at"].to_pydatetime()))
            clat, clon = from_metres(cells["x"].to_numpy(), cells["y"].to_numpy())
            for (_, c), la, lo in zip(cells.iterrows(), clat, clon, strict=True):
                cur.execute("""
                    INSERT INTO source_cells (source_id, cell_id, n_det, x_m, y_m, geom)
                    VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
                            (p["source_id"], int(c["cell"]), int(c["n_det"]), float(c["x"]),
                             float(c["y"]), float(lo), float(la)))
        for r in result.retirements:
            cur.execute("UPDATE sources SET retired_at = %s WHERE source_id = %s",
                        (r["retired_at"].to_pydatetime(), r["source_id"]))

        if len(result.passes):
            ps = result.passes
            frame = pd.DataFrame({
                "source_id": ps["source"].astype(int), "sensor": ps["sensor"],
                "pass_time": ps["acq_datetime"], "instrument": ps["instrument"],
                "daynight": ps["daynight"], "n_det": ps["n_det"].astype(int),
                "frp": ps["frp"], "frp_sum": ps["frp_sum"], "baseline_key": ps["baseline_key"],
                "med": ps["med"], "p99": ps["p99"], "z": ps["z"].round(3),
                "breach": ps["breach"], "extreme": ps["extreme"], "alert": ps["alert"]})
            _copy(cur, "source_passes", frame)

        d = result.detections.copy()
        d["source_id"] = pd.array(d["source_id"], dtype="Int64")
        d["road"] = pd.array(d["road"], dtype="Int64")
        d["pred_conf"] = pd.to_numeric(d["pred_conf"], errors="coerce").round(4)
        cur.execute("CREATE TEMP TABLE road_stage (detection_id bigint, source_id int, "
                    "road smallint, pred_class text, pred_conf real, category text, "
                    "reason text, alert text) ON COMMIT DROP")
        _copy(cur, "road_stage", d[OUT_COLUMNS])
        cur.execute("CREATE INDEX ON road_stage (detection_id); ANALYZE road_stage")
        cur.execute("""
            UPDATE detections d SET source_id = r.source_id, road = r.road,
                   pred_class = r.pred_class, pred_conf = r.pred_conf, category = r.category,
                   reason = r.reason, alert = r.alert
              FROM road_stage r
             WHERE d.detection_id = r.detection_id
               AND d.acq_datetime >= %s AND d.acq_datetime < %s""",
                    (start.to_pydatetime(), end.to_pydatetime()))
        updated = cur.rowcount
        stats = summarise(result)
        cur.execute("INSERT INTO inference_runs (window_start, window_end, thresholds, stats) "
                    "VALUES (%s, %s, %s, %s) RETURNING run_id",
                    (start.to_pydatetime(), end.to_pydatetime(),
                     json.dumps({"z_extreme": thresholds.z_extreme,
                                 "p99_extreme": thresholds.p99_extreme,
                                 "z_breach": an.Z_BREACH, "p99_breach": an.P99_BREACH}),
                     json.dumps({**stats, "detections_updated": updated}, default=str)))
        return cur.fetchone()[0]


def _copy(cur, table: str, frame: pd.DataFrame) -> None:
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False, date_format="%Y-%m-%d %H:%M:%S%z")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(frame.columns)}) FROM STDIN WITH (FORMAT csv)",
                    buf)


def summarise(result: Result) -> dict:
    d = result.detections
    ps = result.passes
    return {
        "detections": int(len(d)),
        "by_road": {str(k): int(v) for k, v in d["road"].value_counts().items()},
        "by_category": {str(k): int(v) for k, v in d["category"].value_counts().items()},
        "road_a_without_reason": int(((d["road"] == ROAD_A)
                                      & d["reason"].fillna("").eq("")).sum()),
        "passes": int(len(ps)),
        "alerts": {str(k): int(v) for k, v in ps["alert"].value_counts().items()}
        if len(ps) else {},
        "new_source_detections": int((d["alert"] == "new_source").sum()),
        "promotions": len(result.promotions),
        "retirements": len(result.retirements),
    }

