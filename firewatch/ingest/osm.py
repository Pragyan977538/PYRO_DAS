"""OpenStreetMap industry, from the Geofabrik India extract.

Geofabrik, not Overpass: a dated snapshot is reproducible and cannot be down
during a demo. pyosmium streams the extract in C++, assembles polygons, and hands
Python only objects carrying one of the keys below, so a 1.7 GB file needs no
native tools and no database-side OSM import.

Each feature gets a ``label_group`` by the tag rules of CLAUDE.md's class table,
first match in priority order. The fine groups are kept (thermal power apart from
steel, for instance); Stage 4 maps them onto its three classes. The same rules
drove the Stage 1.5b label census, so the census and the labels agree.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pandas as pd

from firewatch.config import settings
from firewatch.db import get_conn
from firewatch.ingest.load import logged, mark_loaded

log = logging.getLogger(__name__)
SOURCE = "osm"
KEYS = ("industrial", "man_made", "power", "landuse", "resource")

#: fine label group -> Model 1 class (CLAUDE.md). Kiln has no class: it is kept
#: as context only, since its labels at registry sources were wrong.
CLASS_OF = {"oil_gas": "oil_gas", "steel_cement": "heavy_industry",
            "thermal_power": "heavy_industry", "industrial_other": "heavy_industry",
            "mining": "mining", "kiln": None}


def label_group(tags: dict[str, str]) -> str | None:
    """The label group a feature belongs to, or None. First match wins."""
    ind = tags.get("industrial", "")
    mm = tags.get("man_made", "")
    if ind in ("refinery", "oil") or mm in ("petroleum_well", "flare"):
        return "oil_gas"
    if (mm == "works" and re.search(r"steel|iron|cement|alumin", tags.get("product", ""))) \
            or ind in ("steel", "cement"):
        return "steel_cement"
    if tags.get("power") == "plant" and re.search(r"coal|gas|oil|diesel|biomass",
                                                  tags.get("plant:source", "")):
        return "thermal_power"
    if tags.get("landuse") == "quarry" or tags.get("resource") == "coal" \
            or ind == "mine" or mm == "mineshaft":
        return "mining"
    if mm == "kiln" or ind in ("brickyard", "brickworks"):
        return "kiln"
    if tags.get("landuse") == "industrial" or mm == "works" or ind == "factory":
        return "industrial_other"
    return None


def extract(pbf: Path) -> pd.DataFrame:
    """Every labelled feature in an OSM file: type, id, name, group, tags, WKT."""
    import osmium

    wkt = osmium.geom.WKTFactory()
    rows = []
    processor = (osmium.FileProcessor(str(pbf))
                 .with_locations()
                 .with_areas()
                 .with_filter(osmium.filter.KeyFilter(*KEYS)))
    for obj in processor:
        tags = {t.k: t.v for t in obj.tags}
        group = label_group(tags)
        if group is None:
            continue
        try:
            if obj.is_node():
                kind, oid, geom = "n", obj.id, wkt.create_point(obj)
            elif obj.is_area():
                kind = "w" if obj.from_way() else "r"
                oid, geom = obj.orig_id(), wkt.create_multipolygon(obj)
            elif obj.is_way() and not obj.is_closed():
                kind, oid, geom = "w", obj.id, wkt.create_linestring(obj)
            else:
                continue   # closed ways arrive again as areas; relations likewise
        except (osmium.InvalidLocationError, RuntimeError):
            continue       # broken geometry in the source data
        rows.append((kind, oid, tags.get("name"), group, json.dumps(tags), geom))
    frame = pd.DataFrame(rows, columns=["osm_type", "osm_id", "name", "label_group",
                                        "tags", "wkt"])
    return frame.drop_duplicates(["osm_type", "osm_id"])


def _store(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False)
    buf.seek(0)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TEMP TABLE stage_osm (osm_type CHAR(1), osm_id BIGINT,
                       name TEXT, label_group TEXT, tags JSONB, wkt TEXT) ON COMMIT DROP""")
        cur.copy_expert("COPY stage_osm FROM STDIN WITH (FORMAT csv)", buf)
        cur.execute("""
            INSERT INTO osm_industrial (osm_type, osm_id, name, label_group, tags, geom)
            SELECT osm_type, osm_id, name, label_group, tags,
                   ST_MakeValid(ST_GeomFromText(wkt, 4326))
              FROM stage_osm
            ON CONFLICT (osm_type, osm_id) DO UPDATE
               SET name = EXCLUDED.name, label_group = EXCLUDED.label_group,
                   tags = EXCLUDED.tags, geom = EXCLUDED.geom""")
        return cur.rowcount


def _fixture_frame() -> pd.DataFrame:
    """Mock mode: the fixture's facilities, tagged like OSM."""
    path = settings().mock_dir / "truth" / "facilities.csv"
    if not path.exists():
        return pd.DataFrame()
    fac = pd.read_csv(path)
    rows = []
    for r in fac.itertuples():
        tags = dict(part.split("=", 1) for part in r.tag.split(";"))
        tags["name"] = r.name
        kind = "n" if r.wkt.startswith("POINT") else "w"
        rows.append((kind, int(r.facility_id), r.name, label_group(tags),
                     json.dumps(tags), r.wkt))
    return pd.DataFrame(rows, columns=["osm_type", "osm_id", "name", "label_group",
                                       "tags", "wkt"])


def load_osm(mock: bool | None = None, pbf: Path | None = None) -> int:
    """Load labelled OSM features: the newest Geofabrik extract, or the fixture."""
    cfg = settings()
    use_mock = cfg.mock_mode if mock is None else mock
    if use_mock:
        if logged(SOURCE, "fixture"):
            return 0
        n = _store(_fixture_frame())
        mark_loaded(SOURCE, "fixture", n)
        return n
    if pbf is None:
        extracts = sorted((cfg.raw_dir / "osm").glob("india-*.osm.pbf"))
        if not extracts:
            log.warning("OSM skipped: no extract in %s (see docs/ROADMAP.md, Stage 2)",
                        cfg.raw_dir / "osm")
            return 0
        pbf = extracts[-1]
    if logged(SOURCE, pbf.name):
        return 0
    frame = extract(pbf)
    n = _store(frame)
    mark_loaded(SOURCE, pbf.name, n)
    log.info("OSM %s: %d labelled features", pbf.name, n)
    return n
