"""Population near a fire: within 5 km, and in a 30-degree sector downwind to 10 km.

WorldPop 2020 at 1 km (CC BY 4.0), one 19 MB file for India. Every score needs the
same two sums at a different point, so they are precomputed once as rasters:
a 5 km disk sum, and twelve 10 km sector sums, one per 30 degrees of downwind
bearing. Scoring an event is then a lookup. The grid is in degrees, so a
kilometre is fewer columns in the north than the south; the kernels are rebuilt
for each 5-degree band of latitude.

Smoke does not spread in a circle: a fire upwind of a town is a different problem
from the same fire downwind of it, which is what the sector sum captures.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

URL = ("https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/IND/"
       "ind_ppp_2020_1km_Aggregated.tif")
DISK_KM = 5.0
SECTOR_KM = 10.0
SECTOR_DEG = 30.0
BEARINGS = np.arange(0.0, 360.0, SECTOR_DEG)
BAND_DEG = 5.0
KM_PER_DEG_LAT = 110.574


def raster_path() -> Path:
    from firewatch.config import settings
    return settings().raw_dir / "worldpop" / "ind_ppp_2020_1km_Aggregated.tif"


def download(path: Path | None = None) -> Path:
    import requests

    path = path or raster_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(URL, timeout=600)
        r.raise_for_status()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)
    return path


def disk_kernel(dy_km: float, dx_km: float, radius_km: float) -> np.ndarray:
    ry, rx = int(np.ceil(radius_km / dy_km)), int(np.ceil(radius_km / dx_km))
    yy, xx = np.mgrid[-ry:ry + 1, -rx:rx + 1]
    return ((yy * dy_km) ** 2 + (xx * dx_km) ** 2 <= radius_km ** 2).astype(np.float32)


def sector_kernel(dy_km: float, dx_km: float, radius_km: float, bearing_deg: float,
                  width_deg: float = SECTOR_DEG) -> np.ndarray:
    """Cells within ``radius_km`` whose bearing from the centre is within
    ``width_deg / 2`` of ``bearing_deg`` (clockwise from north). Row 0 is north."""
    ry, rx = int(np.ceil(radius_km / dy_km)), int(np.ceil(radius_km / dx_km))
    yy, xx = np.mgrid[-ry:ry + 1, -rx:rx + 1]
    north, east = -yy * dy_km, xx * dx_km
    dist = np.hypot(north, east)
    bearing = np.degrees(np.arctan2(east, north)) % 360
    off = np.abs((bearing - bearing_deg + 180) % 360 - 180)
    return ((dist <= radius_km) & (dist > 0.5) & (off <= width_deg / 2)).astype(np.float32)


class Population:
    """Precomputed disk and sector sums over India, sampled at points. The same
    grid and convolution serve any other layer (``disk_sum``), such as assets."""

    def __init__(self, path: Path | None = None) -> None:
        import rasterio

        with rasterio.open(path or download()) as ds:
            pop = ds.read(1).astype(np.float32)
            self.west, self.north = ds.transform.c, ds.transform.f
            self.step = ds.transform.a
            nodata = ds.nodata
        pop[(pop < 0) | ~np.isfinite(pop) | (pop == nodata)] = 0.0
        self.shape = pop.shape
        self.disk = self.disk_sum(pop, DISK_KM)
        self.sectors = np.stack([self._convolve(pop, lambda dy, dx, b=b: sector_kernel(
            dy, dx, SECTOR_KM, b)) for b in BEARINGS])

    def _convolve(self, grid: np.ndarray, kernel) -> np.ndarray:
        """Sum ``grid`` under ``kernel(dy_km, dx_km)`` centred on every cell, the
        kernel rebuilt per 5-degree latitude band.

        A correlation, not a convolution: convolving flips the kernel, which would
        count the people *upwind*. The disk does not care; the sectors do.
        """
        from scipy.signal import fftconvolve

        rows = grid.shape[0]
        out = np.zeros_like(grid, dtype=np.float32)
        lat_top = self.north - np.arange(rows) * self.step
        dy = self.step * KM_PER_DEG_LAT
        margin = int(np.ceil(max(SECTOR_KM, DISK_KM) / dy)) + 1
        for band_top in np.arange(np.ceil(self.north / BAND_DEG) * BAND_DEG,
                                  lat_top[-1] - BAND_DEG, -BAND_DEG):
            in_band = np.flatnonzero((lat_top <= band_top) & (lat_top > band_top - BAND_DEG))
            if not len(in_band):
                continue
            dx = self.step * 111.320 * np.cos(np.radians(band_top - BAND_DEG / 2))
            r0, r1 = max(in_band[0] - margin, 0), min(in_band[-1] + margin + 1, rows)
            keep = slice(in_band[0] - r0, in_band[-1] - r0 + 1)
            k = kernel(dy, dx)[::-1, ::-1]
            out[in_band] = fftconvolve(grid[r0:r1], k, mode="same")[keep]
        return np.clip(out, 0, None)

    def disk_sum(self, grid: np.ndarray, radius_km: float) -> np.ndarray:
        return self._convolve(grid, lambda dy, dx: disk_kernel(dy, dx, radius_km))

    def rasterise(self, lat, lon, weight) -> np.ndarray:
        """Sum ``weight`` at points onto this grid."""
        r, c, ok = self._index(lat, lon)
        grid = np.zeros(self.shape, dtype=np.float32)
        np.add.at(grid, (r[ok], c[ok]), np.asarray(weight, dtype=np.float32)[ok])
        return grid

    def sample(self, grid: np.ndarray, lat, lon) -> np.ndarray:
        r, c, ok = self._index(lat, lon)
        return np.where(ok, grid[r, c], 0.0)

    def _index(self, lat, lon) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        r = np.floor((self.north - np.asarray(lat, dtype=float)) / self.step).astype(np.int64)
        c = np.floor((np.asarray(lon, dtype=float) - self.west) / self.step).astype(np.int64)
        ok = (r >= 0) & (r < self.shape[0]) & (c >= 0) & (c < self.shape[1])
        return r.clip(0, self.shape[0] - 1), c.clip(0, self.shape[1] - 1), ok

    def within_5km(self, lat, lon) -> np.ndarray:
        return self.sample(self.disk, lat, lon)

    def downwind(self, lat, lon, bearing_deg) -> np.ndarray:
        """People in the 30-degree sector centred on each point's downwind bearing
        (NaN bearing -> NaN: no wind record)."""
        r, c, ok = self._index(lat, lon)
        b = np.asarray(bearing_deg, dtype=float)
        i = (np.round(np.nan_to_num(b) / SECTOR_DEG).astype(np.int64)) % len(BEARINGS)
        return np.where(ok & np.isfinite(b), self.sectors[i, r, c], np.nan)


@lru_cache(maxsize=1)
def population() -> Population:
    return Population()
