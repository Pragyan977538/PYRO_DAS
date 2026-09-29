"""Stage 4: weak labels for every registry source.

    python scripts/build_labels.py

Labels come from OSM and the WRI power plant list within 1 km of a source's cells
(firewatch/models/labels.py). Sources with no industrial label are then checked
against WorldCover: if at least 30 sit on cropland or tree cover, the conditional
recurrent_biomass class is added. Writes ``source_labels`` and prints the census.
GIHS is used only to describe the result, never to label.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from firewatch.config import settings  # noqa: E402
from firewatch.db import fetch_all  # noqa: E402
from firewatch.ingest.landcover import landcover  # noqa: E402
from firewatch.models import labels as lb  # noqa: E402

OUT = REPO / "reports" / "stage4"
log = logging.getLogger("build_labels")


def on_gihs() -> set[int]:
    """Sources within 1 km of a confirmed GIHS object -- description only."""
    return {r["source_id"] for r in fetch_all("""
        SELECT DISTINCT s.source_id FROM sources s JOIN gihs_reference g
            ON g.confirmed AND g.geom && ST_Expand(s.geom, 0.015)
           AND ST_DWithin(s.geom::geography, g.geom::geography, 1000)""")}


def main() -> int:
    logging.basicConfig(level=settings().log_level, format=">> %(message)s")
    sources = pd.DataFrame(fetch_all("SELECT source_id, provisional FROM sources"))
    if sources.empty:
        print("the registry is empty: run `make registry` first")
        return 1
    near = lb.groups_near()
    labels = lb.assign(pd.Index(sources["source_id"]), near)

    free = labels.index[labels["label_group"].isna()]
    cells = pd.DataFrame(fetch_all(
        "SELECT source_id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM source_cells "
        "WHERE source_id = ANY(:ids)", {"ids": [int(s) for s in free]}))
    log.info("sampling WorldCover at %d cells of %d unlabelled sources", len(cells), len(free))
    cover = lb.landcover_of(cells, landcover) if len(cells) else pd.Series(dtype=int)
    candidates = lb.biomass_candidates(labels, cover)
    gihs = on_gihs()
    biomass = lb.biomass_decision(candidates, gihs)
    biomass["by_landcover"] = {
        lb.BIOMASS_LANDCOVER[int(lc)]: {
            "sources": int(len(ids)),
            "on_confirmed_industry": round(float(np.isin(ids, list(gihs)).mean()), 3)}
        for lc, ids in pd.Series(candidates, index=candidates).groupby(
            labels.loc[candidates, "landcover"].to_numpy()).groups.items()}
    free = labels["label_group"].isna()
    biomass["unlabelled_landcover"] = {str(k): int(v) for k, v in
                                       labels.loc[free, "landcover"].value_counts().items()}
    if biomass["class_added"]:
        lb.add_biomass(labels, candidates)
    lb.write(labels)

    labels["on_gihs"] = labels.index.isin(gihs)
    group = labels["label_group"].fillna("unlabelled")
    census = {
        "sources": int(len(labels)),
        "by_class": {str(k): int(v) for k, v in
                     labels["label"].fillna("none").value_counts().items()},
        "by_group": {g: {"sources": int((group == g).sum()),
                         "on_gihs": round(float(labels.loc[group == g, "on_gihs"].mean()), 3)
                         if (group == g).any() else None}
                     for g in [*lb.GROUP_ORDER, *lb.BIOMASS_LANDCOVER.values(), "unlabelled"]},
        "by_evidence": {k: int(v) for k, v in labels["evidence"].dropna().str.split(":").str[0]
                        .value_counts().items()},
        "near_any": {g: int(sum(g in d for d in labels["groups_near"]))
                     for g in lb.GROUP_ORDER},
        "contested": int(sum(len({lb.CLASS_OF.get(g) for g in d} - {None}) > 1
                             for d in labels["groups_near"])),
        "biomass": biomass,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "labels.json").write_text(json.dumps(census, indent=2), encoding="utf-8")
    print(json.dumps(census, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
