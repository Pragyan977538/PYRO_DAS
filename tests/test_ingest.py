"""Stage 2: ingestion, against a throwaway database.

Everything here runs in mock mode on a separate ``firewatch_test`` database, so
fixture rows never mix with the real archive. The database is created fresh for
this module, migrated twice (migrations must be re-runnable), filled by the real
loaders, and dropped afterwards. Skipped when no PostgreSQL is reachable.

The ordering tests matter most: NRT loaded before SP and NRT loaded after SP must
both end with no pixel counted twice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from conftest import fresh_database, postgres_reachable

from firewatch import config, db

REPO = Path(__file__).resolve().parent.parent
TEST_DB = "firewatch_test"


pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")


@pytest.fixture(scope="module")
def ingest_db(tmp_path_factory):
    """A fresh migrated database, mock mode, and the fixture on disk."""
    yield from fresh_database(TEST_DB, tmp_path_factory)


def q(sql: str, **params) -> list[dict]:
    return db.fetch_all(sql, params)


def overlap() -> int:
    return q("""SELECT count(*) AS n FROM detections n WHERE n.product = 'NRT' AND EXISTS (
                  SELECT 1 FROM detections s WHERE s.product = 'SP' AND s.sensor = n.sensor
                    AND s.acq_datetime::date = n.acq_datetime::date)""")[0]["n"]


# ------------------------------------------------------------------ schema

def test_migrations_rerun_and_database_is_utc(ingest_db):
    tables = {r["tablename"] for r in q(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"detections", "ingest_log", "vnf_raw", "gihs_reference",
            "osm_industrial", "observability"} <= tables
    assert q("SELECT current_setting('TimeZone') AS tz")[0]["tz"] in ("UTC", "Etc/UTC")


# --------------------------------------------- NRT first, then SP, then NRT

def test_nrt_loaded_before_sp_is_superseded(ingest_db):
    from firewatch.ingest.firms_api import MockFirmsApi
    from firewatch.ingest.firms_archive import load_archive

    api = MockFirmsApi()
    for product in api.FILES:
        assert api.backfill(product, *api.window(product)) > 0
    nrt_before = q("SELECT count(*) AS n FROM detections WHERE product = 'NRT'")[0]["n"]

    loaded = load_archive([2022, 2023])
    assert sum(loaded.values()) > 20_000
    assert overlap() == 0, "SP must supersede the NRT rows it covers"
    nrt_after = q("SELECT count(*) AS n FROM detections WHERE product = 'NRT'")[0]["n"]
    assert 0 < nrt_after < nrt_before, "only NRT beyond the SP end survives"


def test_nrt_loaded_after_sp_is_refused_on_covered_days(ingest_db):
    from firewatch.ingest.firms_api import MockFirmsApi

    before = q("SELECT count(*) AS n FROM detections")[0]["n"]
    api = MockFirmsApi()
    for product in api.FILES:        # re-pull the whole window, bypassing the log
        start, end = api.window(product)
        raw = pd.concat([api.fetch(product, d.date(), 1)
                         for d in pd.date_range(start, end)], ignore_index=True)
        api._load(raw)
    assert q("SELECT count(*) AS n FROM detections")[0]["n"] == before
    assert overlap() == 0


def test_reloading_the_archive_is_a_no_op(ingest_db):
    from firewatch.ingest.firms_archive import archive_dir, load_archive, load_file

    before = q("SELECT count(*) AS n FROM detections")[0]["n"]
    assert sum(load_archive([2022, 2023]).values()) == 0          # logged: skipped
    forced = load_file(archive_dir() / "viirs-snpp_2023_India.csv", force=True)
    assert forced == 0                                             # natural key
    assert q("SELECT count(*) AS n FROM detections")[0]["n"] == before


def test_loaded_rows_keep_provenance(ingest_db):
    rows = q("""SELECT instrument, product, count(*) AS n,
                       count(*) FILTER (WHERE firms_type IS NOT NULL) AS typed
                  FROM detections GROUP BY 1, 2""")
    by = {(r["instrument"], r["product"]): r for r in rows}
    assert set(by) == {("VIIRS", "SP"), ("VIIRS", "NRT"), ("MODIS", "SP"), ("MODIS", "NRT")}
    assert by[("VIIRS", "SP")]["typed"] == by[("VIIRS", "SP")]["n"]   # archive has type
    assert by[("VIIRS", "NRT")]["typed"] == 0                          # NRT never does
    bad = q("SELECT count(*) AS n FROM detections WHERE NOT ST_Equals(geom, "
            "ST_SetSRID(ST_MakePoint(longitude, latitude), 4326))")[0]["n"]
    assert bad == 0


# ----------------------------------------------------------- other sources

def test_vnf_joins_onto_night_viirs_only(ingest_db):
    from firewatch.ingest.vnf import load_vnf

    staged = load_vnf()
    assert staged["joined"] > 0
    r = q("""SELECT count(*) FILTER (WHERE vnf_temp_k IS NOT NULL) AS temp,
                    count(*) FILTER (WHERE vnf_temp_k IS NOT NULL
                                     AND (daynight <> 'N' OR instrument <> 'VIIRS')) AS wrong,
                    count(*) FILTER (WHERE daynight = 'N') AS night, count(*) AS total
               FROM detections""")[0]
    assert r["wrong"] == 0
    assert r["temp"] < r["night"], "VNF is night-only and fits often fail"
    assert load_vnf()["joined"] == 0, "the join is idempotent"


def test_observability_from_the_fixture(ingest_db):
    from firewatch.ingest.observability import load_observability

    assert load_observability([2022, 2023]) > 0
    r = q("SELECT min(cloud_frac) AS lo, max(cloud_frac) AS hi, count(*) AS n "
          "FROM observability WHERE source = 'fixture'")[0]
    assert r["n"] > 0 and 0 <= r["lo"] <= r["hi"] <= 1
    assert load_observability([2022, 2023]) == 0


def test_osm_from_fixture_facilities(ingest_db):
    from firewatch.ingest.osm import load_osm

    assert load_osm() == 8
    groups = {r["label_group"]: r["n"] for r in q(
        "SELECT label_group, count(*) AS n FROM osm_industrial GROUP BY 1")}
    assert groups == {"oil_gas": 5, "steel_cement": 1, "thermal_power": 1, "mining": 1}


def test_check_script_passes(ingest_db):
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "check_ingest.py")],
                          capture_output=True, text=True, env=os.environ.copy(), check=False)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    assert "PASS" in proc.stdout


# ------------------------------------------------ pieces without a database

def test_label_group_priority():
    from firewatch.ingest.osm import label_group

    assert label_group({"industrial": "refinery", "landuse": "industrial"}) == "oil_gas"
    assert label_group({"man_made": "works", "product": "steel"}) == "steel_cement"
    assert label_group({"power": "plant", "plant:source": "coal"}) == "thermal_power"
    assert label_group({"power": "plant", "plant:source": "solar"}) is None
    assert label_group({"landuse": "quarry"}) == "mining"
    assert label_group({"man_made": "kiln"}) == "kiln"
    assert label_group({"landuse": "industrial"}) == "industrial_other"
    assert label_group({"landuse": "residential"}) is None


def test_osm_extract_reads_points_polygons_and_skips_the_rest(tmp_path):
    from firewatch.ingest.osm import extract

    osm = tmp_path / "tiny.osm"
    nodes = "".join(f'<node id="{i}" version="1" lat="{22.3 + (i % 2) * 0.01}" '
                    f'lon="{69.8 + (i // 2) * 0.01}"/>' for i in range(1, 5))
    osm.write_text(f"""<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  {nodes}
  <node id="10" version="1" lat="22.35" lon="69.87"><tag k="man_made" v="flare"/></node>
  <node id="11" version="1" lat="22.36" lon="69.88"><tag k="amenity" v="cafe"/></node>
  <way id="20" version="1"><nd ref="1"/><nd ref="2"/><nd ref="4"/><nd ref="3"/><nd ref="1"/>
    <tag k="industrial" v="refinery"/><tag k="name" v="Test refinery"/></way>
  <way id="21" version="1"><nd ref="1"/><nd ref="2"/><nd ref="4"/><nd ref="3"/><nd ref="1"/>
    <tag k="landuse" v="residential"/></way>
