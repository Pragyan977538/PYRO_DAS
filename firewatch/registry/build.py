"""Build the registry from ``detections`` and write it back.

The pure steps (gate, cluster, assign, fingerprint, baseline) take DataFrames, so
tests run them without a database; this module adds the reads and the one
transactional write.

Rebuilt weekly, so source ids must survive a rebuild: events, alerts and the map
all refer to them. Each new cluster takes the id of the old source it shares the
most cells with. A rebuild never clears ``provisional`` -- only promotion (Stage 5)
does, because a provisional source is an accident until proven otherwise -- and
it never deletes a provisional source it failed to match.
"""

from __future__ import annotations

import io
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from firewatch.db import fetch_all, get_conn
from firewatch.grid import CELL_M, cell_bounds, from_metres, grid_cell
from firewatch.registry.baseline import baselines as compute_baselines
from firewatch.registry.cells import (
    Gate,
    add_metres,
    gated_cells,
    recurrence,
    utc,
    utc_day,
)
from firewatch.registry.cluster import ASSIGN_M, assign, cluster_cells
from firewatch.registry.fingerprint import fingerprints

log = logging.getLogger(__name__)

#: Observability grids, by the ``source`` value their rows carry, and their size.
#: A source reads the first grid that has rows for its cell.
CLOUD_GRIDS = {"power_syn1deg": 1.0, "fixture": 0.25}

ClearReader = Callable[[pd.DataFrame], dict[tuple[int, str], pd.Series]]


@dataclass
class Registry:
    gate: Gate
    cells: pd.DataFrame            # gated cells; cluster -1 = noise or capped
    clusters: pd.DataFrame         # per cluster, capped ones included
    sources: pd.DataFrame          # per registered cluster
    fingerprints: pd.DataFrame
    baselines: dict[int, dict]
    viirs: pd.DataFrame            # VIIRS detections with x, y, cell, source
    modis: pd.DataFrame            # MODIS detections with x, y, source
    stats: dict = field(default_factory=dict)


# ------------------------------------------------------------------------ reads

_DET_COLUMNS = ("detection_id, extract(epoch FROM acq_datetime)::bigint AS t, latitude, "
                "longitude, daynight, frp, bt4, bt5, firms_type, vnf_temp_k, vnf_area_m2")
_DTYPES = {"detection_id": "int64", "t": "int64", "latitude": "float64",
           "longitude": "float64", "daynight": "category", "frp": "float32",
           "bt4": "float32", "bt5": "float32", "firms_type": "float32",
           "vnf_temp_k": "float32", "vnf_area_m2": "float32"}


def read_detections(instrument: str) -> pd.DataFrame:
    """Every detection of one instrument, a year at a time to keep peaks down."""
    span = fetch_all("SELECT min(acq_datetime) AS lo, max(acq_datetime) AS hi "
                     "FROM detections WHERE instrument = :i", {"i": instrument})[0]
    if span["lo"] is None:
        return pd.DataFrame(columns=[*_DTYPES, "acq_datetime", "instrument"])
    parts = []
    with get_conn() as conn, conn.cursor() as cur:
        for year in range(span["lo"].year, span["hi"].year + 1):
            sql = cur.mogrify(
                f"COPY (SELECT {_DET_COLUMNS} FROM detections WHERE instrument = %s "
                "AND acq_datetime >= make_timestamptz(%s, 1, 1, 0, 0, 0, 'UTC') "
                "AND acq_datetime < make_timestamptz(%s, 1, 1, 0, 0, 0, 'UTC')) "
                "TO STDOUT WITH (FORMAT csv, HEADER)", (instrument, year, year + 1))
            buf = io.BytesIO()
            cur.copy_expert(sql.decode(), buf)
            buf.seek(0)
            part = pd.read_csv(buf, dtype=_DTYPES)
            if len(part):
                parts.append(part)
    det = pd.concat(parts, ignore_index=True)
    det["acq_datetime"] = pd.to_datetime(det.pop("t"), unit="s", utc=True)
    det["instrument"] = instrument
    return det


