"""ESA WorldCover 2021 (10 m) land cover, sampled at points.

The cropland and forest labels, and Road A's context, need land cover only where
fires are, not everywhere. WorldCover ships as cloud-optimised GeoTIFFs on a
public AWS bucket, so each point costs a ranged HTTP read of the tiles around it
instead of downloading ~10 GB of India. GDAL (bundled with rasterio) does the
range requests.

Cropland labels come from here only, never from season (CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from firewatch.config import settings

TILE_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
            "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
CLASSES = {10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
           50: "built-up", 60: "bare / sparse vegetation", 70: "snow and ice",
           80: "permanent water", 90: "herbaceous wetland", 95: "mangroves",
           100: "moss and lichen"}
GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif", "VSI_CACHE": "TRUE",
            "GDAL_HTTP_MAX_RETRY": "4", "GDAL_HTTP_RETRY_DELAY": "2"}


def tile_name(lat: float, lon: float) -> str:
    """WorldCover's 3-degree tile holding a point, named by its south-west corner."""
    la = int(math.floor(lat / 3) * 3)
    lo = int(math.floor(lon / 3) * 3)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def sample(lat, lon) -> np.ndarray:
    """WorldCover class per point (0 where there is no tile, e.g. open sea)."""
    import rasterio

    lat = np.atleast_1d(np.asarray(lat, dtype=float))
    lon = np.atleast_1d(np.asarray(lon, dtype=float))
    out = np.zeros(lat.size, dtype=np.int16)
    tiles = pd.Series([tile_name(a, o) for a, o in zip(lat, lon, strict=True)])
    with rasterio.Env(**GDAL_ENV):
        for tile, idx in tiles.groupby(tiles).groups.items():
            idx = np.asarray(idx)
            try:
                with rasterio.open("/vsicurl/" + TILE_URL.format(tile=tile)) as ds:
                    values = ds.sample(zip(lon[idx], lat[idx], strict=True))
                    out[idx] = [v[0] for v in values]
            except rasterio.errors.RasterioIOError:
                continue   # no tile here: sea, or outside WorldCover's extent
    return out


def sample_fixture(lat, lon) -> np.ndarray:
    """Mock mode: the fixture's land-cover polygons; 0 elsewhere."""
    from shapely import wkt
    from shapely.geometry import Point

    path = settings().mock_dir / "truth" / "landcover.csv"
    lat = np.atleast_1d(np.asarray(lat, dtype=float))
    lon = np.atleast_1d(np.asarray(lon, dtype=float))
    out = np.zeros(lat.size, dtype=np.int16)
    if not path.exists():
        return out
    for row in pd.read_csv(path).itertuples():
        shape = wkt.loads(row.wkt)
        inside = np.array([shape.contains(Point(o, a))
                           for a, o in zip(lat, lon, strict=True)])
        out[inside] = row.worldcover_class
    return out


def landcover(lat, lon, mock: bool | None = None) -> np.ndarray:
    use_mock = settings().mock_mode if mock is None else mock
    return sample_fixture(lat, lon) if use_mock else sample(lat, lon)
