"""Stage 1.5b - label census: which classes can Model 1 honestly be trained on?

National OSM counts ("22 flares in India") don't answer the question. What matters
is how many *registry sources* sit near an object that can label them -- a
refinery polygon labels a flare source whether or not anyone tagged the flare.
This counts, for the registry from the recommended multi-year gate, how many
sources fall within 1 km of each label group.

OSM comes from a one-off Overpass pull restricted to India and cached locally.
That is fine for a count; the production ingest (Stage 2) still uses the Geofabrik
extract (docs/DESIGN.md §3.12). Thermal power plants are cross-checked against the WRI
Global Power Plant Database (CC BY 4.0).

GIHS is used only to describe the result (are unlabelled sources on GIHS sites?),
never as a label -- the 83% on-GIHS figure has to stay independent.

    python scripts/spike_labels.py      # after scripts/spike_gate_multiyear.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import spike_cluster as sc  # noqa: E402

from firewatch.config import settings  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "firewatch-sih26162/0.1 (student research; one-off label census)"
GPPD_URL = ("https://raw.githubusercontent.com/wri/global-power-plant-database/master/"
            "output_database/global_power_plant_database.csv")
MATCH_M = 1000

# Label groups, in priority order: a source near several gets the first. The
# order puts the most specific evidence first -- a refinery inside a generic
# industrial estate is oil & gas, not "industrial".
GROUPS: dict[str, list[str]] = {
    "oil_gas": ['nwr["industrial"~"^(refinery|oil)$"]',
                'nwr["man_made"~"^(petroleum_well|flare)$"]'],
    "steel_cement": ['nwr["man_made"="works"]["product"~"steel|iron|cement|alumin"]',
                     'nwr["industrial"~"^(steel|cement)$"]'],
    "thermal_power": ['nwr["power"="plant"]["plant:source"~"coal|gas|oil|diesel|biomass"]'],
    "mining": ['nwr["landuse"="quarry"]', 'nwr["resource"="coal"]',
               'nwr["industrial"="mine"]', 'nwr["man_made"="mineshaft"]'],
    "kiln": ['nwr["man_made"="kiln"]', 'nwr["industrial"~"^(brickyard|brickworks)$"]'],
    "industrial_other": ['nwr["landuse"="industrial"]', 'nwr["man_made"="works"]',
                         'nwr["industrial"="factory"]'],
}
OUT = REPO / "reports" / "stage1_5b"


def overpass(group: str, selectors: list[str], cache: Path) -> dict:
    path = cache / f"{group}.json"
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_bytes())
    body = "".join(f"{s}(area.in);" for s in selectors)
    query = (f'[out:json][timeout:600];area["ISO3166-1"="IN"][admin_level=2]->.in;'
             f"({body});out geom;")
    for attempt in range(4):
        try:
            r = requests.post(OVERPASS, data={"data": query}, timeout=900,
                              headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()  # validate before caching
            # Raw bytes, not write_text: Windows would encode OSM's Indian-script
            # names as cp1252 and fail halfway, leaving a truncated cache.
            tmp = path.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(path)
            return data
        except (requests.RequestException, ValueError) as exc:
            print(f"   overpass {group} attempt {attempt + 1} failed: {exc}")
            time.sleep(30 * (attempt + 1))
    raise SystemExit(f"Overpass unavailable for {group}")


def to_geometries(elements: list[dict]):
    """OSM elements -> shapely geometries. Closed ways become polygons, so a
    source inside a mine or refinery measures zero, not the distance to its edge."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import polygonize, unary_union

    geoms = []
    for el in elements:
        if el["type"] == "node":
            geoms.append(Point(el["lon"], el["lat"]))
        elif el["type"] == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                geoms.append(Polygon(pts))
            elif len(pts) >= 2:
                geoms.append(LineString(pts))
            else:
                geoms.append(Point(pts[0]))
        elif el["type"] == "relation":
            lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                     for m in el.get("members", []) if len(m.get("geometry") or []) >= 2]
            if lines:
                polys = list(polygonize(lines))
                geoms.append(unary_union(polys) if polys else unary_union(lines))
    # OSM polygons are often self-intersecting; GEOS distance can refuse those.
    from shapely import make_valid
    return [make_valid(g) for g in geoms]


def gppd_india(raw: Path):
    """Thermal plants in India from the WRI Global Power Plant Database."""
    import geopandas as gpd

    path = raw / "gppd" / "global_power_plant_database.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(GPPD_URL, timeout=300)
        r.raise_for_status()
        path.write_bytes(r.content)
    df = pd.read_csv(path, low_memory=False)
    df = df[(df["country"] == "IND") & df["primary_fuel"].isin(["Coal", "Gas", "Oil"])]
    return gpd.GeoDataFrame(df[["name", "primary_fuel"]],
                            geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
                            crs="EPSG:4326")


