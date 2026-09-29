"""Projection and grid helpers shared by every stage.

One implementation of the metre projection and of the two grids, so the registry,
the router and the observability writer can never disagree about which cell a
point falls in.

Metres come from EPSG:7755 (WGS 84 / India NSF LCC): conformal, so a local
distance is right in every direction, up to a smooth scale factor that stays
within ~2% anywhere in India. The registry grid is 375 m -- one VIIRS pixel -- in that plane.
Observability grids are regular lat/lon grids at the cloud source's own
resolution (NASA POWER's 1 degree; the fixture's ERA5-shaped 0.25 degree): a finer
grid would only repeat the same value.

Why not the equirectangular shortcut ``x = lon * 111320 * cos(lat)``: with each
point's own latitude in the cosine it shears the plane. At India's longitudes two
points 500 m apart north-south come out ~300 m apart east-west as well, which is
the whole scale of a DBSCAN neighbourhood.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from pyproj import Transformer

INDIA_CRS = "EPSG:7755"
CELL_M = 375.0
ERA5_DEG = 0.25
_ERA5_COLS = int(round(360 / ERA5_DEG))


@lru_cache(maxsize=1)
def _forward() -> Transformer:
    return Transformer.from_crs("EPSG:4326", INDIA_CRS, always_xy=True)


@lru_cache(maxsize=1)
def _inverse() -> Transformer:
    return Transformer.from_crs(INDIA_CRS, "EPSG:4326", always_xy=True)


def to_metres(lat, lon) -> tuple[np.ndarray, np.ndarray]:
    """Project to EPSG:7755 metres: (easting, northing)."""
    x, y = _forward().transform(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float))
    return np.asarray(x), np.asarray(y)


def from_metres(x, y) -> tuple[np.ndarray, np.ndarray]:
    """Back from EPSG:7755 metres: (latitude, longitude)."""
    lon, lat = _inverse().transform(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return np.asarray(lat), np.asarray(lon)


def offset(lat, lon, dx_m, dy_m) -> tuple[np.ndarray, np.ndarray]:
    """Move points east by ``dx_m`` and north by ``dy_m`` metres, in the same plane
    ``to_metres`` measures in, so a move and a measurement always agree."""
    x, y = to_metres(lat, lon)
    lon2, lat2 = _inverse().transform(x + np.asarray(dx_m, dtype=float),
                                      y + np.asarray(dy_m, dtype=float))
    return np.asarray(lat2), np.asarray(lon2)


def cell_375(lat, lon) -> np.ndarray:
    """Registry cell key per point.

    EPSG:7755 coordinates over India are positive and below ten million metres, so
    packing the northing cell into the last five digits keeps keys unique.
    """
    return cell_key(*to_metres(lat, lon))


def cell_key(x, y) -> np.ndarray:
    """Registry cell key from coordinates already in EPSG:7755 metres."""
    return (np.floor(np.asarray(x) / CELL_M).astype(np.int64) * 100_000
            + np.floor(np.asarray(y) / CELL_M).astype(np.int64))


def cell_bounds(key) -> tuple[np.ndarray, np.ndarray]:
    """South-west corner, in metres, of registry cells."""
    col, row = np.divmod(np.asarray(key, dtype=np.int64), 100_000)
    return col * CELL_M, row * CELL_M


def grid_cell(lat, lon, deg: float) -> np.ndarray:
    """Id of a regular lat/lon grid cell: row counted from the north pole, column
    from the antimeridian. Which grid an id belongs to travels with it (the
    observability table's ``source``), never inferred from the number."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    cols = int(round(360 / deg))
    row = np.floor((90.0 - lat) / deg).astype(np.int64)
    col = np.floor((lon + 180.0) / deg).astype(np.int64)
    return row * cols + col


def grid_cell_centre(cell, deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of a grid cell's centre."""
    cell = np.asarray(cell, dtype=np.int64)
    row, col = np.divmod(cell, int(round(360 / deg)))
    return 90.0 - (row + 0.5) * deg, (col + 0.5) * deg - 180.0


def era5_cell(lat, lon) -> np.ndarray:
    """ERA5 0.25-degree cell id."""
    return grid_cell(lat, lon, ERA5_DEG)


def era5_cell_centre(cell) -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of an ERA5 cell's centre."""
    return grid_cell_centre(cell, ERA5_DEG)
