"""PYRO_DAS API: what the map asks, answered from PostGIS.

    uvicorn firewatch.api.main:app        # or: make api

Every feature carries what an analyst needs to trust it: the class, the alert tier,
the provisional flag, and **the reason** -- Road A's rule path, or the baseline a
pass breached. Features come back as GeoJSON. Anything that can run to hundreds of
thousands of points comes as vector tiles (``/api/tiles``). The map itself
(``web/``) is served from ``/``, so the API and the app are one process and work
offline.

Legacy class names from the original six-class plan (``flare``, ``furnace``, ...)
are accepted and mapped onto the three approved Model 1 classes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from firewatch.api import tiles
from firewatch.db import engine

WEB = Path(__file__).resolve().parents[2] / "web"
CLASS_ALIASES = {"flare": "oil_gas", "refinery": "oil_gas", "oil": "oil_gas",
                 "furnace": "heavy_industry", "steel": "heavy_industry",
                 "power": "heavy_industry", "mine": "mining", "coal": "mining"}
MAX_FEATURES = 20_000

app = FastAPI(
    title="PYRO_DAS API",
    version="0.8.0",
    description="Industrial fires and persistent thermal sources over India (SIH 2026, "
                "PS 26162). Every feature carries its class, alert tier and the reason "
                "for the call.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])


@app.middleware("http")
async def revalidate_app_files(request, call_next):
    """The map's own files are revalidated on every load, so an updated app.js is
    never shadowed by a cached one mid-demo. API responses are left alone."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ helpers

def _rows(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    with engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params or {}).mappings()]


def _bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError as exc:
        raise HTTPException(422, "bbox must be west,south,east,north") from exc
    if not (w < e and s < n):
        raise HTTPException(422, "bbox must have west < east and south < north")
    return w, s, e, n


def _class(value: str | None) -> str | None:
    return CLASS_ALIASES.get(value.lower(), value.lower()) if value else None


def _jsonable(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float) and value != value:
        return None
    return value


def _feature(row: dict, geometry_key: str = "geometry") -> dict:
    geometry = row.pop(geometry_key)
    return {"type": "Feature", "geometry": json.loads(geometry) if geometry else None,
            "properties": {k: _jsonable(v) for k, v in row.items()}}


def _collection(rows: list[dict], **meta) -> dict:
    return {"type": "FeatureCollection", "features": [_feature(r) for r in rows],
            "meta": {"count": len(rows), **meta}}


def _window(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """The requested window, defaulting to the last 30 days of the latest run."""
    if start and end:
        return datetime.combine(start, datetime.min.time()), \
            datetime.combine(end, datetime.min.time()) + timedelta(days=1)
    run = _rows("SELECT window_end FROM inference_runs ORDER BY run_id DESC LIMIT 1")
    last = run[0]["window_end"] if run else datetime.utcnow()
    if end:
        last = datetime.combine(end, datetime.min.time()) + timedelta(days=1)
    first = datetime.combine(start, datetime.min.time()) if start else last - timedelta(days=30)
    return first, last


# ------------------------------------------------------------------ meta

@app.get("/api/health", tags=["meta"])
def health() -> dict:
    try:
        _rows("SELECT 1")
        return {"status": "ok", "database": True}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"status": "degraded", "database": False, "error": str(exc)[:200]}


@app.get("/api/meta", tags=["meta"])
def meta() -> dict:
    """What the map needs to set itself up: the replayed window and the vocabularies."""
    run = _rows("SELECT run_id, window_start, window_end, stats FROM inference_runs "
                "ORDER BY run_id DESC LIMIT 1")
    return {
        "window": {k: _jsonable(v) for k, v in run[0].items() if k != "stats"} if run else None,
        "run_stats": run[0]["stats"] if run else None,
        "source_classes": ["oil_gas", "heavy_industry", "mining"],
        "categories": ["industrial", "forest", "agricultural", "other_natural", "unclassified"],
        "roads": {"1": "A: no known source", "2": "B: normal at a known source",
                  "3": "C: alert"},
        "alerts": ["confirmed", "provisional", "new_source"],
        "class_aliases": CLASS_ALIASES,
        "sources": _rows("SELECT count(*) FILTER (WHERE NOT provisional) AS registry, "
                         "count(*) FILTER (WHERE provisional) AS provisional FROM sources")[0],
    }


# --------------------------------------------------------------- sources

