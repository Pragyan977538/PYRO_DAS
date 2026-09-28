"""The FIRMS area API: 2025 onward, and the live near-real-time feed.

The yearly archive covers history for free; this client fills what the archive
hasn't published yet and pulls the live NRT products every three hours. It needs
``FIRMS_MAP_KEY``. Suomi NPP delivery ends on 1 November 2026, so the live
products are sensor-agnostic: NOAA-20, NOAA-21 and MODIS carry on after it.

Closed date ranges are pulled in 10-day chunks (the API's maximum) and logged
chunk by chunk, so an interrupted backfill resumes. The live window is re-pulled
each time instead of logged: NRT fills in over hours, and the natural key makes
re-loading free.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from firewatch.config import settings
from firewatch.ingest.load import load_detections, logged, mark_loaded
from firewatch.ingest.normalize import dedupe_sp_nrt, normalise_firms

log = logging.getLogger(__name__)

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
LIVE_PRODUCTS = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT")
MAX_DAYS = 10
SOURCE = "firms_api"


class FirmsApiError(RuntimeError):
    """The API answered, but not with detections (bad key, bad product, quota)."""


class FirmsApi:
    """Thin client over the area endpoint. ``session`` is injectable for tests."""

    def __init__(self, map_key: str | None = None, bbox: str | None = None,
                 session: requests.Session | None = None, pause_s: float = 1.5) -> None:
        cfg = settings()
        self.map_key = map_key or cfg.require_firms_key()
        self.bbox = bbox or cfg.bbox_str
        self.session = session or requests.Session()
        self.pause_s = pause_s   # the limit is 5,000 transactions per 10 minutes

    def fetch(self, product: str, start: date, days: int = MAX_DAYS) -> pd.DataFrame:
        """Raw FIRMS rows for ``days`` days from ``start`` (inclusive)."""
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be 1..{MAX_DAYS}")
        url = f"{BASE}/{self.map_key}/{product}/{self.bbox}/{days}/{start:%Y-%m-%d}"
        r = self.session.get(url, timeout=120)
        text = r.text
        # FIRMS reports errors as plain text, sometimes with a 200.
        if r.status_code != 200 or not text.startswith("latitude"):
            raise FirmsApiError(f"{product} {start}: HTTP {r.status_code}: {text[:200]}")
        return pd.read_csv(io.StringIO(text), dtype={"acq_time": str, "version": str})

    def _load(self, raw: pd.DataFrame) -> int:
        if raw.empty:
            return 0
        return load_detections(dedupe_sp_nrt(normalise_firms(raw)))

    def backfill(self, product: str, start: date, end: date) -> int:
        """Load ``[start, end]`` in 10-day chunks, skipping chunks already logged."""
        total, day = 0, start
        while day <= end:
            days = min(MAX_DAYS, (end - day).days + 1)
            item = f"{product}|{day:%Y-%m-%d}|{days}"
            if not logged(SOURCE, item):
                n = self._load(self.fetch(product, day, days))
                mark_loaded(SOURCE, item, n)
                total += n
                time.sleep(self.pause_s)
            day += timedelta(days=days)
        return total

    def pull_recent(self, days: int = 2, products: tuple[str, ...] = LIVE_PRODUCTS) -> int:
        """The live pull: the last ``days`` days of every NRT product, not logged."""
        start = date.today() - timedelta(days=days - 1)
        total = 0
        for product in products:
            try:
                total += self._load(self.fetch(product, start, days))
            except FirmsApiError as exc:   # e.g. S-NPP after 1 Nov 2026
                log.warning("skipping %s: %s", product, exc)
            time.sleep(self.pause_s)
        return total


class MockFirmsApi(FirmsApi):
    """The same interface over the fixture's NRT files, for MOCK_MODE=1."""

    FILES = {"VIIRS_SNPP_NRT": "VIIRS_SNPP_NRT.csv", "VIIRS_NOAA20_NRT": "VIIRS_NOAA20_NRT.csv",
             "MODIS_NRT": "MODIS_NRT.csv"}

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or settings().mock_dir / "firms_nrt"
        self.pause_s = 0.0

    def _read(self, product: str) -> pd.DataFrame:
        name = self.FILES.get(product)
        if name is None or not (self.directory / name).exists():
            raise FirmsApiError(f"{product}: no fixture file")
        return pd.read_csv(self.directory / name, dtype={"acq_time": str, "version": str})

    def window(self, product: str) -> tuple[date, date]:
        """First and last day the fixture has for a product."""
        when = pd.to_datetime(self._read(product)["acq_date"]).dt.date
        return when.min(), when.max()

    def fetch(self, product: str, start: date, days: int = MAX_DAYS) -> pd.DataFrame:
        raw = self._read(product)
        when = pd.to_datetime(raw["acq_date"]).dt.date
        return raw[(when >= start) & (when < start + timedelta(days=days))].reset_index(
            drop=True)


def client() -> FirmsApi:
    """The live client, or the fixture-backed one in mock mode."""
    return MockFirmsApi() if settings().mock_mode else FirmsApi()