def read_clear(sources: pd.DataFrame) -> dict[tuple[int, str], pd.Series]:
    """Clear-sky fraction by UTC day for each source, night and day.

    Night uses 'N' rows where the grid has them (the fixture), otherwise the daily
    'A' value (NASA POWER): a daily mean on a 1-degree grid, not the sky at the
    overpass.
    """
    from sqlalchemy import text

    from firewatch.db import engine

    def rows_for(grid: str, cells: np.ndarray) -> pd.DataFrame:
        with engine().connect() as conn:
            return pd.read_sql_query(
                text("SELECT cell_id, obs_date, daynight, cloud_frac FROM observability "
                     "WHERE source = :g AND cell_id = ANY(:c)"),
                conn, params={"g": grid, "c": [int(c) for c in np.unique(cells)]})

    out: dict[tuple[int, str], pd.Series] = {}
    todo = sources
    for grid, deg in CLOUD_GRIDS.items():
        # Own cells first; neighbours only for the few sources whose cell is empty.
        for ring in ((0,), (-1, 0, 1)):
            if todo.empty:
                break
            cells = np.concatenate([grid_cell(todo["lat"] + dy * deg,
                                              todo["lon"] + dx * deg, deg)
                                    for dy in ring for dx in ring])
            found = clear_from_rows(todo, rows_for(grid, cells), deg)
            out.update(found)
            todo = todo[~todo.index.isin([s for s, _ in found])]
    return out


def clear_from_rows(sources: pd.DataFrame, rows: pd.DataFrame,
                    deg: float) -> dict[tuple[int, str], pd.Series]:
    """Clear fraction by UTC day per (source, 'N' | 'D'), from observability rows
    (``cell_id``, ``obs_date``, ``daynight``, ``cloud_frac``) on a ``deg`` grid."""
    out: dict[tuple[int, str], pd.Series] = {}
    if rows.empty or sources.empty:
        return out
    rows = rows.assign(day=utc_day(pd.to_datetime(rows["obs_date"])),
                       clear=1.0 - rows["cloud_frac"].astype(float))
    by_cell = dict(tuple(rows.groupby("cell_id")))
    lat, lon = sources["lat"].to_numpy(), sources["lon"].to_numpy()
    # The source's own cell, else the nearest neighbour with data: a source centred
    # a metre across a grid line from its detections should not lose its sky.
    steps = sorted(((dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)),
                   key=lambda s: abs(s[0]) + abs(s[1]))
    for i, source in enumerate(sources.index):
        g = None
        for dy, dx in steps:
            g = by_cell.get(int(grid_cell(lat[i] + dy * deg, lon[i] + dx * deg, deg)))
            if g is not None:
                break
        if g is None:
            continue
        for dn in ("N", "D"):
            pick = g[g["daynight"] == dn]
            if pick.empty:
                pick = g[g["daynight"] == "A"]
            out[(int(source), dn)] = pick.groupby("day")["clear"].mean()
    return out


def read_gihs():
    """GIHS India objects in EPSG:7755 metres, or None if not loaded."""
    import geopandas as gpd
    from sqlalchemy import text

    from firewatch.db import engine
    with engine().connect() as conn:
        frame = gpd.read_postgis(text("SELECT gihs_id, confirmed, active_2021, geom "
                                      "FROM gihs_reference"), conn, geom_col="geom")
    return None if frame.empty else frame.to_crs(7755).rename_geometry("geometry")


# ------------------------------------------------------------------------ build

def cluster(viirs: pd.DataFrame, gate: Gate) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Gate and cluster VIIRS cells. ``viirs`` must already carry x, y, cell."""
    return cluster_cells(gated_cells(recurrence(viirs), gate))


def _footprint(cell_keys: np.ndarray) -> str:
    """Convex hull of a source's cell squares, as WKT in EPSG:4326."""
    import shapely

    x0, y0 = cell_bounds(cell_keys)
    corners = np.concatenate([np.column_stack([x0 + dx, y0 + dy])
                              for dx in (0, CELL_M) for dy in (0, CELL_M)])
    hull = shapely.convex_hull(shapely.multipoints(corners))
    xs, ys = np.asarray(hull.exterior.coords).T
    lat, lon = from_metres(xs, ys)
    return shapely.to_wkt(shapely.polygons(np.column_stack([lon, lat])), rounding_precision=7)


def _source_table(cells: pd.DataFrame, viirs: pd.DataFrame,
                  modis: pd.DataFrame) -> pd.DataFrame:
    reg = cells[cells["cluster"] >= 0]
    w = reg["n_det"].to_numpy(dtype=float)
    frame = pd.DataFrame({"cluster": reg["cluster"].to_numpy(), "wx": reg["x"] * w,
                          "wy": reg["y"] * w, "w": w})
    agg = frame.groupby("cluster").sum()
    x, y = agg["wx"] / agg["w"], agg["wy"] / agg["w"]
    lat, lon = from_metres(x.to_numpy(), y.to_numpy())
    src = pd.DataFrame({"x": x, "y": y, "lat": lat, "lon": lon}, index=agg.index)
    src["footprint"] = [_footprint(g.index.to_numpy())
                        for _, g in reg.groupby("cluster")]
    assigned = pd.concat([d.loc[d["source"] >= 0, ["source", "acq_datetime"]]
                          for d in (viirs, modis) if len(d)], ignore_index=True)
    days = utc(assigned["acq_datetime"]).normalize().date
    per = pd.DataFrame({"source": assigned["source"].to_numpy(), "day": days}).groupby(
        "source")["day"]
    src["n_detections"] = per.size()
    src["first_seen"] = per.min()
    src["last_seen"] = per.max()
    return src


