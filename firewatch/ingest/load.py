"""Bulk loading into PostGIS, and the ingest log that makes backfills resumable.

Detections arrive in millions, so they go through COPY into a temporary staging
table and then one INSERT ... ON CONFLICT DO NOTHING on the natural key. That is
an order of magnitude faster than row inserts, and re-loading the same file is a
no-op rather than a pile of duplicates.

The same INSERT enforces "SP beats NRT" on the way in: an NRT row is not inserted
for a sensor-day the archive already covers. Together with ``supersede_nrt`` (for
SP arriving after NRT) no pixel is ever counted twice, whatever the load order.
"""

from __future__ import annotations

import io
import logging

import pandas as pd

from firewatch.db import fetch_all, get_conn
from firewatch.ingest.normalize import COLUMNS

log = logging.getLogger(__name__)

_STAGE_DDL = """
CREATE TEMP TABLE stage_detections (
    acq_datetime TIMESTAMPTZ, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
    sensor TEXT, instrument TEXT, satellite TEXT, product TEXT, version TEXT,
    daynight CHAR(1), frp REAL, bt4 REAL, bt5 REAL, scan REAL, track REAL,
    confidence TEXT, firms_type SMALLINT
) ON COMMIT DROP
"""

# Days are UTC days: the database runs in UTC (003_ingest.sql), and the explicit
# AT TIME ZONE keeps this correct even on a server that doesn't.
_INSERT = """
INSERT INTO detections (acq_datetime, latitude, longitude, geom, sensor, instrument,
                        satellite, product, version, daynight, frp, bt4, bt5, scan,
                        track, confidence, firms_type)
SELECT s.acq_datetime, s.latitude, s.longitude,
       ST_SetSRID(ST_MakePoint(s.longitude, s.latitude), 4326),
       s.sensor, s.instrument, s.satellite, s.product, s.version, s.daynight, s.frp,
       s.bt4, s.bt5, s.scan, s.track, s.confidence, s.firms_type
FROM stage_detections s
WHERE s.product = 'SP' OR NOT EXISTS (
    SELECT 1 FROM detections d
    WHERE d.product = 'SP' AND d.sensor = s.sensor
      AND d.acq_datetime >= (date_trunc('day', s.acq_datetime AT TIME ZONE 'UTC')
                             AT TIME ZONE 'UTC')
      AND d.acq_datetime <  (date_trunc('day', s.acq_datetime AT TIME ZONE 'UTC')
                             AT TIME ZONE 'UTC') + interval '1 day')
ON CONFLICT (sensor, acq_datetime, latitude, longitude) DO NOTHING
"""


def _csv(frame: pd.DataFrame) -> io.StringIO:
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False, date_format="%Y-%m-%d %H:%M:%S%z")
    buf.seek(0)
    return buf


def load_detections(df: pd.DataFrame) -> int:
    """Insert normalised detections; returns how many rows were new.

    ``df`` must carry ``normalize.COLUMNS``. Rows whose natural key already exists,
    and NRT rows for sensor-days that SP covers, are skipped.
    """
    if df.empty:
        return 0
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"not normalised: missing {missing}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_STAGE_DDL)
        cur.copy_expert("COPY stage_detections FROM STDIN WITH (FORMAT csv)",
                        _csv(df[COLUMNS]))
        cur.execute(_INSERT)
        inserted = cur.rowcount
    log.info("detections: %d new of %d", inserted, len(df))
    return inserted


def logged(source: str, item: str) -> bool:
    """Has this unit of work already been loaded?"""
    return bool(fetch_all("SELECT 1 FROM ingest_log WHERE source = :s AND item = :i",
                          {"s": source, "i": item}))


def mark_loaded(source: str, item: str, rows: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingest_log (source, item, rows) VALUES (%s, %s, %s)
            ON CONFLICT (source, item) DO UPDATE
                SET rows = EXCLUDED.rows, loaded_at = now()""", (source, item, rows))


def copy_rows(table: str, columns: list[str], frame: pd.DataFrame,
              conflict: str = "DO NOTHING", geom_from: tuple[str, str] | None = None) -> int:
    """COPY a frame into ``table`` through a staging table.

    ``geom_from`` names the (longitude, latitude) columns to build a point geometry
    from. Returns rows inserted.
    """
    if frame.empty:
        return 0
    cols = ", ".join(columns)
    with get_conn() as conn, conn.cursor() as cur:
        # Just these columns, with the target's types and none of its constraints
        # (LIKE would copy NOT NULL on identity and geometry columns we don't send).
        cur.execute(f"CREATE TEMP TABLE stage_rows ON COMMIT DROP AS "
                    f"SELECT {cols} FROM {table} WITH NO DATA")
        cur.copy_expert(f"COPY stage_rows ({cols}) FROM STDIN WITH (FORMAT csv)",
                        _csv(frame[columns]))
        target = cols
        select = cols
        if geom_from:
            lon, lat = geom_from
            target = f"{cols}, geom"
            select = f"{cols}, ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)"
        cur.execute(f"INSERT INTO {table} ({target}) SELECT {select} FROM stage_rows "
                    f"ON CONFLICT {conflict}")
        return cur.rowcount
