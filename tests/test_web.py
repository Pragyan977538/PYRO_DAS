"""Stage 9: the map, as far as it can be tested without a browser.

The offline guarantee is the one that matters at the finale: nothing the page
loads may come from another host. The rest checks that the app is served at /,
that the basemap route behaves, and that the JavaScript names every layer the
acceptance criteria toggle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
OWN = ["index.html", "app.js", "style.css"]


@pytest.mark.parametrize("name", OWN)
def test_no_external_hosts(name):
    """Venue internet is unreliable: every URL the page fetches must be local."""
    text = (WEB / name).read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\s\"'`)]+", text)
    assert not [u for u in urls if not re.match(r"https?://(localhost|127\.0\.0\.1)", u)], urls


def test_vendored_maplibre_is_complete():
    assert (WEB / "vendor" / "maplibre-gl.js").stat().st_size > 500_000
    assert (WEB / "vendor" / "maplibre-gl.css").exists()
    assert (WEB / "vendor" / "LICENSE.maplibre.txt").exists()
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'src="vendor/maplibre-gl.js"' in html and 'href="vendor/maplibre-gl.css"' in html


def test_every_class_has_its_own_layer():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for cat in ("industrial", "agricultural", "forest", "other_natural", "unclassified"):
        assert re.search(rf"\b{cat}:\s*{{\s*label", js), cat
    for src in ("oil_gas", "heavy_industry", "mining", "provisional"):
        assert re.search(rf"\b{src}:\s*{{\s*label", js), src
    for layer in ("`det-${key}`", "`src-${key}`", '"det-alerts"', '"events"', '"cloud"'):
        assert layer in js, layer


def test_detail_panel_shows_the_reason():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'class="reason' in js and "p.reason" in js
    for field in ("Temperature", "Nearest named facility", "FRP against this site's own baseline",
                  "Class"):
        assert field in js, field


def test_app_and_basemap_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from firewatch import config

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config._cached = None
    try:
        from firewatch.api.main import app
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200 and "PYRO_DAS" in r.text
        assert r.headers["cache-control"] == "no-cache"
        assert client.get("/app.js").status_code == 200
        assert client.get("/basemap/countries.geojson").status_code == 404   # not built here
        assert client.get("/basemap/secrets.geojson").status_code == 404
        (tmp_path / "basemap").mkdir()
        (tmp_path / "basemap" / "countries.geojson").write_text(
            '{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        ok = client.get("/basemap/countries.geojson")
        assert ok.status_code == 200 and ok.json()["type"] == "FeatureCollection"
    finally:
        config._cached = None
