"""Stage 8: the API, end to end on the fixture.

A throwaway database gets the fixture's registry, an inference run and its events;
then every endpoint is called through FastAPI's test client. The acceptance calls
from docs/ROADMAP.md are here too: sources by class (legacy ``flare`` included), a
vector tile, and the OpenAPI page.
"""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import fresh_database, postgres_reachable

pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="no PostgreSQL reachable")
WINDOW = (pd.Timestamp("2023-06-01", tz="UTC"), pd.Timestamp("2023-09-01", tz="UTC"))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import sys
    from pathlib import Path

    from fastapi.testclient import TestClient

    from firewatch.inference import events as ev
    from firewatch.ingest.firms_archive import load_archive
    from firewatch.ingest.observability import load_observability
    from firewatch.ingest.osm import load_osm
    from firewatch.registry.build import build, read_detections, write
    from firewatch.registry.cells import Gate

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from run_inference import run

    for _ in fresh_database("firewatch_test_api", tmp_path_factory):
        load_archive([2022, 2023])
        load_observability([2022, 2023])
        load_osm()
        write(build(read_detections("VIIRS"), read_detections("MODIS"), Gate(3, 10, 2)))
        run(*WINDOW)
        det, provisional, promotions, cells, meta = ev.read_run()
        events, assigned = ev.assemble(det, provisional, promotions, cells, meta["window_end"])
        ev.write(events, assigned, meta)
        from firewatch.api.main import app
        yield TestClient(app)


def test_health_and_meta(client):
    assert client.get("/api/health").json()["database"] is True
    meta = client.get("/api/meta").json()
    assert meta["window"]["window_start"].startswith("2023-06-01")
    assert meta["sources"]["registry"] > 0


def test_sources_by_class_and_legacy_alias(client):
    everything = client.get("/api/sources?bbox=68,6,98,37").json()
    assert everything["type"] == "FeatureCollection" and everything["features"]
    classes = {f["properties"]["cls"] for f in everything["features"]}
    for cls in classes - {None}:
        got = client.get(f"/api/sources?bbox=68,6,98,37&class={cls}").json()["features"]
        assert got and {f["properties"]["cls"] for f in got} == {cls}
    flare = client.get("/api/sources?bbox=68,6,98,37&class=flare").json()
    assert flare["meta"]["class_filter"] == "oil_gas"


def test_bad_bbox_is_a_422(client):
    assert client.get("/api/sources?bbox=98,6,68,37").status_code == 422
    assert client.get("/api/sources?bbox=a,b,c,d").status_code == 422


def test_source_detail_and_timeseries(client):
    sid = client.get("/api/sources").json()["features"][0]["properties"]["source_id"]
    detail = client.get(f"/api/sources/{sid}").json()
    assert detail["properties"]["source_id"] == sid and "baselines" in detail["properties"]
    ts = client.get(f"/api/sources/{sid}/timeseries").json()
    assert ts["daily"] and ts["passes"]
    assert client.get("/api/sources/999999").status_code == 404


def test_detections_carry_their_reason(client):
    feats = client.get("/api/detections?limit=500").json()["features"]
    assert feats
    for f in feats:
        p = f["properties"]
        assert p["road"] in (1, 2, 3) and p["reason"]


def test_detection_detail_panel(client):
    b = client.get("/api/detections?road=2&limit=1").json()["features"][0]["properties"]
    p = client.get(f"/api/detections/{b['detection_id']}").json()["properties"]
    assert p["baseline"]["baseline_key"] and p["baseline"]["breach"] is False
    assert p["temperature"]["vnf_k"] is None and "licence" in p["temperature"]["note"]
    assert p["reason"].startswith(("normal for this site", "watch"))


def test_alerts_and_events(client):
    alerts = client.get("/api/detections?road=3&limit=50").json()["features"]
    assert alerts and all(f["properties"]["alert"] for f in alerts)
    events = client.get("/api/events?limit=50").json()["features"]
    assert events and {"kind", "reason", "status"} <= set(events[0]["properties"])
    eid = events[0]["properties"]["event_id"]
    one = client.get(f"/api/events/{eid}").json()
    assert one["properties"]["timeline"] and "risk_breakdown" in one
    blowout = client.get("/api/events?kind=new_source").json()["features"]
    assert blowout, "the fixture's blowout is a new-source incident"


def test_observability_layer(client):
    r = client.get("/api/observability?date=2023-07-15")
    assert r.status_code == 200 and r.json()["type"] == "FeatureCollection"


@pytest.mark.parametrize("path", ["/api/tiles/detections/6/40/28.mvt",
                                  "/api/tiles/detections/5/22/14.mvt",
                                  "/api/tiles/events/5/22/14.mvt"])
def test_tiles(client, path):
    r = client.get(path + "?start=2023-06-01&end=2023-08-31")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"


def test_tile_over_a_source_has_content(client):
    """Jamnagar's flare at zoom 10, raw points."""
    import math

    lat, lon, z = 22.34, 69.87, 10
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    r = client.get(f"/api/tiles/detections/{z}/{x}/{y}.mvt?start=2023-06-01&end=2023-08-31")
    assert r.status_code == 200 and len(r.content) > 0


def test_openapi_docs(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/sources", "/api/events/{event_id}",
            "/api/tiles/detections/{z}/{x}/{y}.mvt"} <= set(paths)
