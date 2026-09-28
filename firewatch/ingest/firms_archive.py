"""The FIRMS yearly archive for India: public CSVs, no key.

Real history comes from here (2012 onward for VIIRS), so every stage is built on
real detections. In mock mode the same loader reads the fixture's files, which use
the same names and columns.

Each file is one unit in the ingest log: a backfill interrupted halfway resumes at
the next unloaded file, and reloading a file is a no-op.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests

from firewatch.config import settings
from firewatch.ingest.load import load_detections, logged, mark_loaded
from firewatch.ingest.normalize import dedupe_sp_nrt, normalise_firms, supersede_nrt

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://firms.modaps.eosdis.nasa.gov/data/country/{slug}/{year}/{slug}_{year}_India.csv"
#: Archive prefix -> first year it has India data. NOAA-21 is not in the yearly
#: archive yet; its history comes from the API.
SLUGS = {"viirs-snpp": 2012, "viirs-jpss1": 2018, "modis": 2000}
SOURCE = "firms_archive"


def archive_dir(mock: bool | None = None) -> Path:
    cfg = settings()
    use_mock = cfg.mock_mode if mock is None else mock
    return (cfg.mock_dir if use_mock else cfg.raw_dir) / "firms"


def file_name(slug: str, year: int) -> str:
    return f"{slug}_{year}_India.csv"


def download(slug: str, year: int, directory: Path) -> Path | None:
    """Fetch one yearly file. None when FIRMS has not published that year yet."""
    path = directory / file_name(slug, year)
    if path.exists():
        return path
    directory.mkdir(parents=True, exist_ok=True)
    url = ARCHIVE_URL.format(slug=slug, year=year)
    with requests.get(url, stream=True, timeout=300) as r:
        if r.status_code == 404:
            log.info("not published yet: %s", url)
            return None
        r.raise_for_status()
        part = path.with_suffix(".part")
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    part.replace(path)
    return path


def read_file(path: Path) -> pd.DataFrame:
    """One archive file, normalised and de-duplicated."""
    raw = pd.read_csv(path, dtype={"acq_time": str, "version": str})
    return dedupe_sp_nrt(normalise_firms(raw))


def load_file(path: Path, force: bool = False) -> int:
    """Load one archive file. Returns new rows; 0 if already loaded."""
    if not force and logged(SOURCE, path.name):
        return 0
    df = read_file(path)
    # SP arriving after NRT: remove the NRT rows it now covers, sensor by sensor.
    for sensor, part in df[df["product"] == "SP"].groupby("sensor"):
        first = part["acq_datetime"].min().floor("D")
        last = part["acq_datetime"].max().floor("D") + timedelta(days=1)
        supersede_nrt(sensor, first.to_pydatetime(), last.to_pydatetime())
    inserted = load_detections(df)
    mark_loaded(SOURCE, path.name, inserted)
    log.info("%s: %d rows new", path.name, inserted)
    return inserted


def load_archive(years: list[int], slugs: list[str] | None = None,
                 mock: bool | None = None, fetch: bool = True) -> dict[str, int]:
    """Load every available (slug, year) file; download missing ones unless mock."""
    cfg = settings()
    use_mock = cfg.mock_mode if mock is None else mock
    directory = archive_dir(use_mock)
    loaded: dict[str, int] = {}
    for slug in slugs or list(SLUGS):
        for year in years:
            if year < SLUGS[slug]:
                continue
            path = directory / file_name(slug, year)
            if not path.exists() and fetch and not use_mock:
                path = download(slug, year, directory) or path
            if path.exists():
                loaded[path.name] = load_file(path)
    return loaded
