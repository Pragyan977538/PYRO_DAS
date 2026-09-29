"""Daily 10 m wind over India, for the risk score's downwind exposure term.

NASA POWER, the same free, keyless service as the cloud record. It serves wind on
MERRA-2's 0.5 x 0.625 degree grid, so each row keeps its grid point's own
coordinates rather than a cell id. The *components* are stored, not a direction:
POWER's daily ``WD10M`` comes back as nonsense (-8094 for a bearing), and a day's
mean direction would be meaningless anyway -- the mean vector is not.

A regional request takes one parameter, so a tile-year is two requests (U, V).
"""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from firewatch.ingest.load import copy_rows, logged, mark_loaded
from firewatch.ingest.observability import POWER_URL, TILES

log = logging.getLogger(__name__)
SOURCE = "power_wind"
COLUMNS = ["latitude", "longitude", "obs_date", "u10m", "v10m"]
CONFLICT = "(obs_date, latitude, longitude) DO UPDATE SET u10m = EXCLUDED.u10m, " \
           "v10m = EXCLUDED.v10m"


def fetch_component(tile: tuple[int, int, int, int], year: int, param: str,
                    session: requests.Session | None = None) -> pd.DataFrame:
    la0, la1, lo0, lo1 = tile
    params = {"latitude-min": la0, "latitude-max": la1, "longitude-min": lo0,
              "longitude-max": lo1, "parameters": param, "community": "RE",
              "start": f"{year}0101", "end": f"{year}1231", "format": "JSON",
              "time-standard": "UTC"}
    r = (session or requests).get(POWER_URL, params=params, timeout=300)
    r.raise_for_status()
    payload = r.json()
    fill = payload.get("header", {}).get("fill_value", -999.0)
    rows = []
    for feature in payload.get("features", []):
        lon, lat = feature["geometry"]["coordinates"][:2]
        for day, value in feature["properties"]["parameter"][param].items():
            if value is not None and value != fill:
                rows.append((round(lat, 4), round(lon, 4), day, float(value)))
    frame = pd.DataFrame(rows, columns=["latitude", "longitude", "day", param.lower()])
    frame["obs_date"] = pd.to_datetime(frame.pop("day"), format="%Y%m%d").dt.date
    return frame


def fetch_tile(tile, year: int, session: requests.Session | None = None) -> pd.DataFrame:
    """Both components for one tile-year, joined."""
    u = fetch_component(tile, year, "U10M", session)
    v = fetch_component(tile, year, "V10M", session)
    return u.merge(v, on=["latitude", "longitude", "obs_date"], how="inner")[COLUMNS]


def load_wind(years: list[int], pause_s: float = 1.0) -> int:
    """Every tile for every year, skipping tile-years already logged."""
    total = 0
    with requests.Session() as session:
        for year in years:
            for tile in TILES:
                item = f"{year}|{tile[0]}_{tile[2]}"
                if logged(SOURCE, item):
                    continue
                frame = fetch_tile(tile, year, session)
                n = copy_rows("wind_daily", COLUMNS, frame, conflict=CONFLICT)
                mark_loaded(SOURCE, item, n)
                total += n
                log.info("wind %s: %d point-days", item, n)
                time.sleep(pause_s)
    return total


def read_wind(dates) -> pd.DataFrame:
    """Wind rows for the given dates."""
    from sqlalchemy import text

    from firewatch.db import engine
    with engine().connect() as conn:
        return pd.read_sql_query(text(
            "SELECT latitude, longitude, obs_date, u10m, v10m FROM wind_daily "
            "WHERE obs_date = ANY(:d)"), conn,
            params={"d": sorted({pd.Timestamp(d).date() for d in dates})})
