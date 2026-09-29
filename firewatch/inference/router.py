"""Is there a known source within 500 m?

Asked of a source's *cells*, not its centre: a coalfield or a steel works is
kilometres wide, and a fire at its edge is still at that source. It is the same
rule the registry used to assign history (``registry.cluster.assign``), so a
detection routed live lands where the same detection would have landed in the
archive. VIIRS gets 500 m; MODIS, whose pixels are 1 km at nadir, gets 1 km.

The router holds registry sources and live provisional sources in memory: a few
thousand cells in a KD-tree, rebuilt when promotion adds a source.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from firewatch.registry.cluster import ASSIGN_M, assign


class Router:
    def __init__(self, cells: pd.DataFrame, provisional: set[int] | None = None) -> None:
        """``cells``: ``source_id``, ``x``, ``y`` (EPSG:7755 metres), one row per cell."""
        self.cells = cells[["source_id", "x", "y"]].reset_index(drop=True)
        self.provisional = set(provisional or ())

    def route(self, x: np.ndarray, y: np.ndarray, instrument: np.ndarray) -> np.ndarray:
        """Source id per detection, or -1 (Road A)."""
        out = np.full(len(x), -1, dtype=np.int64)
        instrument = np.asarray(instrument)
        for inst, radius in ASSIGN_M.items():
            sel = instrument == inst
            if sel.any():
                out[sel] = assign(np.asarray(x)[sel], np.asarray(y)[sel], self.cells, radius,
                                  column="source_id")
        return out

    def add(self, source_id: int, cells: pd.DataFrame, provisional: bool = True) -> None:
        new = pd.DataFrame({"source_id": source_id, "x": cells["x"].to_numpy(),
                            "y": cells["y"].to_numpy()})
        self.cells = pd.concat([self.cells, new], ignore_index=True)
        if provisional:
            self.provisional.add(int(source_id))

    def remove(self, source_id: int) -> None:
        self.cells = self.cells[self.cells["source_id"] != source_id].reset_index(drop=True)
        self.provisional.discard(int(source_id))

    @classmethod
    def from_db(cls, as_of: pd.Timestamp | None = None) -> Router:
        """Registry sources, plus provisional sources live at ``as_of``: promoted
        before it and not retired by it. Promotions after it belong to the future
        of a replay and are left out."""
        from firewatch.db import fetch_all

        rows = fetch_all("""
            SELECT c.source_id, c.x_m AS x, c.y_m AS y, s.provisional
              FROM source_cells c JOIN sources s USING (source_id)
             WHERE NOT s.provisional
                OR (s.promoted_at < :t AND (s.retired_at IS NULL OR s.retired_at > :t))
                OR (s.promoted_at IS NULL AND s.retired_at IS NULL)""",
                         {"t": (as_of or pd.Timestamp.now(tz="UTC")).to_pydatetime()})
        cells = pd.DataFrame(rows, columns=["source_id", "x", "y", "provisional"])
        return cls(cells, set(cells.loc[cells["provisional"], "source_id"].astype(int)))
