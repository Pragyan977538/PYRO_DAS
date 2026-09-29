"""Observability: was the sky clear enough to see a fire at all?

It is the denominator of persistence (nights detected / nights observable) and
the map's "where were we blind" layer. FIRMS publishes no swath footprints and no
cloud masks, so cloud comes from elsewhere:

* **Real data:** NASA POWER's daily cloud amount, from CERES SYN1deg -- satellite-
  observed cloud on a 1-degree grid. One regional request returns a 10 x 10
  degree grid for a whole year, so all of India for 2012-2024 is ~160 requests.
  Open-Meteo's ERA5 was the first plan, but its free tier counts each 14 days per
  location as a call: 13 years for a few hundred cells would take weeks of quota.
* **Mock mode:** the fixture's ERA5-shaped table.

Daily data carries ``daynight = 'A'``; ``source`` names the grid a ``cell_id``
belongs to.
"""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from firewatch.config import settings
from firewatch.grid import grid_cell
from firewatch.ingest.load import copy_rows, logged, mark_loaded

log = logging.getLogger(__name__)

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/regional"
POWER_SOURCE = "power_syn1deg"   # 1-degree cells
POWER_DEG = 1.0
FIXTURE_SOURCE = "fixture"       # 0.25-degree ERA5-shaped cells
# India's box in POWER-sized pieces (lat0, lat1, lon0, lon1). A regional request
# takes one parameter and a box 2 to 10 degrees on a side; the service answers 422
# to anything narrower, so the northern strip (36-37 N) is fetched as 36-38 N.
TILES = [(la, la + span, lo, lo + 10)
         for la, span in ((6, 10), (16, 10), (26, 10), (36, 2)) for lo in (68, 78, 88)]
COLUMNS = ["cell_id", "obs_date", "daynight", "cloud_frac", "source"]
CONFLICT = "(cell_id, obs_date, daynight) DO UPDATE SET cloud_frac = EXCLUDED.cloud_frac, " \
           "source = EXCLUDED.source"


def fetch_power_tile(tile: tuple[int, int, int, int], year: int,
                     session: requests.Session | None = None) -> pd.DataFrame:
    """One tile-year of daily cloud amount, as observability rows."""
    la0, la1, lo0, lo1 = tile
    params = {"latitude-min": la0, "latitude-max": la1, "longitude-min": lo0,
              "longitude-max": lo1, "parameters": "CLOUD_AMT", "community": "RE",
              "start": f"{year}0101", "end": f"{year}1231", "format": "JSON",
              "time-standard": "UTC"}
    r = (session or requests).get(POWER_URL, params=params, timeout=300)
    r.raise_for_status()
    payload = r.json()
    fill = payload.get("header", {}).get("fill_value", -999.0)
    rows = []
    for feature in payload.get("features", []):
        lon, lat = feature["geometry"]["coordinates"][:2]
        cell = int(grid_cell(lat, lon, POWER_DEG))
        for day, value in feature["properties"]["parameter"]["CLOUD_AMT"].items():
            if value is None or value == fill:
                continue
            rows.append((cell, day, min(max(value / 100.0, 0.0), 1.0)))
    frame = pd.DataFrame(rows, columns=["cell_id", "day", "cloud_frac"])
    frame["obs_date"] = pd.to_datetime(frame["day"], format="%Y%m%d").dt.date
    frame["daynight"] = "A"
    frame["source"] = POWER_SOURCE
    return frame[COLUMNS]


def load_power(years: list[int], pause_s: float = 1.0) -> int:
    """Every tile for every year, skipping tile-years already logged."""
    total = 0
    with requests.Session() as session:
        for year in years:
            for tile in TILES:
                item = f"{year}|{tile[0]}_{tile[2]}"
                if logged(POWER_SOURCE, item):
                    continue
                frame = fetch_power_tile(tile, year, session)
                n = copy_rows("observability", COLUMNS, frame, conflict=CONFLICT)
                mark_loaded(POWER_SOURCE, item, n)
                total += n
                log.info("cloud %s: %d cell-days", item, n)
                time.sleep(pause_s)
    return total


def load_fixture_cloud() -> int:
    """The fixture's ERA5-shaped table, for mock mode."""
    path = settings().mock_dir / "observability" / "cloud.csv"
    if not path.exists() or logged(FIXTURE_SOURCE, path.name):
        return 0
    frame = pd.read_csv(path)
    frame["source"] = FIXTURE_SOURCE
    n = copy_rows("observability", COLUMNS, frame[COLUMNS], conflict=CONFLICT)
    mark_loaded(FIXTURE_SOURCE, path.name, n)
    return n


def load_observability(years: list[int], mock: bool | None = None) -> int:
    use_mock = settings().mock_mode if mock is None else mock
    return load_fixture_cloud() if use_mock else load_power(years)
