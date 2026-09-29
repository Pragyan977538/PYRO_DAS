-- FireWatch Stage 3: the registry's cells and its build history.
-- Idempotent, like every migration: safe to re-run on a built registry.

-- ===========================================================================
-- source_cells -- the 375 m cells that make up each source.
--
-- The router asks "is there a known source within 500 m?" of a source's cells,
-- not of its centre: a coalfield or a steel works is kilometres wide, and a fire
-- at its edge is still at that source. geom is the cell's mean detection
-- position (EPSG:4326); x_m / y_m are the same point in EPSG:7755 metres.
--
-- Keyed (source_id, cell_id) rather than cell_id alone: a provisional source
-- (Stage 5) may overlap cells a rebuild later registers.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS source_cells (
    source_id    INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    cell_id      BIGINT  NOT NULL,
    n_det        INTEGER NOT NULL,
    years_passed SMALLINT,
    x_m          DOUBLE PRECISION NOT NULL,
    y_m          DOUBLE PRECISION NOT NULL,
    geom         GEOMETRY(Point, 4326) NOT NULL,
    PRIMARY KEY (source_id, cell_id)
);
CREATE INDEX IF NOT EXISTS source_cells_geom_gist ON source_cells USING GIST (geom);
CREATE INDEX IF NOT EXISTS source_cells_cell ON source_cells (cell_id);

-- ===========================================================================
-- registry_runs -- one row per build: the gate used and what came out. The map
-- and the API name the run a registry came from.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS registry_runs (
    run_id    SERIAL PRIMARY KEY,
    built_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    gate      TEXT        NOT NULL,
    stats     JSONB       NOT NULL
);