def build(viirs: pd.DataFrame, modis: pd.DataFrame, gate: Gate,
          clear_reader: ClearReader = read_clear) -> Registry:
    """The whole registry, in memory. Detection frames gain x, y, cell, source."""
    add_metres(viirs)
    cells, clusters = cluster(viirs, gate)
    viirs["source"] = assign(viirs["x"].to_numpy(), viirs["y"].to_numpy(), cells,
                             ASSIGN_M["VIIRS"])
    if len(modis):
        add_metres(modis)
        modis["source"] = assign(modis["x"].to_numpy(), modis["y"].to_numpy(), cells,
                                 ASSIGN_M["MODIS"])
    else:
        modis = modis.assign(source=pd.Series(dtype=np.int64))
    sources = _source_table(cells, viirs, modis)
    fps = fingerprints(viirs, clusters, clear_reader(sources))
    both = pd.concat([d[["source", "instrument", "daynight", "acq_datetime", "frp"]]
                      for d in (viirs, modis) if len(d)], ignore_index=True)
    registry = Registry(gate=gate, cells=cells, clusters=clusters, sources=sources,
                        fingerprints=fps, baselines=compute_baselines(both),
                        viirs=viirs, modis=modis)
    registry.stats = {
        "viirs_detections": int(len(viirs)), "modis_detections": int(len(modis)),
        "gated_cells": int(len(cells)), "sources": int(len(sources)),
        "capped_clusters": int(clusters["capped"].sum()) if len(clusters) else 0,
        "widest_km": round(float(clusters.loc[~clusters["capped"], "width_km"].max()), 2)
        if len(sources) else None,
        "viirs_assigned": round(float((viirs["source"] >= 0).mean()), 4),
        "modis_assigned": round(float((modis["source"] >= 0).mean()), 4) if len(modis)
        else None,
    }
    return registry


# ------------------------------------------------------------------------ write

def match_ids(new_cells: pd.Series, old_cells: pd.Series) -> dict[int, int]:
    """Cluster label -> old source id, greedily by shared cells, one to one.

    ``new_cells``: cell key -> cluster label; ``old_cells``: (cell key) -> old id
    (a cell may appear under several old ids).
    """
    both = pd.merge(new_cells.rename("cluster").reset_index(),
                    old_cells.rename("old").reset_index(), on="cell")
    if both.empty:
        return {}
    overlap = both.groupby(["cluster", "old"]).size().sort_values(ascending=False)
    taken_new, taken_old, out = set(), set(), {}
    for (new, old), _ in overlap.items():
        if new not in taken_new and old not in taken_old:
            out[int(new)] = int(old)
            taken_new.add(new)
            taken_old.add(old)
    return out


def _json(values: dict) -> str:
    def clean(v):
        if isinstance(v, float | np.floating):
            return None if not math.isfinite(float(v)) else round(float(v), 6)
        if isinstance(v, np.integer):
            return int(v)
        return v
    return json.dumps({k: clean(v) for k, v in values.items()})


