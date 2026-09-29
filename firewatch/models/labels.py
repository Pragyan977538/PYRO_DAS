"""Weak labels for Model 1, built only from location.

A registry source is labelled by what is mapped around it: OSM industry tags and
WRI's power plant list, within 1 km of any of its cells. Where several groups are
within reach, the most specific wins, in this order (the order of the approved
Stage 1.5b census):

    oil_gas > steel_cement > thermal_power > mining > kiln > industrial_other

Mining sits above generic industry on purpose: a coalfield is ringed by washeries
and depots tagged ``landuse=industrial``, and the fire is in the mine. Kiln labels
proved wrong at registry sources (one was a Raniganj coal fire), so a source whose
first group is kiln gets no label rather than a guess.

Every label is location. That is why Model 1 must never see location: a model
given the inputs below would learn the labelling rule and nothing else.
``LABEL_INPUTS`` declares them, and ``scripts/train.py`` asserts that none of them
is a feature.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from firewatch.db import get_conn
from firewatch.ingest.gppd import THERMAL_FUELS
from firewatch.ingest.osm import CLASS_OF

log = logging.getLogger(__name__)

GROUP_ORDER = ("oil_gas", "steel_cement", "thermal_power", "mining", "kiln",
               "industrial_other")
CLASSES = ("oil_gas", "heavy_industry", "mining")
BIOMASS = "recurrent_biomass"
MATCH_M = 1000.0
#: The conditional fourth class (ROADMAP Stage 4): added only if at least this many
#: sources with no industrial label sit on cropland or tree cover.
BIOMASS_MIN_SOURCES = 30
BIOMASS_MAX_INDUSTRIAL = 0.5
BIOMASS_LANDCOVER = {40: "worldcover_cropland", 10: "worldcover_tree_cover"}

#: What each label is built from. None of these may ever be a Model 1 feature.
LABEL_INPUTS: dict[str, set[str]] = {
    "oil_gas": {"osm_tags", "osm_distance", "cell_geom"},
    "heavy_industry": {"osm_tags", "osm_distance", "gppd_fuel", "gppd_distance",
                       "cell_geom"},
    "mining": {"osm_tags", "osm_distance", "cell_geom"},
    BIOMASS: {"landcover", "cell_geom"},
}
#: Location in any other form, and the evaluation-only references. Also barred.
LOCATION_INPUTS = {"lat", "lon", "latitude", "longitude", "x", "y", "geom", "cell_geom",
                   "state_code", "dist_industrial", "landcover", "osm_distance",
                   "gppd_distance"}
EVALUATION_ONLY = {"firms_type", "type2_frac", "gihs", "on_gihs"}


def barred_inputs() -> set[str]:
    """Every name that may not appear among Model 1's features."""
    return set().union(*LABEL_INPUTS.values()) | LOCATION_INPUTS | EVALUATION_ONLY


_NEAR_SQL = """
SELECT c.source_id, o.label_group AS grp, 'osm:' || o.osm_type || o.osm_id AS evidence,
       o.osm_id AS osm_ref, o.name, ST_Distance(c.geom::geography, o.geom::geography) AS d
  FROM source_cells c
  JOIN osm_industrial o
    ON o.geom && ST_Expand(c.geom, %(deg)s)
   AND ST_DWithin(c.geom::geography, o.geom::geography, %(m)s)
UNION ALL
SELECT c.source_id, 'thermal_power', 'gppd:' || p.gppd_id, NULL, p.name,
       ST_Distance(c.geom::geography, p.geom::geography)
  FROM source_cells c
  JOIN power_plants p
    ON p.geom && ST_Expand(c.geom, %(deg)s)
   AND ST_DWithin(c.geom::geography, p.geom::geography, %(m)s)
   AND p.primary_fuel = ANY(%(fuels)s)
"""


def groups_near() -> pd.DataFrame:
    """Nearest object of each label group within ``MATCH_M`` of any source cell:
    one row per (source, group)."""
    # ST_Expand pre-filter in degrees: 1.6 km of latitude, and >= 1.3 km of
    # longitude anywhere south of 37 N -- enough to contain every 1 km match.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_NEAR_SQL, {"deg": 0.015, "m": MATCH_M, "fuels": list(THERMAL_FUELS)})
        rows = pd.DataFrame(cur.fetchall(), columns=["source_id", "grp", "evidence",
                                                     "osm_ref", "name", "d"])
    if rows.empty:
        return rows
    rows = rows.sort_values("d").drop_duplicates(["source_id", "grp"])
    return rows.reset_index(drop=True)