def main() -> int:
    import geopandas as gpd

    cfg = settings()
    work = cfg.data_dir / "interim" / "spike_multiyear"
    cells_csv = work / "registry_cells.csv"
    if not cells_csv.exists():
        raise SystemExit("run scripts/spike_gate_multiyear.py first")
    cells = pd.read_csv(cells_csv)
    pts = gpd.GeoDataFrame(cells, geometry=gpd.points_from_xy(cells["lon"], cells["lat"]),
                           crs="EPSG:4326").to_crs(7755)
    cache = cfg.raw_dir / "osm_census"
    cache.mkdir(parents=True, exist_ok=True)

    near_group: dict[str, set[int]] = {}
    osm_counts: dict[str, int] = {}
    for group, selectors in GROUPS.items():
        print(f">> {group}")
        elements = overpass(group, selectors, cache)["elements"]
        geoms = to_geometries(elements)
        osm_counts[group] = len(geoms)
        ref = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326").to_crs(7755)
        hit = gpd.sjoin_nearest(pts[["source", "geometry"]], ref, how="inner",
                                max_distance=MATCH_M)
        near_group[group] = set(hit["source"].unique())
        print(f"   {len(geoms):,} OSM objects; {len(near_group[group])} sources within 1 km")

    gppd = gppd_india(cfg.raw_dir).to_crs(7755)
    gppd_hit = gpd.sjoin_nearest(pts[["source", "geometry"]], gppd, how="inner",
                                 max_distance=MATCH_M)
    near_gppd = set(gppd_hit["source"].unique())

    sources = cells.groupby("source").agg(lat=("lat", "mean"), lon=("lon", "mean"),
                                          detections=("n_det", "sum"))

    def primary(s: int) -> str:
        for group in GROUPS:
            if s in near_group[group]:
                return group
        return "unlabelled"

    sources["label"] = [primary(s) for s in sources.index]
    sources["thermal_gppd"] = sources.index.isin(near_gppd)

    gihs = sc.load_gihs(cfg.raw_dir)
    if gihs is None:
        raise SystemExit("GIHS unavailable; the census reports on-GIHS shares")
    src = gpd.GeoDataFrame(sources, geometry=gpd.points_from_xy(sources["lon"],
                                                                sources["lat"]),
                           crs="EPSG:4326").to_crs(7755)
    on = gpd.sjoin_nearest(src[["geometry"]], gihs["confirmed"][["geometry"]],
                           how="inner", max_distance=MATCH_M)
    sources["on_gihs"] = sources.index.isin(on.index.unique())

    summary = sources.groupby("label").agg(
        sources=("detections", "size"), detections=("detections", "sum"),
        on_gihs=("on_gihs", "mean"))
    summary = summary.reindex([*GROUPS, "unlabelled"]).fillna(0)
    results = {
        "sources": int(len(sources)),
        "osm_objects_in_india": osm_counts,
        "sources_near_group_any": {g: len(v) for g, v in near_group.items()},
        "sources_near_gppd_thermal": len(near_gppd),
        "primary_label": {g: {"sources": int(row["sources"]),
                              "detections": int(row["detections"]),
                              "on_gihs": round(float(row["on_gihs"]), 3)}
                          for g, row in summary.iterrows()},
        "examples": {g: sources[sources["label"] == g].sort_values(
            "detections", ascending=False).head(5)[["lat", "lon", "detections"]]
            .round(3).reset_index().to_dict(orient="records")
            for g in [*GROUPS, "unlabelled"]},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "labels.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    lines = ["# Stage 1.5b - label census", "",
             f"{len(sources)} registry sources (recommended multi-year gate). A source is "
             f"labelled by the first group with an OSM object within {MATCH_M} m.", "",
             "| Label group | OSM objects in India | Sources near any | Primary label: sources "
             "| Detections | On a GIHS site |", "|---|---|---|---|---|---|"]
    for g in [*GROUPS, "unlabelled"]:
        p = results["primary_label"][g]
        objects = f"{osm_counts[g]:,}" if g in osm_counts else "-"
        near_any = results["sources_near_group_any"].get(g, "-")
        lines.append(f"| {g} | {objects} | {near_any} | {p['sources']} "
                     f"| {p['detections']:,} | {p['on_gihs']:.0%} |")
    lines.append("")
    lines.append(f"WRI GPPD thermal plants (coal/gas/oil) within {MATCH_M} m: "
                 f"{len(near_gppd)} sources.")
    (OUT / "labels.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((OUT / "labels.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    sys.exit(main())
