-- FireWatch Stage 2: ingestion tables.
-- Idempotent, like every migration: safe to re-run on a loaded database.

-- Every date in this system is a UTC date: FIRMS sensor-days, the SP/NRT
-- dedupe and persistence all count UTC days. A server that inherited the
-- machine's zone (IST here) would make ::date silently disagree with them.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC');
END
$$;

-- ===========================================================================
-- ingest_log — what has been loaded, so a backfill can stop and resume.
-- One row per unit of work: an archive file, an API date chunk, a cloud tile.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS ingest_log (
    source     TEXT        NOT NULL,
    item       TEXT        NOT NULL,
    rows       INTEGER     NOT NULL,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, item)
);

-- ===========================================================================
-- vnf_raw — VIIRS Nightfire detections, staged before the join onto
-- detections. Optional: empty until the licence arrives (or in mock mode).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS vnf_raw (
    vnf_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scan_time  TIMESTAMPTZ      NOT NULL,
    latitude   DOUBLE PRECISION NOT NULL,
    longitude  DOUBLE PRECISION NOT NULL,
    geom       GEOMETRY(Point, 4326) NOT NULL,
    temp_k     REAL,
    area_m2    REAL,
    rh_mw      REAL,
    sat        TEXT,
    UNIQUE (scan_time, latitude, longitude)
);
CREATE INDEX IF NOT EXISTS vnf_raw_geom_gist ON vnf_raw USING GIST (geom);
CREATE INDEX IF NOT EXISTS vnf_raw_time ON vnf_raw (scan_time);

-- ===========================================================================
-- gihs_reference — Global Industrial Heat Sources (Ma et al. 2024), India.
-- EVALUATION ONLY: never a label, never a feature, so the registry's on-GIHS
-- share stays an independent number.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS gihs_reference (
    gihs_id     INTEGER PRIMARY KEY,
    confirmed   BOOLEAN NOT NULL,          -- GIHS Type 0: imagery-verified industrial
    active_2021 BOOLEAN NOT NULL,          -- detections in its 2021 column
    points_num  INTEGER,
    min_date    TEXT,
    max_date    TEXT,
    geom        GEOMETRY(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS gihs_reference_geom_gist ON gihs_reference USING GIST (geom);

-- ===========================================================================
-- osm_industrial, reshaped. Stage 0 declared MultiPolygon only, but flares and
-- wells are mapped as points, and one OSM id is unique only per element type.
-- Rebuilt only while the old shape is still in place, so a re-run keeps data.
-- label_group follows CLAUDE.md's class table (first match in priority order).
-- ===========================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'osm_industrial' AND column_name = 'osm_type') THEN
        DROP TABLE IF EXISTS osm_industrial;
        CREATE TABLE osm_industrial (
            osm_type    CHAR(1)  NOT NULL CHECK (osm_type IN ('n', 'w', 'r')),
            osm_id      BIGINT   NOT NULL,
            name        TEXT,
            label_group TEXT     NOT NULL,
            tags        JSONB    NOT NULL,
            geom        GEOMETRY(Geometry, 4326) NOT NULL,
            PRIMARY KEY (osm_type, osm_id)
        );
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS osm_industrial_geom_gist ON osm_industrial USING GIST (geom);
CREATE INDEX IF NOT EXISTS osm_industrial_group ON osm_industrial (label_group);

-- ===========================================================================
-- observability: daily sources. NASA POWER's cloud amount (CERES SYN1deg) is a
-- daily mean on a 1-degree grid, not a per-overpass value, so it is stored with
-- daynight = 'A' (all day). `source` says which grid cell_id belongs to.
-- ===========================================================================
ALTER TABLE observability DROP CONSTRAINT IF EXISTS observability_daynight_check;
ALTER TABLE observability ADD CONSTRAINT observability_daynight_check
    CHECK (daynight IN ('D', 'N', 'A'));