def write(reg: Registry) -> dict[int, int]:
    """Replace the registry in one transaction. Returns cluster label -> source id."""
    registered = reg.cells[reg.cells["cluster"] >= 0]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT c.cell_id, c.source_id, s.provisional FROM source_cells c "
                    "JOIN sources s USING (source_id)")
        old = pd.DataFrame(cur.fetchall(), columns=["cell", "old", "provisional"])
        cur.execute("SELECT source_id, provisional FROM sources")
        provisional = {sid for sid, prov in cur.fetchall() if prov}
        ids = match_ids(registered["cluster"].rename_axis("cell"),
                        old.set_index("cell")["old"])
        for label in reg.sources.index:
            if int(label) not in ids:
                cur.execute("SELECT nextval(pg_get_serial_sequence('sources', 'source_id'))")
                ids[int(label)] = cur.fetchone()[0]
        kept = set(ids.values())
        # Unmatched old sources go, unless provisional: those are Stage 5's to settle.
        cur.execute("DELETE FROM sources WHERE NOT provisional AND NOT (source_id = ANY(%s))",
                    (list(kept),))
        deleted = cur.rowcount
        cur.execute("DELETE FROM source_cells WHERE source_id = ANY(%s)", (list(kept),))

        for label, s in reg.sources.iterrows():
            fp = reg.fingerprints.loc[label].to_dict() if label in reg.fingerprints.index else {}
            cur.execute("""
                INSERT INTO sources (source_id, geom, footprint, n_detections, first_seen,
                                     last_seen, fingerprint, baselines, provisional,
                                     updated_at)
                VALUES (%(id)s, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                        ST_GeomFromText(%(fp)s, 4326), %(n)s, %(first)s, %(last)s,
                        %(fingerprint)s, %(baselines)s, %(prov)s, now())
                ON CONFLICT (source_id) DO UPDATE SET
                    geom = EXCLUDED.geom, footprint = EXCLUDED.footprint,
                    n_detections = EXCLUDED.n_detections, first_seen = EXCLUDED.first_seen,
                    last_seen = EXCLUDED.last_seen, fingerprint = EXCLUDED.fingerprint,
                    baselines = EXCLUDED.baselines, updated_at = now()""",
                {"id": ids[int(label)], "lon": float(s["lon"]), "lat": float(s["lat"]),
                 "fp": s["footprint"], "n": int(s["n_detections"]),
                 "first": s["first_seen"], "last": s["last_seen"],
                 "fingerprint": _json(fp),
                 "baselines": json.dumps(reg.baselines.get(int(label), {})),
                 "prov": ids[int(label)] in provisional})
        cur.execute("SELECT setval(pg_get_serial_sequence('sources', 'source_id'), "
                    "GREATEST((SELECT max(source_id) FROM sources), 1))")

        lat, lon = from_metres(registered["x"].to_numpy(), registered["y"].to_numpy())
        cells_out = pd.DataFrame({
            "source_id": registered["cluster"].map(ids).to_numpy(),
            "cell_id": registered.index.to_numpy(), "n_det": registered["n_det"].to_numpy(),
            "years_passed": registered["years_passed"].to_numpy(),
            "x_m": registered["x"].round(2).to_numpy(), "y_m": registered["y"].round(2).to_numpy(),
            "lon": np.round(lon, 7), "lat": np.round(lat, 7)})
        _copy(cur, "source_cells_stage", cells_out,
              "source_id int, cell_id bigint, n_det int, years_passed int, x_m float8, "
              "y_m float8, lon float8, lat float8")
        cur.execute("""
            INSERT INTO source_cells (source_id, cell_id, n_det, years_passed, x_m, y_m, geom)
            SELECT source_id, cell_id, n_det, years_passed, x_m, y_m,
                   ST_SetSRID(ST_MakePoint(lon, lat), 4326) FROM source_cells_stage""")

        assigned = pd.concat([
            pd.DataFrame({"detection_id": d.loc[d["source"] >= 0, "detection_id"].to_numpy(),
                          "source_id": d.loc[d["source"] >= 0, "source"].map(ids).to_numpy()})
            for d in (reg.viirs, reg.modis) if len(d) and "detection_id" in d],
            ignore_index=True)
        _copy(cur, "assign_stage", assigned, "detection_id bigint, source_id int")
        cur.execute("CREATE INDEX ON assign_stage (detection_id); ANALYZE assign_stage")
        cur.execute("""
            UPDATE detections d SET source_id = NULL
             WHERE d.source_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM assign_stage a WHERE a.detection_id = d.detection_id)""")
        cleared = cur.rowcount
        cur.execute("""
            UPDATE detections d SET source_id = a.source_id FROM assign_stage a
             WHERE d.detection_id = a.detection_id
               AND d.source_id IS DISTINCT FROM a.source_id""")
        updated = cur.rowcount

        cur.execute("INSERT INTO registry_runs (gate, stats) VALUES (%s, %s) RETURNING run_id",
                    (str(reg.gate), json.dumps({**reg.stats, "deleted_sources": deleted,
                                                "detections_cleared": cleared,
                                                "detections_updated": updated})))
        run_id = cur.fetchone()[0]
    reg.stats.update(run_id=run_id, deleted_sources=deleted, detections_cleared=cleared,
                     detections_updated=updated, matched_ids=len(set(ids.values()) &
                                                                 set(old["old"])))
    log.info("registry run %d: %d sources, %d deleted, %d detections re-pointed",
             run_id, len(reg.sources), deleted, cleared + updated)
    return ids


def _copy(cur, name: str, frame: pd.DataFrame, columns_sql: str) -> None:
    cur.execute(f"CREATE TEMP TABLE {name} ({columns_sql}) ON COMMIT DROP")
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False)
    buf.seek(0)
    cur.copy_expert(f"COPY {name} FROM STDIN WITH (FORMAT csv)", buf)
