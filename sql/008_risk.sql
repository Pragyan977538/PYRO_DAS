-- FireWatch Stage 7: risk scoring.
-- Idempotent, like every migration.

-- ===========================================================================
-- critical_assets -- what a fire could hit. Still static: seeded from maps and
-- registries (OSM, WRI power plants), never from thermal history, so a nuclear
-- plant that is thermally invisible still counts at full criticality.
-- footprint keeps the mapped outline, since distance to a refinery is distance
-- to its fence, not its centre; geom stays a point for the map.
-- source_ref ('osm:w123', 'gppd:IND0000001', 'manual:...') makes reseeding
-- idempotent and lets hand-added entries coexist with the generated ones.
-- ===========================================================================
ALTER TABLE critical_assets ADD COLUMN IF NOT EXISTS footprint GEOMETRY(Geometry, 4326);
CREATE UNIQUE INDEX IF NOT EXISTS critical_assets_source_ref ON critical_assets (source_ref);
CREATE INDEX IF NOT EXISTS critical_assets_footprint_gist ON critical_assets
    USING GIST (footprint);

-- ===========================================================================
-- wind_daily -- NASA POWER daily mean 10 m wind components (MERRA-2 grid,
-- 0.5 x 0.625 degrees), for the downwind exposure term. Components, not a
-- direction: a day's mean direction is meaningless, its mean vector is not.
-- u10m > 0 blows toward the east, v10m > 0 toward the north.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS wind_daily (
    latitude   REAL NOT NULL,
    longitude  REAL NOT NULL,
    obs_date   DATE NOT NULL,
    u10m       REAL,
    v10m       REAL,
    PRIMARY KEY (obs_date, latitude, longitude)
);
