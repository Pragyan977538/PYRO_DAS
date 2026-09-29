"""Event assembly: from detections to incidents.

A fire is seen as many detections: several pixels on one pass, again on the next
pass, and for days if it keeps burning. The map, the alerts and the risk score all
need the *incident*, so detections are assembled into events:

- **Road B makes no events.** A plant operating normally is not an incident.
- **Anomaly** (Road C at a registry source): one event per source. An alert within
  72 h of its last alert extends it, and a later one opens a new event.
- **New source** (a provisional source): one event for the source's whole life,
  whatever the gaps, until it is retired or confirmed. It includes the Road A
  detections that got it promoted, so a blowout is one incident from its first
  detection, not from the day it was promoted.
- **Fire** (Road A): a UTC day's detections are clustered at 750 m (two VIIRS pixels).
  A cluster joins an open event within 750 m of that event's detections from the
  last 72 h, if the joined event stays <= 10 km wide; otherwise it starts one. The
  cap is what stops a week of 750 m links chaining the paddy belt into one "event".

Lifecycle, as of the run's end: active within 24 h of the last detection; dormant
up to 72 h, when a new detection still re-ignites the same event; closed after.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from firewatch.grid import from_metres
from firewatch.inference import ROAD_A, ROAD_B

log = logging.getLogger(__name__)

LINK_M = 750.0
REIGNITE = pd.Timedelta(hours=72)
DORMANT_AFTER = pd.Timedelta(hours=24)
MAX_EVENT_KM = 10.0
PRE_PROMOTION_M = 500.0
HALF_PIXEL_M = 190.0
ALERT_RANK = {"provisional": 1, "confirmed": 2, "new_source": 3}


@dataclass
class _Event:
    """What linking needs to know about an event: its kind, span and extent.

    Counts, peaks, classes and days are computed once, at the end, from the
    detections assigned to it -- keeping per-cluster work to a few comparisons.
    Times are nanoseconds since the epoch.
    """

    id: int
    kind: str
    source_id: int | None
    first: int
    last: int
    x0: float = np.inf
    x1: float = -np.inf
    y0: float = np.inf
    y1: float = -np.inf

    def width_with(self, x0: float, x1: float, y0: float, y1: float) -> float:
        return float(np.hypot(max(self.x1, x1) - min(self.x0, x0),
                              max(self.y1, y1) - min(self.y0, y0))) / 1000

    def extend(self, t0: int, t1: int, x0: float, x1: float, y0: float, y1: float) -> None:
        self.first, self.last = min(self.first, t0), max(self.last, t1)
        self.x0, self.x1 = min(self.x0, x0), max(self.x1, x1)
        self.y0, self.y1 = min(self.y0, y0), max(self.y1, y1)


_EXTENT = {"t0": ("t", "min"), "t1": ("t", "max"), "x0": ("x", "min"), "x1": ("x", "max"),
           "y0": ("y", "min"), "y1": ("y", "max")}


class Assembler:
    """Stateful: feed it days in order (``add``), then read ``result()``."""

    def __init__(self, provisional: dict[int, pd.Timestamp | None] | None = None) -> None:
        """``provisional``: provisional source id -> when it was retired (or None)."""
        self.retired = {int(k): (_ns([v])[0] if v is not None and pd.notna(v) else None)
                        for k, v in (provisional or {}).items()}
        self.events: dict[int, _Event] = {}
        self.by_source: dict[int, int] = {}
        self.recent = pd.DataFrame({"x": pd.Series(dtype=float), "y": pd.Series(dtype=float),
                                    "t": pd.Series(dtype=np.int64),
                                    "event": pd.Series(dtype=np.int64)})
        # Every point of every live new-source event: its incident stays open
        # whatever the gaps, so a fire beside it links at any time.
        self.anchors = pd.DataFrame({"x": pd.Series(dtype=float), "y": pd.Series(dtype=float),
                                     "event": pd.Series(dtype=np.int64)})
        self.assigned: list[pd.DataFrame] = []
        self._next = 1

    def _new(self, kind: str, source_id: int | None, t: int) -> _Event:
        ev = _Event(id=self._next, kind=kind, source_id=source_id, first=t, last=t)
        self.events[ev.id] = ev
        self._next += 1
        return ev

    def _live(self, ev: _Event, t: int) -> bool:
        retired = self.retired.get(ev.source_id)
        return retired is None or t < retired

    def _source_events(self, frame: pd.DataFrame) -> np.ndarray:
        agg = frame.groupby("source_id").agg(**_EXTENT)
        event_of = {}
        for sid, a in zip(agg.index.astype(int), agg.itertuples(), strict=True):
            ev = self.events.get(self.by_source.get(sid, -1))
            if sid in self.retired:
                if ev is None:
                    ev = self._new("new_source", sid, a.t0)
            elif ev is None or a.t0 - ev.last > REIGNITE.value:
                ev = self._new("anomaly", sid, a.t0)
            self.by_source[sid] = ev.id
            ev.extend(a.t0, a.t1, a.x0, a.x1, a.y0, a.y1)
            event_of[sid] = ev.id
        return frame["source_id"].astype(int).map(event_of).to_numpy()

    def _fire_events(self, frame: pd.DataFrame) -> np.ndarray:
        from scipy.spatial import cKDTree
        from sklearn.cluster import DBSCAN

        x, y = frame["x"].to_numpy(), frame["y"].to_numpy()
        labels = DBSCAN(eps=LINK_M, min_samples=1).fit_predict(np.column_stack([x, y]))
        # Split day clusters wider than the cap on a grid of the cap's size.
        ext = pd.DataFrame({"c": labels, "x": x, "y": y}).groupby("c").agg(
            x0=("x", "min"), x1=("x", "max"), y0=("y", "min"), y1=("y", "max"))
        wide = ext.index[np.hypot(ext.x1 - ext.x0, ext.y1 - ext.y0) / 1000 > MAX_EVENT_KM]
        if len(wide):
            tile = MAX_EVENT_KM * 1000 / np.sqrt(2)
            key = (np.floor(x / tile).astype(np.int64) * 100_000
                   + np.floor(y / tile).astype(np.int64))
            split = np.isin(labels, wide)
            labels = np.where(split, labels.max() + 1 + pd.factorize(key)[0], labels)

        match = np.full(len(frame), -1, dtype=np.int64)
        for pool in (self.anchors, self.recent):      # new-source incidents first
            todo = match < 0
            if not len(pool) or not todo.any():
                continue
            dist, idx = cKDTree(pool[["x", "y"]].to_numpy(dtype=float)).query(
                np.column_stack([x[todo], y[todo]]), distance_upper_bound=LINK_M)
            hit = np.isfinite(dist)
            match[np.flatnonzero(todo)[hit]] = pool["event"].to_numpy()[idx[hit]]

        work = frame.assign(c=labels, m=match)
        agg = work.groupby("c").agg(**_EXTENT)
        cand = work[work["m"] >= 0].groupby(["c", "m"]).size().rename("k").reset_index()
        cand = cand.sort_values(["c", "k"], ascending=[True, False])
        choices = cand.groupby("c")["m"].agg(list).to_dict()
        event_of = {}
        for c, a in zip(agg.index, agg.itertuples(), strict=True):
            ev = None
            for m in choices.get(c, ()):
                e = self.events[int(m)]
                open_ = (e.kind == "new_source" and self._live(e, a.t0)) or \
                    a.t0 - e.last <= REIGNITE.value
                if open_ and (e.kind != "fire"
                              or e.width_with(a.x0, a.x1, a.y0, a.y1) <= MAX_EVENT_KM):
                    ev = e
                    break
            if ev is None:
                ev = self._new("fire", None, a.t0)
            ev.extend(a.t0, a.t1, a.x0, a.x1, a.y0, a.y1)
            event_of[c] = ev.id
        return pd.Series(labels).map(event_of).to_numpy()

    def add(self, day: pd.DataFrame) -> None:
        """One UTC day of routed detections: ``detection_id``, ``x``, ``y``,
        ``acq_datetime``, ``road``, ``source_id`` (NaN for none), ``alert``,
        ``pred_class``, ``category``, ``frp``."""
        rows = day[day["road"] != ROAD_B]
        if rows.empty:
            return
        frame = pd.DataFrame({
            "detection_id": rows["detection_id"].to_numpy(), "x": rows["x"].to_numpy(),
            "y": rows["y"].to_numpy(), "t": _ns(rows["acq_datetime"]),
            "source_id": rows["source_id"].to_numpy(dtype=float),
            "frp": rows["frp"].to_numpy(dtype=float), "pred_class": rows["pred_class"].to_numpy(),
            "category": rows["category"].to_numpy(), "alert": rows["alert"].to_numpy()})
        start = int(frame["t"].min())
        self.recent = self.recent[self.recent["t"] >= start - REIGNITE.value]
        if len(self.anchors):            # a retired source no longer holds an incident
            alive = [e for e in self.anchors["event"].unique()
                     if self._live(self.events[int(e)], start)]
            self.anchors = self.anchors[self.anchors["event"].isin(alive)]
        ev = np.zeros(len(frame), dtype=np.int64)
        at_source = frame["source_id"].notna().to_numpy()
        if at_source.any():
            ev[at_source] = self._source_events(frame[at_source])
        if (~at_source).any():
            ev[~at_source] = self._fire_events(frame[~at_source])
        frame["event"] = ev
        self.assigned.append(frame)
        new = frame[["x", "y", "t", "event"]]
        kinds = np.array([self.events[int(e)].kind for e in np.unique(ev)])
        anchored = new[new["event"].isin(np.unique(ev)[kinds == "new_source"])]
        if len(anchored):
            self.anchors = pd.concat([self.anchors, anchored[["x", "y", "event"]]],
                                     ignore_index=True)
        self.recent = pd.concat([self.recent, new], ignore_index=True)

    def status(self, ev: _Event, as_of: int) -> str:
        gap = as_of - ev.last
        if ev.kind == "new_source" and self._live(ev, as_of):
            return "active" if gap <= DORMANT_AFTER.value else "dormant"
        return ("active" if gap <= DORMANT_AFTER.value
                else "dormant" if gap <= REIGNITE.value else "closed")

    def result(self, as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(events, assignment): one row per event with its footprint as WKT, and
        each detection's local event id."""
        import shapely

        if not self.assigned:
            return pd.DataFrame(), pd.DataFrame(columns=["detection_id", "event"])
        a = pd.concat(self.assigned, ignore_index=True)
        a["day"] = a["t"] // pd.Timedelta(days=1).value
        a["rank"] = a["alert"].map(ALERT_RANK).fillna(0)
        g = a.groupby("event")
        stats = g.agg(n_detections=("x", "size"), peak_frp=("frp", "max"), cx=("x", "mean"),
                      cy=("y", "mean"), days=("day", "nunique"), rank=("rank", "max"))
        mode = {}
        for col in ("pred_class", "category"):
            counts = a.dropna(subset=[col]).groupby(["event", col]).size()
            mode[col] = counts.groupby(level=0).idxmax().map(lambda k: k[1])
        pts = a.sort_values("event", kind="stable")
        order = stats.index.to_numpy()
        code = np.searchsorted(order, pts["event"].to_numpy())
        hulls = shapely.convex_hull(shapely.multipoints(
            pts[["x", "y"]].to_numpy(dtype=float), indices=code))
        shapes = shapely.transform(shapely.buffer(hulls, HALF_PIXEL_M, quad_segs=4), _to_lonlat)
        lat, lon = from_metres(stats["cx"].to_numpy(), stats["cy"].to_numpy())
        as_of_ns = int(_ns([as_of])[0])
        by_rank = {v: k for k, v in ALERT_RANK.items()}
        evs = [self.events[int(e)] for e in order]
        events = pd.DataFrame({
            "event": order, "kind": [e.kind for e in evs],
            "source_id": [e.source_id for e in evs],
            "first_seen": pd.to_datetime([e.first for e in evs], utc=True),
            "last_seen": pd.to_datetime([e.last for e in evs], utc=True),
            "status": [self.status(e, as_of_ns) for e in evs],
            "event_class": mode["pred_class"].reindex(order).to_numpy(),
            "category": mode["category"].reindex(order).to_numpy(),
            "alert": [by_rank.get(int(r)) for r in stats["rank"]],
            "peak_frp": stats["peak_frp"].round(2).to_numpy(),
            "n_detections": stats["n_detections"].to_numpy(),
            "days": stats["days"].to_numpy(), "lat": lat, "lon": lon,
            "footprint": shapely.to_wkt(shapes, rounding_precision=6)})
        events["reason"] = [_reason(r) for r in events.itertuples()]
        return events, a[["detection_id", "event"]]