def assign(sources: pd.Index, near: pd.DataFrame) -> pd.DataFrame:
    """The first group in ``GROUP_ORDER`` near each source, and its class."""
    out = pd.DataFrame(index=pd.Index(sources, name="source_id"))
    out["label"] = None
    out["label_group"] = None
    out["evidence"] = None
    out["evidence_name"] = None
    out["osm_ref"] = None
    out["distance_m"] = np.nan
    out["groups_near"] = [{} for _ in range(len(out))]
    if near.empty:
        return out
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    near = near.assign(rank=near["grp"].map(rank)).sort_values(["source_id", "rank"])
    for sid, g in near.groupby("source_id"):
        if sid not in out.index:
            continue
        first = g.iloc[0]
        out.at[sid, "label_group"] = first["grp"]
        out.at[sid, "label"] = CLASS_OF.get(first["grp"])      # kiln -> None
        out.at[sid, "evidence"] = first["evidence"]
        out.at[sid, "evidence_name"] = first["name"]
        out.at[sid, "osm_ref"] = first["osm_ref"]
        out.at[sid, "distance_m"] = round(float(first["d"]), 1)
        out.at[sid, "groups_near"] = {r.grp: round(float(r.d)) for r in g.itertuples()}
    return out


def landcover_of(source_cells: pd.DataFrame, sampler) -> pd.Series:
    """Majority WorldCover class over each source's cells (0 = no data)."""
    classes = sampler(source_cells["lat"].to_numpy(), source_cells["lon"].to_numpy())
    frame = pd.DataFrame({"source_id": source_cells["source_id"].to_numpy(),
                          "lc": np.asarray(classes)})
    frame = frame[frame["lc"] > 0]
    if frame.empty:
        return pd.Series(dtype=int)
    return frame.groupby("source_id")["lc"].agg(lambda s: int(s.mode().iat[0]))


def biomass_candidates(labels: pd.DataFrame, landcover: pd.Series) -> pd.Index:
    """Sources the conditional recurrent_biomass class would label.

    Sets ``labels["landcover"]``. Candidates have no label group at all -- a
    kiln-first source is industrial evidence, just untrustworthy -- and sit on
    cropland or tree cover.
    """
    labels["landcover"] = landcover.reindex(labels.index).astype("Int64")
    free = labels["label_group"].isna()
    on_land = labels["landcover"].isin(list(BIOMASS_LANDCOVER)).fillna(False).astype(bool)
    return labels.index[free & on_land]


def biomass_decision(candidates: pd.Index, industrial: set[int]) -> dict:
    """Is WorldCover an honest label source for recurrent biomass here?

    Two conditions, both fixed before training: enough candidates (the ROADMAP's
    ~30), and fewer than half of them on an imagery-confirmed industrial site
    (``industrial``: GIHS, evaluation only). The second was added on 2026-09-29,
    after the first build: of 42 candidates, most sat on GIHS-confirmed industry --
    mines and plants in forested or farmed country that OSM has not mapped. A
    label source that is mostly wrong is not an honest one. GIHS decides only
    whether the class exists; it never labels a source.
    """
    share = float(np.isin(candidates, list(industrial)).mean()) if len(candidates) else 0.0
    add = len(candidates) >= BIOMASS_MIN_SOURCES and share < BIOMASS_MAX_INDUSTRIAL
    return {"candidates": int(len(candidates)), "min_candidates": BIOMASS_MIN_SOURCES,
            "on_confirmed_industry": round(share, 3),
            "max_on_confirmed_industry": BIOMASS_MAX_INDUSTRIAL, "class_added": bool(add)}


def add_biomass(labels: pd.DataFrame, candidates: pd.Index) -> None:
    labels.loc[candidates, "label"] = BIOMASS
    labels.loc[candidates, "label_group"] = labels.loc[candidates, "landcover"].map(
        BIOMASS_LANDCOVER)
    labels.loc[candidates, "evidence"] = "worldcover:" + labels.loc[
        candidates, "landcover"].astype(str)


def write(labels: pd.DataFrame) -> None:
    """Replace every source's label; mirror the evidence onto ``sources``."""
    from psycopg2.extras import execute_values

    rows = [(int(sid), r.label, r.label_group, r.evidence, r.evidence_name,
             None if pd.isna(r.distance_m) else float(r.distance_m),
             json.dumps(r.groups_near),
             None if pd.isna(r.landcover) else int(r.landcover))
            for sid, r in labels.iterrows()]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM source_labels")
        execute_values(cur, """
            INSERT INTO source_labels (source_id, label, label_group, evidence,
                                       evidence_name, distance_m, groups_near, landcover)
            VALUES %s""", rows)
        cur.execute("""
            UPDATE sources s SET label_source = l.evidence, osm_name = l.evidence_name,
                   osm_ref = CASE WHEN l.evidence LIKE 'osm:%%'
                                  THEN substring(l.evidence FROM 6)::bigint END
              FROM source_labels l WHERE l.source_id = s.source_id""")
