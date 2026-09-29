"""Mapbox vector tiles straight from PostGIS (``ST_AsMVT``).

A year of India is over a million detections, far past what GeoJSON can carry to
a browser. Tiles carry only what is in view. Below zoom 7 a tile would still
hold hundreds of thousands of points, so detections are binned onto a 128 x 128
grid per tile: each bin carries its count, its commonest category and how many
alerts it holds. From zoom 7 up, every detection is its own feature.
"""

from __future__ import annotations

from sqlalchemy import text

from firewatch.db import engine

WEB_MERCATOR_WIDTH = 40_075_016.68557849
BIN_BELOW_ZOOM = 7
BINS_PER_TILE = 128
MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

_RAW = """
WITH b AS (SELECT ST_TileEnvelope(:z, :x, :y) AS env),
q AS (
    SELECT ST_AsMVTGeom(ST_Transform(d.geom, 3857), b.env, 4096, 64, true) AS geom,
           d.detection_id AS id, d.road, d.category, d.pred_class, d.alert, d.frp,
           d.event_id, d.source_id, extract(epoch FROM d.acq_datetime)::bigint AS t
      FROM detections d, b
     WHERE d.geom && ST_Transform(b.env, 4326)
       AND d.acq_datetime >= :s AND d.acq_datetime < :e AND d.road IS NOT NULL
)
SELECT ST_AsMVT(q.*, 'detections', 4096, 'geom') FROM q
"""

# Binned tiles cover a lot of ground, where the time window is far more selective
# than the area: filter by time first (MATERIALIZED stops the planner merging it
# back into an all-years spatial scan), then by the tile.
_BINNED = """
WITH b AS (SELECT ST_TileEnvelope(:z, :x, :y) AS env),
w AS MATERIALIZED (
    SELECT geom, category, alert, frp FROM detections
     WHERE acq_datetime >= :s AND acq_datetime < :e AND road IS NOT NULL
),
g AS (
    SELECT ST_SnapToGrid(ST_Transform(d.geom, 3857), :cell) AS pt, count(*) AS n,
           mode() WITHIN GROUP (ORDER BY d.category) AS category,
           count(*) FILTER (WHERE d.alert IS NOT NULL) AS alerts, max(d.frp) AS max_frp
      FROM w d, b
     WHERE d.geom && ST_Transform(b.env, 4326)
     GROUP BY 1
),
q AS (
    SELECT ST_AsMVTGeom(g.pt, b.env, 4096, 64, true) AS geom, g.n, g.category, g.alerts,
           g.max_frp
      FROM g, b
)
SELECT ST_AsMVT(q.*, 'detections', 4096, 'geom') FROM q
"""

_EVENTS = """
WITH b AS (SELECT ST_TileEnvelope(:z, :x, :y) AS env),
q AS (
    SELECT ST_AsMVTGeom(ST_Transform(e.centroid, 3857), b.env, 4096, 64, true) AS geom,
           e.event_id AS id, e.kind, e.category, e.event_class, e.alert, e.status,
           e.risk_score AS risk, e.n_detections AS n, e.peak_frp
      FROM events e, b
     WHERE e.centroid && ST_Transform(b.env, 4326)
       AND e.last_seen >= :s AND e.first_seen < :e
       AND coalesce(e.risk_score, 0) >= :min_risk
)
SELECT ST_AsMVT(q.*, 'events', 4096, 'geom') FROM q
"""


def detections(z: int, x: int, y: int, start, end) -> bytes:
    params = {"z": z, "x": x, "y": y, "s": start, "e": end}
    sql = _RAW
    if z < BIN_BELOW_ZOOM:
        sql = _BINNED
        params["cell"] = WEB_MERCATOR_WIDTH / (2 ** z) / BINS_PER_TILE
    with engine().connect() as conn:
        tile = conn.execute(text(sql), params).scalar()
    return bytes(tile or b"")


def events(z: int, x: int, y: int, start, end, min_risk: float = 0.0) -> bytes:
    with engine().connect() as conn:
        tile = conn.execute(text(_EVENTS), {"z": z, "x": x, "y": y, "s": start, "e": end,
                                            "min_risk": min_risk}).scalar()
    return bytes(tile or b"")