@app.get("/api/sources", tags=["sources"])
def sources(bbox: str | None = None, cls: str | None = Query(None, alias="class"),
            provisional: bool | None = None, geometry: str = "point",
            limit: int = Query(5000, le=MAX_FEATURES)) -> dict:
    """Registry and provisional sources as GeoJSON. ``geometry=footprint`` returns
    the outlines instead of the centres."""
    box = _bbox(bbox)
    where, params = ["TRUE"], {"limit": limit}
    if box:
        where.append("s.geom && ST_MakeEnvelope(:w, :s, :e, :n, 4326)")
        params.update(dict(zip("wsen", box, strict=True)))
    if cls:
        where.append("s.cls = :cls")
        params["cls"] = _class(cls)
    if provisional is not None:
        where.append("s.provisional = :prov")
        params["prov"] = provisional
    geom = "coalesce(s.footprint, s.geom)" if geometry == "footprint" else "s.geom"
    rows = _rows(f"""
        SELECT ST_AsGeoJSON({geom}, 6) AS geometry, s.source_id, s.cls,
               round(s.cls_conf::numeric, 3)::float AS cls_conf, s.provisional, s.origin,
               s.n_detections, s.first_seen, s.last_seen, s.promoted_at, s.retired_at,
               l.label, l.label_group, l.evidence_name AS nearest_named_facility,
               round((s.fingerprint->>'persistence_night')::numeric, 3)::float AS persistence,
               round((s.fingerprint->>'frp_med')::numeric, 1)::float AS frp_median,
               round((s.fingerprint->>'width_km')::numeric, 2)::float AS width_km
          FROM sources s LEFT JOIN source_labels l USING (source_id)
         WHERE {' AND '.join(where)}
         ORDER BY s.n_detections DESC LIMIT :limit""", params)
    return _collection(rows, class_filter=_class(cls))


@app.get("/api/sources/{source_id}", tags=["sources"])
def source(source_id: int) -> dict:
    rows = _rows("""
        SELECT ST_AsGeoJSON(s.geom, 6) AS geometry, ST_AsGeoJSON(s.footprint, 6) AS footprint,
               s.source_id, s.cls, s.cls_conf, s.provisional, s.origin, s.n_detections,
               s.first_seen, s.last_seen, s.promoted_at, s.retired_at, s.confirmed_at,
               s.fingerprint, s.baselines, s.updated_at, l.label, l.label_group,
               l.evidence, l.evidence_name, l.distance_m, l.groups_near
          FROM sources s LEFT JOIN source_labels l USING (source_id)
         WHERE s.source_id = :id""", {"id": source_id})
    if not rows:
        raise HTTPException(404, f"no source {source_id}")
    row = rows[0]
    footprint = row.pop("footprint")
    feat = _feature(row)
    feat["properties"]["footprint"] = json.loads(footprint) if footprint else None
    feat["properties"]["events"] = [
        {k: _jsonable(v) for k, v in e.items()} for e in _rows("""
            SELECT event_id, kind, alert, status, first_seen, last_seen, n_detections,
                   round(risk_score::numeric, 1)::float AS risk, reason
              FROM events WHERE source_id = :id ORDER BY first_seen DESC LIMIT 50""",
                                                                {"id": source_id})]
    return feat


@app.get("/api/sources/{source_id}/timeseries", tags=["sources"])
def timeseries(source_id: int, start: date | None = None, end: date | None = None) -> dict:
    """The source's FRP history for the chart: daily maxima over its whole record,
    and each scored pass with the baseline it was judged against."""
    if not _rows("SELECT 1 FROM sources WHERE source_id = :id", {"id": source_id}):
        raise HTTPException(404, f"no source {source_id}")
    params = {"id": source_id, "s": start or date(2000, 1, 1),
              "e": (end or date(2100, 1, 1)) + timedelta(days=1)}
    daily = _rows("""
        SELECT acq_datetime::date AS day, max(frp) AS max_frp, count(*) AS n,
               count(*) FILTER (WHERE daynight = 'N') AS night,
               count(*) FILTER (WHERE alert IS NOT NULL) AS alerts
          FROM detections WHERE source_id = :id
           AND acq_datetime >= :s AND acq_datetime < :e
         GROUP BY 1 ORDER BY 1""", params)
    passes = _rows("""
        SELECT pass_time, sensor, instrument, daynight, frp, med, p99, round(z::numeric, 2)::float
               AS z, baseline_key, breach, alert
          FROM source_passes WHERE source_id = :id
           AND pass_time >= :s AND pass_time < :e ORDER BY pass_time""", params)
    base = _rows("SELECT baselines FROM sources WHERE source_id = :id", {"id": source_id})
    return {"source_id": source_id,
            "daily": [{k: _jsonable(v) for k, v in r.items()} for r in daily],
            "passes": [{k: _jsonable(v) for k, v in r.items()} for r in passes],
            "baselines": base[0]["baselines"] if base else None}


