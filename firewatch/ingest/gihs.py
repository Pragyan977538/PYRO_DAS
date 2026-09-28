"""GIHS: the Global Industrial Heat Sources dataset (Ma et al. 2024), India only.

25,544 industrial heat sources worldwide for 2012-2021, each checked against
high-resolution imagery. CC BY 4.0, from Zenodo record 10570342.

EVALUATION ONLY. It is how the registry's precision and recall are measured
(Stage 3), so it must never become a label or a feature -- the moment it did, the
on-GIHS share would stop being an independent number.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import requests
from shapely.geometry import MultiPolygon

from firewatch.config import settings
from firewatch.db import get_conn
from firewatch.ingest.load import logged, mark_loaded

log = logging.getLogger(__name__)

URL = ("https://zenodo.org/api/records/10570342/files/"
       "Global%20Remote%20Industrial%20Heat%20Sources%20DatasetV3.0_20240126.rar/content")
ARCHIVE = "GIHS_V3.0_20240126.rar"
SOURCE = "gihs"


def shapefile(raw_dir: Path | None = None) -> Path | None:
    """Download and unpack GIHS if needed; the .shp path, or None if unavailable.

    The archive is a .rar. Windows' own tar (bsdtar) reads it; so does bsdtar or
    unrar elsewhere. GNU tar cannot.
    """
    folder = (raw_dir or settings().raw_dir) / "gihs"
    shp = next((folder / "extracted").rglob("*.shp"), None) if folder.exists() else None
    if shp:
        return shp
    folder.mkdir(parents=True, exist_ok=True)
    rar = folder / ARCHIVE
    if not rar.exists():
        r = requests.get(URL, timeout=300)
        r.raise_for_status()
        rar.write_bytes(r.content)
    dest = folder / "extracted"
    dest.mkdir(exist_ok=True)
    for tool in (r"C:\Windows\System32\tar.exe", "bsdtar", "unrar"):
        args = [tool, "x", "-o+", str(rar), str(dest)] if tool == "unrar" else \
            [tool, "-xf", str(rar), "-C", str(dest)]
        try:
            subprocess.run(args, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        shp = next(dest.rglob("*.shp"), None)
        if shp:
            return shp
    log.warning("GIHS: could not unpack %s (need Windows tar, bsdtar or unrar)", rar)
    return None


def load_gihs(raw_dir: Path | None = None) -> int:
    """Load GIHS's India objects into gihs_reference. Returns rows loaded."""
    import geopandas as gpd

    shp = shapefile(raw_dir)
    if shp is None or logged(SOURCE, shp.name):
        return 0
    gdf = gpd.read_file(shp)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    india = gdf[gdf["Nation"].astype(str).str.contains("India", case=False)].copy()
    india["geom"] = india.geometry.to_crs(4326).apply(
        lambda g: g if g.geom_type == "MultiPolygon" else MultiPolygon([g]))
    rows = [(int(idx), int(r["Type"]) == 0, int(r.get("date2021_p", 0) or 0) > 0,
             int(r["Points_num"]), str(r["Min_date"]), str(r["Max_date"]), r["geom"].wkt)
            for idx, r in india.iterrows()]
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO gihs_reference (gihs_id, confirmed, active_2021, points_num,
                                        min_date, max_date, geom)
            VALUES (%s, %s, %s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4326)))
            ON CONFLICT (gihs_id) DO NOTHING""", rows)
    mark_loaded(SOURCE, shp.name, len(rows))
    return len(rows)
