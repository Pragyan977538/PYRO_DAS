"""WRI Global Power Plant Database (v1.3, CC BY 4.0): India's power plants.

A heavy_industry label source for Model 1 (CLAUDE.md's class table), next to OSM's
``power=plant``: OSM tags a plant's fuel inconsistently, and GPPD fills the gaps
with a named, geolocated list. Only combustion plants label a heat source; the
whole list is kept for the map.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import requests

from firewatch.config import settings
from firewatch.ingest.load import copy_rows, logged, mark_loaded

log = logging.getLogger(__name__)

URL = ("https://raw.githubusercontent.com/wri/global-power-plant-database/master/"
       "output_database/global_power_plant_database.csv")
SOURCE = "gppd"
#: Fuels burned on site. Nuclear is left out on purpose: its waste heat goes into
#: cooling water at 30-40 C, far below detection (CLAUDE.md, critical assets).
THERMAL_FUELS = ("Coal", "Gas", "Oil", "Biomass", "Petcoke", "Cogeneration", "Waste")
COLUMNS = ["gppd_id", "name", "primary_fuel", "capacity_mw", "commissioning_year",
           "owner", "longitude", "latitude"]


def download(raw_dir: Path) -> Path:
    path = raw_dir / "gppd" / "global_power_plant_database.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(URL, timeout=300)
        r.raise_for_status()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)
    return path


def read_india(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df = df[df["country"] == "IND"].rename(columns={"gppd_idnr": "gppd_id"})
    return df[COLUMNS].dropna(subset=["latitude", "longitude"])


def load_gppd() -> int:
    """Load India's plants once. Real data only: the fixture has none."""
    cfg = settings()
    if cfg.mock_mode:
        return 0
    path = download(cfg.raw_dir)
    if logged(SOURCE, path.name):
        return 0
    frame = read_india(path)
    n = copy_rows("power_plants", COLUMNS, frame, conflict="DO NOTHING",
                  geom_from=("longitude", "latitude"))
    mark_loaded(SOURCE, path.name, n)
    log.info("GPPD: %d Indian power plants", n)
    return n