# ------------------------------------------------------------- detections

@app.get("/api/detections", tags=["detections"])
def detections(bbox: str | None = None, start: date | None = None, end: date | None = None,
               cls: str | None = Query(None, alias="class"), category: str | None = None,
               road: int | None = Query(None, ge=1, le=3), alert: str | None = None,
               limit: int = Query(5000, le=MAX_FEATURES)) -> dict:
    """Routed detections as GeoJSON, newest first. Windows default to the last 30
    days of the latest run; for more, use the tiles."""
    first, last = _window(start, end)
    where = ["d.acq_datetime >= :s", "d.acq_datetime < :e", "d.road IS NOT NULL"]
    params: dict = {"s": first, "e": last, "limit": limit}
    box = _bbox(bbox)
    if box:
        where.append("d.geom && ST_MakeEnvelope(:w, :so, :ea, :n, 4326)")
        params.update({"w": box[0], "so": box[1], "ea": box[2], "n": box[3]})
    for col, value in (("pred_class", _class(cls)), ("category", category), ("road", road),
                       ("alert", alert)):
        if value is not None:
            where.append(f"d.{col} = :{col}")
            params[col] = value
    rows = _rows(f"""
        SELECT ST_AsGeoJSON(d.geom, 5) AS geometry, d.detection_id, d.acq_datetime,
               d.instrument, d.sensor, d.daynight, d.frp, d.road, d.pred_class,
               round(d.pred_conf::numeric, 3)::float AS pred_conf, d.category, d.alert,
               d.reason, d.source_id, d.event_id
          FROM detections d WHERE {' AND '.join(where)}
         ORDER BY d.acq_datetime DESC LIMIT :limit""", params)
    return _collection(rows, window=[first.isoformat(), last.isoformat()],
                       truncated=len(rows) == limit)


@app.get("/api/detections/{detection_id}", tags=["detections"])
def detection(detection_id: int) -> dict:
    """One detection with everything the detail panel shows: class, confidence,
    temperatures, FRP against its source's baseline, the nearest named facility,
    the alert tier, the event, and the reason."""
    rows = _rows("""
        SELECT ST_AsGeoJSON(d.geom, 6) AS geometry, d.detection_id, d.acq_datetime,
               d.latitude, d.longitude, d.sensor, d.instrument, d.product, d.daynight,
               d.frp, d.bt4, d.bt5, d.vnf_temp_k, d.confidence, d.road, d.pred_class,
               d.pred_conf, d.category, d.alert, d.reason, d.source_id, d.event_id
          FROM detections d WHERE d.detection_id = :id""", {"id": detection_id})
    if not rows:
        raise HTTPException(404, f"no detection {detection_id}")
    feat = _feature(rows[0])
    p = feat["properties"]
    p["temperature"] = ({"vnf_k": p["vnf_temp_k"]} if p["vnf_temp_k"] else
                        {"vnf_k": None, "note": "no VIIRS Nightfire temperature (optional, "
                                                "licence pending)",
                         "bt4_k": p["bt4"], "bt5_k": p["bt5"]})
    facility = _rows("""
        SELECT o.name, o.label_group,
               round(ST_Distance(o.geom::geography, d.geom::geography))::int AS distance_m
          FROM detections d, osm_industrial o
         WHERE d.detection_id = :id AND o.name IS NOT NULL
           AND o.geom && ST_Expand(d.geom, 0.05)
         ORDER BY o.geom <-> d.geom LIMIT 1""", {"id": detection_id})
    p["nearest_named_facility"] = facility[0] if facility else None
    p["baseline"] = None
    if p["source_id"]:
        cmp = _rows("""
            SELECT sp.frp AS pass_max_frp, sp.med, sp.p99, round(sp.z::numeric, 2)::float AS z,
                   sp.baseline_key, sp.breach, sp.extreme, sp.alert
              FROM source_passes sp JOIN detections d ON d.source_id = sp.source_id
               AND d.sensor = sp.sensor
               AND d.acq_datetime BETWEEN sp.pass_time AND sp.pass_time + interval '20 minutes'
             WHERE d.detection_id = :id LIMIT 1""", {"id": detection_id})
        p["baseline"] = {k: _jsonable(v) for k, v in cmp[0].items()} if cmp else None
        src = _rows("SELECT cls, cls_conf, provisional, origin FROM sources "
                    "WHERE source_id = :id", {"id": p["source_id"]})
        p["source"] = {k: _jsonable(v) for k, v in src[0].items()} if src else None
    if p["event_id"]:
        ev = _rows("""SELECT event_id, kind, status, alert, reason,
                             round(risk_score::numeric, 1)::float AS risk
                        FROM events WHERE event_id = :id""", {"id": p["event_id"]})
        p["event"] = {k: _jsonable(v) for k, v in ev[0].items()} if ev else None
    return feat


