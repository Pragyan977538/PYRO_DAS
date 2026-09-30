"""Road A: a fire where nothing has burned before. Rules plus physics, never a model.

Its inputs -- land cover, distance to mapped industry -- are exactly what every
weak label is built from, so a model trained here could only learn the labelling
rule back (docs/DESIGN.md §3.1). Instead the rules are written down, applied in order,
first match wins, and the rule that fired becomes the reason on the map:

1. a mapped facility within 375 m -> its class. A specific one (refinery, steel,
   power, mine, kiln; OSM or a WRI combustion plant) counts anywhere within 375 m.
   Generic industry (``landuse=industrial``, works, factory) counts only if the
   fire is inside it, or beside it on built-up or bare land: the first 2024
   replay called stubble fires next to rice mills industrial.
2. bare ground within 2 km of a mapped mine -> mining (open-cast pits are bare)
3. open sea beyond the land tiles -> offshore (platform flare or vessel)
4. land cover: tree cover or mangrove -> forest; cropland -> agricultural;
   shrub, grass, wetland -> other vegetation; built-up, bare or water ->
   unclassified, saying why. Water is not "offshore": at ~300 m it is mostly
   rivers, char lands and wetlands.

Season never changes a class here -- it only adds context to the reason, so a
stubble fire outside the stubble season still reads as agricultural.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from firewatch.models.labels import GROUP_ORDER

INDUSTRY_M = 375.0      # one VIIRS pixel: a fire inside a plant can land this far out
MINE_BARE_M = 2000.0
FOREST, CROP, VEG = {10, 95}, {40}, {20, 30, 90, 100}
BUILT, BARE, WATER = {50}, {60}, {80}
CLASS_OF_GROUP = {"oil_gas": "oil_gas", "steel_cement": "heavy_industry",
                  "thermal_power": "heavy_industry", "mining": "mining", "kiln": "kiln",
                  "industrial_other": "industrial"}
CATEGORY = {"oil_gas": "industrial", "heavy_industry": "industrial", "mining": "industrial",
            "kiln": "industrial", "industrial": "industrial", "offshore": "industrial",
            "forest": "forest", "agricultural": "agricultural", "vegetation": "other_natural",
            "unclassified": "unclassified"}
GROUP_WORDS = {"oil_gas": "oil and gas", "steel_cement": "steel or cement works",
               "thermal_power": "thermal power plant", "mining": "mine",
               "kiln": "kiln or brickworks", "industrial_other": "industrial area"}


class Context:
    """Mapped industry (EPSG:7755 geometries) and a land-cover sampler.

    ``zero_is_sea``: whether the sampler's 0 means open sea (the WorldCover mosaic,
    where only land tiles exist) or merely "no data" (the fixture).
    """

    def __init__(self, industry: pd.DataFrame, landcover: Callable,
                 zero_is_sea: bool = True) -> None:
        import shapely

        self.industry = industry.reset_index(drop=True)
        self.tree = shapely.STRtree(self.industry["geometry"].to_numpy())
        mines = self.industry[self.industry["group"] == "mining"].reset_index(drop=True)
        self.mines = mines
        self.mine_tree = shapely.STRtree(mines["geometry"].to_numpy())
        self.landcover = landcover
        self.zero_is_sea = zero_is_sea
        self.rank = self.industry["group"].map({g: i for i, g in enumerate(GROUP_ORDER)})

    @classmethod
    def from_db(cls, mock: bool | None = None) -> Context:
        import geopandas as gpd
        from sqlalchemy import text

        from firewatch.config import settings
        from firewatch.db import engine
        from firewatch.ingest.gppd import THERMAL_FUELS
        from firewatch.ingest.landcover import Mosaic, sample_fixture

        with engine().connect() as conn:
            frame = gpd.read_postgis(text("""
                SELECT label_group AS "group", name, 'osm:' || osm_type || osm_id AS evidence,
                       geom FROM osm_industrial
                UNION ALL
                SELECT 'thermal_power', name, 'gppd:' || gppd_id, geom FROM power_plants
                 WHERE primary_fuel = ANY(:fuels)"""), conn, geom_col="geom",
                params={"fuels": list(THERMAL_FUELS)})
        frame = frame.to_crs(7755).rename_geometry("geometry")
        use_mock = settings().mock_mode if mock is None else mock
        sampler = sample_fixture if use_mock else Mosaic().sample
        return cls(pd.DataFrame(frame), sampler, zero_is_sea=not use_mock)


def _season_note(kind: str, month: int) -> str:
    if kind == "agricultural":
        if month in (4, 5):
            return " in the rabi harvest burning season (Apr-May)"
        if month in (10, 11):
            return " in the kharif stubble-burning season (Oct-Nov)"
        return ", outside the main stubble-burning seasons"
    if kind == "forest":
        return (" in the Feb-May forest-fire season" if month in (2, 3, 4, 5)
                else ", outside the Feb-May forest-fire season")
    return ""


def classify(det: pd.DataFrame, ctx: Context) -> pd.DataFrame:
    """``pred_class``, ``category`` and ``reason`` for Road A detections.

    ``det`` needs ``x``, ``y`` (metres), ``latitude``, ``longitude`` and
    ``acq_datetime``. Returns a frame on ``det``'s index.
    """
    import shapely

    n = len(det)
    out = pd.DataFrame({"pred_class": pd.Series([None] * n, dtype=object),
                        "reason": pd.Series([None] * n, dtype=object)}, index=det.index)
    if not n:
        out["category"] = pd.Series(dtype=object)
        return out
    points = shapely.points(det["x"].to_numpy(), det["y"].to_numpy())
    months = pd.DatetimeIndex(det["acq_datetime"]).month.to_numpy()
    cls = np.full(n, None, dtype=object)
    why = np.full(n, None, dtype=object)

    cover = np.asarray(ctx.landcover(det["latitude"].to_numpy(), det["longitude"].to_numpy()))
    near_generic: dict[int, float] = {}

    # 1. a mapped facility within one pixel: the most specific group wins
    if len(ctx.industry):
        pt, feat = ctx.tree.query(points, predicate="dwithin", distance=INDUSTRY_M)
        if len(pt):
            dist = shapely.distance(points[pt], ctx.industry["geometry"].to_numpy()[feat])
            generic = ctx.industry["group"].to_numpy()[feat] == "industrial_other"
            hard = np.isin(cover[pt], list(BUILT | BARE))
            ok = ~generic | (dist < 1) | hard
            for p, d in zip(pt[~ok], dist[~ok], strict=True):
                near_generic[int(p)] = min(float(d), near_generic.get(int(p), np.inf))
            hits = pd.DataFrame({"pt": pt[ok], "feat": feat[ok], "d": dist[ok],
                                 "rank": ctx.rank.to_numpy()[feat[ok]]})
            best = hits.sort_values(["pt", "rank", "d"]).drop_duplicates("pt")
            for p, f, d in zip(best["pt"], best["feat"], best["d"], strict=True):
                row = ctx.industry.iloc[f]
                name = f"'{row['name']}'" if isinstance(row["name"], str) and row["name"] \
                    else "a mapped"
                cls[p] = CLASS_OF_GROUP[row["group"]]
                where = "inside" if d < 1 else f"{d:.0f} m from"
                why[p] = (f"{where} {name} {GROUP_WORDS[row['group']]} ({row['evidence']}); "
                          "no thermal history here")

    todo = np.array([c is None for c in cls])

    # 2. bare ground near a mapped mine
    bare = todo & np.isin(cover, list(BARE))
    if bare.any() and len(ctx.mines):
        idx = np.flatnonzero(bare)
        pt, feat = ctx.mine_tree.query(points[idx], predicate="dwithin", distance=MINE_BARE_M)
        for p in np.unique(pt):
            i = idx[p]
            cls[i] = "mining"
            why[i] = ("bare ground within 2 km of a mapped mine: open-cast workings "
                      "and spoil heaps are bare")
    todo = np.array([c is None for c in cls])

    # 3. open sea
    sea = todo & (cover == 0) & ctx.zero_is_sea
    cls[sea] = "offshore"
    why[sea] = ("open sea beyond the land-cover tiles: an offshore platform flare, "
                "or a vessel")
    todo = np.array([c is None for c in cls])

    # 4. land cover
    for kinds, label, text in (
            (FOREST, "forest", "WorldCover tree cover"),
            (CROP, "agricultural", "WorldCover cropland"),
            (VEG, "vegetation", "WorldCover shrub, grass or wetland"),
            (BUILT, "unclassified", "built-up land with no mapped facility within 375 m: "
                                    "an unmapped plant, or an urban fire"),
            (BARE, "unclassified", "bare ground with no mapped mine nearby: a quarry, a "
                                   "dry riverbed or an unmapped mine"),
            (WATER, "unclassified", "on water at ~300 m: a riverbank, char-land or "
                                    "wetland fire, a vessel, or a coastal flare")):
        sel = np.flatnonzero(todo & np.isin(cover, list(kinds)))
        for i in sel:
            cls[i] = label
            why[i] = text + _season_note(label, int(months[i]))
            if i in near_generic:
                why[i] += (f"; {near_generic[i]:.0f} m from a mapped industrial area, "
                           "but not on it")
    rest = np.array([c is None for c in cls])
    cls[rest] = "unclassified"
    why[rest] = "no rule matched: land cover unavailable"

    out["pred_class"] = cls
    out["reason"] = why
    out["category"] = out["pred_class"].map(CATEGORY)
    return out