</osm>""", encoding="utf-8")
    frame = extract(osm).set_index(["osm_type", "osm_id"])
    assert set(frame.index) == {("n", 10), ("w", 20)}
    assert frame.loc[("n", 10), "wkt"].startswith("POINT")
    assert frame.loc[("w", 20), "wkt"].startswith("MULTIPOLYGON")
    assert frame.loc[("w", 20), "name"] == "Test refinery"


class _FakeResponse:
    def __init__(self, text: str, status: int = 200, payload=None):
        self.text, self.status_code, self._payload = text, status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeSession:
    def __init__(self, reply):
        self.reply, self.urls = reply, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.reply(url, kwargs)


def test_firms_api_url_shape_and_errors():
    from firewatch.ingest.firms_api import FirmsApi, FirmsApiError

    header = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
              "instrument,confidence,version,bright_ti5,frp,daynight\n")
    session = _FakeSession(lambda url, kw: _FakeResponse(header))
    api = FirmsApi("KEY123", "68,6,98,37", session=session, pause_s=0)
    assert api.fetch("VIIRS_NOAA20_NRT", date(2025, 1, 1), 10).empty
    assert session.urls[0].endswith("/KEY123/VIIRS_NOAA20_NRT/68,6,98,37/10/2025-01-01")
    with pytest.raises(ValueError):
        api.fetch("VIIRS_NOAA20_NRT", date(2025, 1, 1), 11)
    bad = FirmsApi("BAD", "68,6,98,37", pause_s=0, session=_FakeSession(
        lambda url, kw: _FakeResponse("Invalid MAP_KEY.", 400)))
    with pytest.raises(FirmsApiError, match="Invalid MAP_KEY"):
        bad.fetch("MODIS_NRT", date(2025, 1, 1))


def test_firms_api_demands_a_key_only_when_built(monkeypatch):
    from firewatch.ingest.firms_api import FirmsApi

    monkeypatch.setenv("FIRMS_MAP_KEY", "")
    config._cached = None
    try:
        with pytest.raises(config.ConfigError, match="FIRMS_MAP_KEY"):
            FirmsApi()
    finally:
        config._cached = None


def test_power_tile_parsing():
    from firewatch.grid import grid_cell
    from firewatch.ingest.observability import POWER_SOURCE, fetch_power_tile

    payload = {"header": {"fill_value": -999.0}, "features": [
        {"geometry": {"coordinates": [70.5, 20.5, 0]},
         "properties": {"parameter": {"CLOUD_AMT": {"20230101": 14.2, "20230102": -999.0,
                                                    "20230103": 101.0}}}}]}
    session = _FakeSession(lambda url, kw: _FakeResponse("", payload=payload))
    frame = fetch_power_tile((20, 30, 70, 80), 2023, session)
    assert len(frame) == 2                                   # the fill value is dropped
    assert frame["cloud_frac"].tolist() == [pytest.approx(0.142), 1.0]   # clipped to 1
    assert (frame["cell_id"] == int(grid_cell(20.5, 70.5, 1.0))).all()
    assert set(frame["daynight"]) == {"A"} and set(frame["source"]) == {POWER_SOURCE}


def test_power_tiles_cover_india_within_service_limits():
    """POWER refuses a regional box under 2 degrees a side (HTTP 422), which
    stopped the first real backfill at the 36-37 N strip."""
    from firewatch.config import settings
    from firewatch.ingest.observability import TILES

    for la0, la1, lo0, lo1 in TILES:
        assert 2 <= la1 - la0 <= 10 and 2 <= lo1 - lo0 <= 10, (la0, la1, lo0, lo1)
    w, s, e, n = settings().india_bbox
    covered = lambda lat, lon: any(la0 <= lat < la1 and lo0 <= lon < lo1  # noqa: E731
                                   for la0, la1, lo0, lo1 in TILES)
    assert all(covered(lat + 0.5, lon + 0.5)
               for lat in range(int(s), int(n)) for lon in range(int(w), int(e)))


def test_landcover_live_sample():
    from firewatch.ingest.landcover import sample

    try:
        classes = sample([30.40, 19.0], [75.60, 70.0])   # rural Punjab; Arabian Sea
    except Exception as exc:  # pragma: no cover - offline
        pytest.skip(f"WorldCover unreachable: {exc}")
    if classes[0] == 0:
        pytest.skip("WorldCover unreachable")
    assert classes[0] == 40 and classes[1] == 0