# ----------------------------------------------------------------- events

@app.get("/api/events", tags=["events"])
def events(bbox: str | None = None, status: str | None = None, kind: str | None = None,
           category: str | None = None, min_risk: float = 0.0, start: date | None = None,
           end: date | None = None, geometry: str = "point",
           limit: int = Query(2000, le=MAX_FEATURES)) -> dict:
    """Incidents, highest risk first."""
    where, params = ["coalesce(e.risk_score, 0) >= :min_risk"], {"min_risk": min_risk,
                                                                   "limit": limit}
    box = _bbox(bbox)
    if box:
        where.append("e.centroid && ST_MakeEnvelope(:w, :so, :ea, :n, 4326)")
        params.update({"w": box[0], "so": box[1], "ea": box[2], "n": box[3]})
    if start or end:
        first, last = _window(start, end)
        where.append("e.last_seen >= :s AND e.first_seen < :e")
        params.update({"s": first, "e": last})
    for col, value in (("status", status), ("kind", kind), ("category", category)):
        if value:
            where.append(f"e.{col} = :{col}")
            params[col] = value
    geom = "e.footprint" if geometry == "footprint" else "e.centroid"
    rows = _rows(f"""
        SELECT ST_AsGeoJSON({geom}, 5) AS geometry, e.event_id, e.kind, e.category,
               e.event_class, e.alert, e.status, e.first_seen, e.last_seen, e.n_detections,
               e.peak_frp, round(e.risk_score::numeric, 1)::float AS risk, e.reason,
               e.source_id
          FROM events e WHERE {' AND '.join(where)}
         ORDER BY e.risk_score DESC NULLS LAST LIMIT :limit""", params)
    return _collection(rows)


@app.get("/api/events/{event_id}", tags=["events"])
def event(event_id: int) -> dict:
    """One incident with its risk decomposition and its detections' timeline."""
    rows = _rows("""
        SELECT ST_AsGeoJSON(e.centroid, 6) AS geometry, ST_AsGeoJSON(e.footprint, 6)
               AS footprint, e.event_id, e.kind, e.category, e.event_class, e.alert,
               e.status, e.first_seen, e.last_seen, e.n_detections, e.peak_frp,
               e.risk_score, e.risk_breakdown, e.reason, e.source_id, e.run_id
          FROM events e WHERE e.event_id = :id""", {"id": event_id})
    if not rows:
        raise HTTPException(404, f"no event {event_id}")
    row = rows[0]
    footprint = row.pop("footprint")
    feat = _feature(row)
    p = feat["properties"]
    p["footprint"] = json.loads(footprint) if footprint else None
    p["timeline"] = [{k: _jsonable(v) for k, v in r.items()} for r in _rows("""
        SELECT acq_datetime::date AS day, count(*) AS n, max(frp) AS max_frp,
               count(*) FILTER (WHERE alert IS NOT NULL) AS alerts
          FROM detections WHERE event_id = :id GROUP BY 1 ORDER BY 1""", {"id": event_id})]
    return {**feat, "risk_breakdown": p.get("risk_breakdown")}


# ----------------------------------------------------------- observability

