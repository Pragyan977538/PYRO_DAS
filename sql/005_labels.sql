-- FireWatch Stage 4: weak labels for Model 1.
-- Idempotent, like every migration.

-- ===========================================================================
-- power_plants -- WRI Global Power Plant Database, India (CC BY 4.0).
-- A heavy_industry label source (combustion plants only), and map context.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS power_plants (
    gppd_id            TEXT PRIMARY KEY,
    name               TEXT,
    primary_fuel       TEXT NOT NULL,
    capacity_mw        REAL,
    commissioning_year REAL,
    owner              TEXT,
    longitude          DOUBLE PRECISION NOT NULL,
    latitude           DOUBLE PRECISION NOT NULL,
    geom               GEOMETRY(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS power_plants_geom_gist ON power_plants USING GIST (geom);

-- ===========================================================================
-- source_labels -- one weak label per registry source, with its evidence.
--
-- Built ONLY from location (OSM tags, the power plant database, WorldCover):
-- exactly the inputs Model 1 is forbidden to see. `label` is the Model 1 class,
-- or NULL when no honest label exists (unlabelled, or kiln -- dropped). The
-- fine `label_group` and every group within reach (`groups_near`) are kept so
-- the report can show where the labels came from and where they conflict.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS source_labels (
    source_id     INTEGER PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
    label         TEXT,
    label_group   TEXT,
    evidence      TEXT,
    evidence_name TEXT,
    distance_m    REAL,
    groups_near   JSONB NOT NULL DEFAULT '{}'::jsonb,
    landcover     SMALLINT,
    labelled_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_labels_label ON source_labels (label);