def _ns(times) -> np.ndarray:
    """Nanoseconds since the epoch, whatever the datetime unit."""
    return pd.DatetimeIndex(times).as_unit("ns").asi8


def _to_lonlat(coords: np.ndarray) -> np.ndarray:
    lat, lon = from_metres(coords[:, 0], coords[:, 1])
    return np.column_stack([lon, lat])


def _reason(r) -> str:
    span = f"{r.n_detections} detections on {r.days} day{'s' if r.days != 1 else ''}"
    # A source Model 1 has not classified yet has no class: say so, not "nan".
    cls = (r.event_class if isinstance(r.event_class, str) else "unclassified").replace("_", " ")
    if r.kind == "anomaly":
        return (f"{r.alert or 'watch'} anomaly at registered {cls} source "
                f"{int(r.source_id)}: "
                f"{span}, peak {r.peak_frp:.0f} MW")
    if r.kind == "new_source":
        what = (f"new persistent {cls} site" if r.category == "industrial"
                else f"persistent {cls} fire in one place")
        return (f"{what} since {r.first_seen.date()}: {span}, peak {r.peak_frp:.0f} MW; "
                "alerting until an analyst reviews it")
    return f"{cls} fire: {span}, peak {r.peak_frp:.0f} MW"


def pre_promotion(det: pd.DataFrame, promotions: pd.DataFrame, cells: pd.DataFrame) -> pd.Series:
    """The provisional source each pre-promotion Road A detection belongs to.

    Promotion consumed the Road A history within 500 m of the new source's cells
    since its first fire; those detections are the same incident. ``promotions``:
    ``source_id``, ``first_seen`` (date), ``promoted_at``; ``cells``: ``source_id``,
    ``x``, ``y``. Returns source ids on ``det``'s index (NaN elsewhere).
    """
    from scipy.spatial import cKDTree

    out = pd.Series(np.nan, index=det.index)
    road_a = det[(det["road"] == ROAD_A) & det["source_id"].isna()]
    if road_a.empty or promotions.empty or cells.empty:
        return out
    tree = cKDTree(cells[["x", "y"]].to_numpy(dtype=float))
    dist, idx = tree.query(road_a[["x", "y"]].to_numpy(dtype=float),
                           distance_upper_bound=PRE_PROMOTION_M)
    hit = np.isfinite(dist)
    sid = np.full(len(road_a), np.nan)
    sid[hit] = cells["source_id"].to_numpy()[idx[hit]]
    cand = pd.Series(sid, index=road_a.index).dropna()
    meta = promotions.set_index("source_id")
    t = road_a.loc[cand.index, "acq_datetime"]
    first = pd.to_datetime(cand.map(meta["first_seen"]).astype(str), utc=True)
    until = pd.to_datetime(cand.map(meta["promoted_at"]), utc=True)
    keep = (t >= first) & (t < until)
    out.loc[cand.index[keep.to_numpy()]] = cand[keep.to_numpy()]
    return out