@app.get("/api/observability", tags=["observability"])
def observability(day: Annotated[date, Query(alias="date")],
                  bbox: str | None = None) -> dict:
    """Where the satellites could see on a day: clear-sky fraction per cloud cell
    (NASA POWER, 1 degree daily) -- the "where were we blind" layer."""
    box = _bbox(bbox) or (68.0, 6.0, 98.0, 37.0)
    rows = _rows("""
        SELECT cell_id, source, daynight, cloud_frac FROM observability
         WHERE obs_date = :d AND source = 'power_syn1deg'""", {"d": day})
    feats = []
    for r in rows:
        row, col = divmod(int(r["cell_id"]), 360)
        north, west = 90.0 - row, col - 180.0
        if not (west + 1 > box[0] and west < box[2] and north > box[1] and north - 1 < box[3]):
            continue
        ring = [[west, north], [west + 1, north], [west + 1, north - 1], [west, north - 1],
                [west, north]]
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"cell_id": r["cell_id"],
                                     "clear": round(1 - float(r["cloud_frac"]), 3),
                                     "cloud_frac": round(float(r["cloud_frac"]), 3)}})
    return {"type": "FeatureCollection", "features": feats,
            "meta": {"date": day.isoformat(), "count": len(feats),
                     "source": "NASA POWER CLOUD_AMT, CERES SYN1deg, daily mean"}}


@app.get("/api/summary", tags=["meta"])
def summary(start: date | None = None, end: date | None = None) -> dict:
    """Counts for a time window: detections by category and road, alerts, events
    by kind, and the ten highest-risk events -- the map's legend and watch list."""
    first, last = _window(start, end)
    params = {"s": first, "e": last}
    by_cat = _rows("""
        SELECT category, count(*) AS n FROM detections
         WHERE acq_datetime >= :s AND acq_datetime < :e AND road IS NOT NULL
         GROUP BY 1""", params)
    by_road = _rows("""
        SELECT road, count(*) AS n, count(*) FILTER (WHERE alert = 'confirmed') AS confirmed,
               count(*) FILTER (WHERE alert = 'provisional') AS provisional,
               count(*) FILTER (WHERE alert = 'new_source') AS new_source
          FROM detections WHERE acq_datetime >= :s AND acq_datetime < :e
           AND road IS NOT NULL GROUP BY 1""", params)
    top = _rows("""
        SELECT event_id, kind, category, event_class, alert, status,
               round(risk_score::numeric, 1)::float AS risk, reason,
               ST_X(centroid) AS lon, ST_Y(centroid) AS lat, first_seen, last_seen
          FROM events WHERE last_seen >= :s AND first_seen < :e
         ORDER BY risk_score DESC NULLS LAST LIMIT 10""", params)
    kinds = _rows("""
        SELECT kind, count(*) AS n FROM events
         WHERE last_seen >= :s AND first_seen < :e GROUP BY 1""", params)
    return {"window": [first.isoformat(), last.isoformat()],
            "categories": {r["category"]: r["n"] for r in by_cat},
            "roads": {str(r["road"]): {k: v for k, v in r.items() if k != "road"}
                      for r in by_road},
            "events": {r["kind"]: r["n"] for r in kinds},
            "top_events": [{k: _jsonable(v) for k, v in r.items()} for r in top]}


# ---------------------------------------------------------------- basemap

@app.get("/basemap/{name}.geojson", include_in_schema=False)
def basemap(name: str) -> FileResponse:
    """The offline basemap layers (``make basemap``), served locally."""
    from firewatch.ingest.basemap import LAYERS, basemap_dir

    path = basemap_dir() / f"{name}.geojson"
    if name not in LAYERS or not path.exists():
        raise HTTPException(404, f"basemap layer {name!r} not built: run `make basemap`")
    return FileResponse(path, media_type="application/geo+json")


# ------------------------------------------------------------------ tiles

@app.get("/api/tiles/detections/{z}/{x}/{y}.mvt", tags=["tiles"])
def detection_tile(z: int, x: int, y: int, start: date | None = None,
                   end: date | None = None) -> Response:
    """Detections as a vector tile; binned below zoom 7."""
    first, last = _window(start, end)
    return Response(tiles.detections(z, x, y, first, last), media_type=tiles.MVT_MEDIA_TYPE)


@app.get("/api/tiles/events/{z}/{x}/{y}.mvt", tags=["tiles"])
def event_tile(z: int, x: int, y: int, start: date | None = None, end: date | None = None,
               min_risk: float = 0.0) -> Response:
    first, last = _window(start, end)
    return Response(tiles.events(z, x, y, first, last, min_risk),
                    media_type=tiles.MVT_MEDIA_TYPE)


# ---------------------------------------------------------------- the map

if WEB.exists():
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=WEB), name="web")
