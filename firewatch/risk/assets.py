"""The critical asset register: what a fire could hit.

Static, and never derived from thermal history. A nuclear plant dumps its waste
heat into cooling water far below what a satellite detects, so it is thermally
invisible until something burns; a register built from fires would score it
zero. The same goes for LPG plants, depots and chemical works.

The original design's register was ~200 entries compiled by hand from PESO, CEA and
MoPNG lists. For the prototype it is generated instead (approved 2026-09-29):
- **WRI Global Power Plant Database:** every combustion plant, and India's nuclear
  plants
- **OSM:** refineries, oil and gas fields, chemical and fertiliser works, steel and
  cement, mines, kilns and industrial estates, typed from their tags
Rows added by hand (``source_ref`` 'manual:...') survive reseeding, so the PESO and
CEA lists can still be layered on.

Criticality follows the original design's table; mining, kilns and oil and gas fields,
which it does not list, are placed between its rows and marked as ours.
"""

from __future__ import annotations

import logging
import re

import pandas as pd

log = logging.getLogger(__name__)

#: asset type -> criticality (original design table; * = added here)
CRITICALITY = {
    "nuclear_power_station": 1.00,   # * thermally invisible, highest consequence
    "lng_lpg_terminal": 1.00,
    "oil_refinery": 0.95,
    "petrochemical": 0.90,
    "chemical_plant": 0.80,
    "thermal_power_station": 0.70,
    "fertiliser_plant": 0.70,
    "oil_gas_field": 0.60,           # * wells and flares: blowouts, few people
    "major_port": 0.60,
    "steel_cement": 0.50,
    "mine": 0.40,                    # * coal-seam fires, open-cast workings
    "industrial_estate": 0.30,
    "kiln": 0.25,                    # * like a warehouse: small, many
}
#: Model 1 / Road A class of a burning source -> criticality of the thing burning.
CLASS_CRITICALITY = {"oil_gas": 0.80, "heavy_industry": 0.60, "mining": 0.40,
                     "industrial": 0.30, "kiln": 0.25, "offshore": 0.60}


def osm_type(group: str, tags: dict) -> str | None:
    """Asset type of an OSM feature from its label group and tags."""
    text = " ".join(str(tags.get(k, "")) for k in ("industrial", "product", "name",
                                                   "operator", "man_made")).lower()
    if re.search(r"\blng\b|\blpg\b|liquefied", text):
        return "lng_lpg_terminal"
    if tags.get("industrial") == "refinery" or "refinery" in text:
        return "oil_refinery"
    if "petrochem" in text:
        return "petrochemical"
    if re.search(r"fertili[sz]er|urea", text):
        return "fertiliser_plant"
    if tags.get("industrial") in ("chemical", "pharmaceutical") or \
            re.search(r"chemical|pharma", text):
        return "chemical_plant"
    return {"oil_gas": "oil_gas_field", "steel_cement": "steel_cement",
            "thermal_power": "thermal_power_station", "mining": "mine", "kiln": "kiln",
            "industrial_other": "industrial_estate"}.get(group)


GPPD_TYPE = {"Nuclear": "nuclear_power_station", "Coal": "thermal_power_station",
             "Gas": "thermal_power_station", "Oil": "thermal_power_station",
             "Biomass": "thermal_power_station", "Petcoke": "thermal_power_station",
             "Cogeneration": "thermal_power_station", "Waste": "thermal_power_station"}


def build() -> dict:
    """Regenerate the register's OSM and GPPD rows; keep manual ones."""
    from firewatch.db import fetch_all, get_conn

    osm = pd.DataFrame(fetch_all(
        "SELECT osm_type, osm_id, name, label_group, tags FROM osm_industrial"))
    osm["asset_type"] = [osm_type(g, t or {}) for g, t in zip(osm["label_group"], osm["tags"],
                                                             strict=True)]
    osm = osm[osm["asset_type"].notna()]
    rows = [(f"osm:{r.osm_type}{r.osm_id}", r.name, r.asset_type,
             CRITICALITY[r.asset_type], (r.tags or {}).get("operator"))
            for r in osm.itertuples()]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM critical_assets WHERE source_ref LIKE 'osm:%%' "
                    "OR source_ref LIKE 'gppd:%%'")
        cur.execute("CREATE TEMP TABLE asset_stage (source_ref text, name text, "
                    "asset_type text, criticality real, operator text) ON COMMIT DROP")
        from psycopg2.extras import execute_values
        execute_values(cur, "INSERT INTO asset_stage VALUES %s", rows, page_size=5000)
        cur.execute("""
            INSERT INTO critical_assets (name, geom, footprint, asset_type, criticality,
                                         operator, source_ref)
            SELECT coalesce(s.name, initcap(replace(s.asset_type, '_', ' '))),
                   ST_PointOnSurface(o.geom), o.geom, s.asset_type, s.criticality,
                   s.operator, s.source_ref
              FROM asset_stage s
              JOIN osm_industrial o ON s.source_ref = 'osm:' || o.osm_type || o.osm_id""")
        n_osm = cur.rowcount
        # WRI plants, unless OSM already has that plant within 2 km.
        fuels = ", ".join(cur.mogrify("(%s, %s, %s)", (f, t, CRITICALITY[t])).decode()
                          for f, t in GPPD_TYPE.items())
        cur.execute(f"""
            INSERT INTO critical_assets (name, geom, footprint, asset_type, criticality,
                                         operator, source_ref)
            SELECT p.name, p.geom, p.geom, t.asset_type, t.crit, p.owner,
                   'gppd:' || p.gppd_id
              FROM power_plants p
              JOIN (VALUES {fuels}) AS t(fuel, asset_type, crit) ON t.fuel = p.primary_fuel
             WHERE t.asset_type = 'nuclear_power_station' OR NOT EXISTS (
                   SELECT 1 FROM critical_assets a
                    WHERE a.asset_type = t.asset_type
                      AND a.geom && ST_Expand(p.geom, 0.03)
                      AND ST_DWithin(a.footprint::geography, p.geom::geography, 2000))""")
        n_gppd = cur.rowcount
    counts = {r["asset_type"]: r["n"] for r in fetch_all(
        "SELECT asset_type, count(*) AS n FROM critical_assets GROUP BY 1 ORDER BY 2 DESC")}
    log.info("critical assets: %d from OSM, %d from GPPD", n_osm, n_gppd)
    return {"from_osm": n_osm, "from_gppd": n_gppd, "by_type": counts}
