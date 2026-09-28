"""VIIRS Nightfire temperatures: optional enrichment.

VNF is licence-gated and night-only, so the pipeline runs without it. When it is
available (the academic licence, or the fixture in mock mode) its rows are staged
in ``vnf_raw`` and joined onto night VIIRS detections: nearest VNF detection
within 750 m (VIIRS geolocation error at 375 m pixels) and 30 minutes (scan-time
differences between the two products).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from firewatch.config import settings
from firewatch.db import get_conn
from firewatch.ingest.load import copy_rows, logged, mark_loaded

log = logging.getLogger(__name__)
SOURCE = "vnf"

# Names have shifted across VNF versions; map every spelling seen to one.
RENAME = {"date_mscan": "scan_time", "lat_gmtco": "latitude", "lon_gmtco": "longitude",
          "temp_bb": "temp_k", "area_bb": "area_m2", "rh": "rh_mw", "sat": "sat",
          "satellite": "sat"}
FILL = 999_999   # VNF's missing-value marker for failed fits


def read_vnf(path: Path) -> pd.DataFrame:
    """A VNF CSV in ``vnf_raw``'s columns, with failed fits as NULL, not 999999."""
    raw = pd.read_csv(path)
    df = raw.rename(columns={c: RENAME[c.lower()] for c in raw.columns
                             if c.lower() in RENAME})
    need = {"scan_time", "latitude", "longitude", "temp_k"}
    if need - set(df.columns):
        raise ValueError(f"{path.name}: missing VNF columns {sorted(need - set(df.columns))}")
    df["scan_time"] = pd.to_datetime(df["scan_time"], format="mixed", utc=True)
    for col in ("temp_k", "area_m2", "rh_mw"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
        df.loc[df[col] >= FILL, col] = np.nan
    if "sat" not in df.columns:
        df["sat"] = None
    return df[["scan_time", "latitude", "longitude", "temp_k", "area_m2", "rh_mw", "sat"]]


def load_file(path: Path, force: bool = False) -> int:
    if not force and logged(SOURCE, path.name):
        return 0
    df = read_vnf(path)
    n = copy_rows("vnf_raw", ["scan_time", "latitude", "longitude", "temp_k", "area_m2",
                              "rh_mw", "sat"], df,
                  conflict="(scan_time, latitude, longitude) DO NOTHING",
                  geom_from=("longitude", "latitude"))
    mark_loaded(SOURCE, path.name, n)
    return n


JOIN_SQL = """
UPDATE detections d
   SET vnf_temp_k = m.temp_k, vnf_area_m2 = m.area_m2, vnf_rh_mw = m.rh_mw
  FROM (
    SELECT d2.detection_id, d2.acq_datetime, v.temp_k, v.area_m2, v.rh_mw
      FROM detections d2
      CROSS JOIN LATERAL (
        SELECT temp_k, area_m2, rh_mw
          FROM vnf_raw v
         WHERE v.scan_time BETWEEN d2.acq_datetime - interval '30 minutes'
                               AND d2.acq_datetime + interval '30 minutes'
           AND v.geom && ST_Expand(d2.geom, 0.01)
           AND ST_DWithin(v.geom::geography, d2.geom::geography, 750)
           AND v.temp_k IS NOT NULL
         ORDER BY v.geom <-> d2.geom
         LIMIT 1) v
     WHERE d2.daynight = 'N' AND d2.instrument = 'VIIRS' AND d2.vnf_temp_k IS NULL
  ) m
 WHERE d.detection_id = m.detection_id AND d.acq_datetime = m.acq_datetime
"""


def join() -> int:
    """Copy the nearest VNF fit onto each unmatched night VIIRS detection."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(JOIN_SQL)
        return cur.rowcount


def load_vnf(mock: bool | None = None) -> dict[str, int]:
    """Stage every available VNF file, then join. Skips cleanly without VNF."""
    cfg = settings()
    use_mock = cfg.mock_mode if mock is None else mock
    directory = (cfg.mock_dir if use_mock else cfg.raw_dir) / "vnf"
    files = sorted(directory.glob("*.csv")) if directory.exists() else []
    if not files:
        if not use_mock and not cfg.vnf_enabled:
            log.info("VNF skipped: no licence credentials and no VNF files")
        return {}
    staged = {p.name: load_file(p) for p in files}
    staged["joined"] = join()
    return staged