def assemble(det: pd.DataFrame, provisional: dict[int, pd.Timestamp | None],
             promotions: pd.DataFrame, cells: pd.DataFrame,
             as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Events for routed detections (with ``x``, ``y``), day by day."""
    from firewatch.registry.cells import utc_day

    det = det.copy()
    pre = pre_promotion(det, promotions, cells)
    det["source_id"] = det["source_id"].where(det["source_id"].notna(), pre)
    det["day"] = utc_day(det["acq_datetime"])
    asm = Assembler(provisional)
    for _, day in det.sort_values("acq_datetime", kind="stable").groupby("day", sort=True):
        asm.add(day)
    return asm.result(as_of)


# ------------------------------------------------------------------------ db

def read_run(run_id: int | None = None) -> tuple[pd.DataFrame, dict, pd.DataFrame,
                                                  pd.DataFrame, pd.Series]:
    """The routed detections of an inference run's window, and its provisional
    sources: (detections, provisional, promotions, cells, run row)."""
    from sqlalchemy import text

    from firewatch.db import engine
    from firewatch.registry.cells import add_metres

    with engine().connect() as conn:
        run = pd.read_sql_query(text(
            "SELECT run_id, window_start, window_end FROM inference_runs "
            + ("WHERE run_id = :r" if run_id else "ORDER BY run_id DESC LIMIT 1")),
            conn, params={"r": run_id}).iloc[0]
        det = pd.read_sql_query(text("""
            SELECT detection_id, acq_datetime, latitude, longitude, road, source_id, alert,
                   pred_class, category, frp
              FROM detections WHERE acq_datetime >= :s AND acq_datetime < :e
               AND road IS NOT NULL AND road <> 2"""), conn,
            params={"s": run["window_start"], "e": run["window_end"]})
        prov = pd.read_sql_query(text("""
            SELECT source_id, first_seen, promoted_at, retired_at FROM sources
             WHERE provisional"""), conn)
        cells = pd.read_sql_query(text("""
            SELECT c.source_id, c.x_m AS x, c.y_m AS y FROM source_cells c
              JOIN sources s USING (source_id) WHERE s.provisional"""), conn)
    det["acq_datetime"] = pd.to_datetime(det["acq_datetime"], utc=True)
    add_metres(det)
    provisional = {int(r.source_id): (pd.Timestamp(r.retired_at) if pd.notna(r.retired_at)
                                      else None) for r in prov.itertuples()}
    promotions = prov[prov["promoted_at"].notna()][["source_id", "first_seen", "promoted_at"]]
    return det, provisional, promotions, cells, run


def write(events: pd.DataFrame, assigned: pd.DataFrame, run) -> dict:
    """Replace the run window's events and point its detections at them."""
    from firewatch.db import get_conn

    s, e = run["window_start"], run["window_end"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE detections SET event_id = NULL WHERE event_id IS NOT NULL "
                    "AND acq_datetime >= %s AND acq_datetime < %s", (s, e))
        cur.execute("DELETE FROM events WHERE first_seen >= %s AND first_seen < %s", (s, e))
        cur.execute("SELECT nextval(pg_get_serial_sequence('events', 'event_id')) "
                    "FROM generate_series(1, %s)", (len(events),))
        ids = dict(zip(events["event"], [r[0] for r in cur.fetchall()], strict=True))
        frame = events.assign(event_id=events["event"].map(ids), run_id=int(run["run_id"]))
        frame["source_id"] = pd.array(frame["source_id"], dtype="Int64")
        cols = ["event_id", "source_id", "kind", "category", "event_class", "alert", "status",
                "first_seen", "last_seen", "peak_frp", "n_detections", "reason", "run_id",
                "lon", "lat", "footprint"]
        cur.execute("CREATE TEMP TABLE event_stage (event_id int, source_id int, kind text, "
                    "category text, event_class text, alert text, status text, "
                    "first_seen timestamptz, last_seen timestamptz, peak_frp real, "
                    "n_detections int, reason text, run_id int, lon float8, lat float8, "
                    "footprint text) ON COMMIT DROP")
        _copy(cur, "event_stage", frame[cols])
        cur.execute("""
            INSERT INTO events (event_id, source_id, kind, category, event_class, alert, status,
                                first_seen, last_seen, peak_frp, n_detections, reason, run_id,
                                centroid, footprint, updated_at)
            SELECT event_id, source_id, kind, category, event_class, alert, status, first_seen,
                   last_seen, peak_frp, n_detections, reason, run_id,
                   ST_SetSRID(ST_MakePoint(lon, lat), 4326),
                   ST_GeomFromText(footprint, 4326), now()
              FROM event_stage""")
        link = pd.DataFrame({"detection_id": assigned["detection_id"],
                             "event_id": assigned["event"].map(ids)})
        cur.execute("CREATE TEMP TABLE event_link (detection_id bigint, event_id int) "
                    "ON COMMIT DROP")
        _copy(cur, "event_link", link)
        cur.execute("CREATE INDEX ON event_link (detection_id); ANALYZE event_link")
        cur.execute("""
            UPDATE detections d SET event_id = l.event_id FROM event_link l
             WHERE d.detection_id = l.detection_id
               AND d.acq_datetime >= %s AND d.acq_datetime < %s""", (s, e))
        linked = cur.rowcount
    return {"events": int(len(events)), "detections_linked": int(linked)}


def _copy(cur, table: str, frame: pd.DataFrame) -> None:
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False, date_format="%Y-%m-%d %H:%M:%S%z")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT csv)", buf)


def summarise(events: pd.DataFrame) -> dict:
    if events.empty:
        return {"events": 0}
    return {
        "events": int(len(events)),
        "by_kind": {str(k): int(v) for k, v in events["kind"].value_counts().items()},
        "by_status": {str(k): int(v) for k, v in events["status"].value_counts().items()},
        "by_category": {str(k): int(v) for k, v in events["category"].value_counts().items()},
        "detections_per_event_median": float(events["n_detections"].median()),
        "largest_event_detections": int(events["n_detections"].max()),
        "multi_day_events": int((events["days"] > 1).sum()),
        "alerts": {str(k): int(v) for k, v in events["alert"].value_counts().items()},
    }


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
