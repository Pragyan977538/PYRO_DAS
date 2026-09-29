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
from pathlib import Path

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


# ------------------------------------------------------ India at detection scale

#: Overview factor for the India mosaic: 10 m x 32 = ~300 m, just under a VIIRS
#: pixel. Road A asks what land a *detection* sits on, and a 375 m pixel has no
#: 10 m answer; a remote read per detection would also take days for a year of
#: fires. WorldCover's own overviews make the mosaic a few minutes' download.
MOSAIC_FACTOR = 32
MOSAIC_DEG = 3.0 / (36000 // MOSAIC_FACTOR)


def mosaic_path() -> Path:
    return settings().raw_dir / "worldcover" / f"india_wc2021_x{MOSAIC_FACTOR}.tif"


def build_mosaic(path: Path | None = None) -> Path:
    """Mosaic WorldCover's overviews over the India bbox into one local GeoTIFF."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    path = path or mosaic_path()
    if path.exists():
        return path
    w, s, e, n = settings().india_bbox
    lat0, lon0 = math.floor(s / 3) * 3, math.floor(w / 3) * 3
    lat1, lon1 = math.ceil(n / 3) * 3, math.ceil(e / 3) * 3
    size = 36000 // MOSAIC_FACTOR
    rows, cols = int((lat1 - lat0) / 3) * size, int((lon1 - lon0) / 3) * size
    out = np.zeros((rows, cols), dtype=np.uint8)
    with rasterio.Env(**GDAL_ENV):
        for la in range(lat0, lat1, 3):
            for lo in range(lon0, lon1, 3):
                try:
                    with rasterio.open("/vsicurl/" + TILE_URL.format(
                            tile=tile_name(la + 1, lo + 1))) as ds:
                        block = ds.read(1, out_shape=(size, size),
                                        resampling=Resampling.nearest)
                except rasterio.errors.RasterioIOError:
                    continue   # open sea: no tile
                r = int((lat1 - (la + 3)) / 3) * size
                c = int((lo - lon0) / 3) * size
                out[r:r + size, c:c + size] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part.tif")
    with rasterio.open(tmp, "w", driver="GTiff", width=cols, height=rows, count=1,
                       dtype="uint8", crs="EPSG:4326", compress="deflate",
                       transform=from_origin(lon0, lat1, MOSAIC_DEG, MOSAIC_DEG)) as dst:
        dst.write(out, 1)
    tmp.replace(path)
    return path


class Mosaic:
    """The India mosaic in memory: WorldCover class at any point, 0 off the map."""

    def __init__(self, path: Path | None = None) -> None:
        import rasterio
        with rasterio.open(path or mosaic_path()) as ds:
            self.array = ds.read(1)
            self.west, self.north = ds.transform.c, ds.transform.f
            self.step = ds.transform.a

    def sample(self, lat, lon) -> np.ndarray:
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        r = np.floor((self.north - lat) / self.step).astype(np.int64)
        c = np.floor((lon - self.west) / self.step).astype(np.int64)
        ok = (r >= 0) & (r < self.array.shape[0]) & (c >= 0) & (c < self.array.shape[1])
        out = np.zeros(lat.shape, dtype=np.int16)
        out[ok] = self.array[r[ok], c[ok]]
        return out
