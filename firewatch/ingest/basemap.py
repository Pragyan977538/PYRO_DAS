"""An offline basemap: Natural Earth vector layers, clipped to India, as GeoJSON.

Venue internet at the finale is unreliable, so the map may not fetch a single
tile from the network. Natural Earth (public domain) gives land, the national
boundary, states, rivers and cities in a few megabytes. The national boundary is
Natural Earth's **India point-of-view** edition, which draws India's boundaries as
India claims them -- the depiction an Indian government audience expects.
Plain, and entirely local: ``make basemap`` builds it once into DATA_DIR/basemap.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)
BASE = "https://naciscdn.org/naturalearth/10m"
LAYERS = {
    "countries": f"{BASE}/cultural/ne_10m_admin_0_countries_ind.zip",
    "states": f"{BASE}/cultural/ne_10m_admin_1_states_provinces.zip",
    "rivers": f"{BASE}/physical/ne_10m_rivers_lake_centerlines.zip",
    "places": f"{BASE}/cultural/ne_10m_populated_places_simple.zip",
}
BBOX = (60.0, 2.0, 102.0, 40.0)      # India and its neighbours, a little beyond the data
MIN_CITY_POP = 500_000


def basemap_dir() -> Path:
    from firewatch.config import settings
    return settings().data_dir / "basemap"


def _download(url: str, raw: Path) -> Path:
    path = raw / url.rsplit("/", 1)[1]
    if not path.exists():
        raw.mkdir(parents=True, exist_ok=True)
        r = requests.get(url, timeout=600, headers={"User-Agent": "firewatch-sih26162"})
        r.raise_for_status()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)
    return path


def build(force: bool = False) -> dict[str, int]:
    """Download, clip and write the four layers. Returns features per layer."""
    import geopandas as gpd
    from shapely.geometry import box

    from firewatch.config import settings

    out_dir = basemap_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = settings().raw_dir / "naturalearth"
    clip = box(*BBOX)
    counts = {}
    for name, url in LAYERS.items():
        target = out_dir / f"{name}.geojson"
        if target.exists() and not force:
            counts[name] = -1
            continue
        frame = gpd.read_file(_download(url, raw))
        frame = frame[frame.intersects(clip)]
        if name == "countries":
            frame = frame[["NAME", "ADM0_A3", "geometry"]].rename(
                columns={"NAME": "name", "ADM0_A3": "iso3"})
            frame["geometry"] = frame.geometry.intersection(clip).simplify(0.005)
        elif name == "states":
            frame = frame[frame["admin"] == "India"][["name", "geometry"]]
            frame["geometry"] = frame.geometry.simplify(0.005).boundary
        elif name == "rivers":
            frame = frame[frame["scalerank"] <= 8][["name", "scalerank", "geometry"]]
            frame["geometry"] = frame.geometry.intersection(clip).simplify(0.005)
        elif name == "places":
            keep = (frame["pop_max"] >= MIN_CITY_POP) | frame["featurecla"].str.contains(
                "capital", case=False, na=False)
            frame = frame[keep & (frame["adm0name"] == "India")][
                ["name", "pop_max", "featurecla", "geometry"]]
        frame = frame[~frame.geometry.is_empty]
        frame.to_file(target, driver="GeoJSON")
        counts[name] = int(len(frame))
        log.info("basemap %s: %d features, %.1f MB", name, len(frame),
                 target.stat().st_size / 1e6)
    return counts
