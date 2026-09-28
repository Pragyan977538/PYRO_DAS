-- FireWatch schema (Stage 0)
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===========================================================================
-- detections — one row per satellite hot-pixel observation
--
-- bt4 / bt5 are the NORMALISED brightness temperatures. MODIS emits
-- brightness / bright_t31 and VIIRS emits bright_ti4 / bright_ti5; ingestion
-- maps both onto these two columns so downstream joins cannot silently drop
-- half the archive.
--
-- vnf_* arrive from a separate VIIRS Nightfire join and are NULL for roughly
-- 55% of rows (night-only product, plus Planck-fit failures on cooler
-- sources). Never impute them.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS detections (
    detection_id  BIGINT GENERATED ALWAYS AS IDENTITY,
    acq_datetime  TIMESTAMPTZ NOT NULL,
    latitude      DOUBLE PRECISION NOT NULL,
    longitude     DOUBLE PRECISION NOT NULL,
    geom          GEOMETRY(Point, 4326) NOT NULL,
    sensor        TEXT        NOT NULL,
    satellite     TEXT,
    daynight      CHAR(1),
    frp           REAL,
    bt4           REAL,
    bt5           REAL,
    scan          REAL,
    track         REAL,
    confidence    TEXT,
    vnf_temp_k    REAL,
    vnf_area_m2   REAL,
    vnf_rh_mw     REAL,
    source_id     INTEGER,
    event_id      INTEGER,
    road          SMALLINT CHECK (road IN (1, 2, 3)),
    pred_class    TEXT,
    pred_conf     REAL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (detection_id, acq_datetime)
);

SELECT create_hypertable(
    'detections', 'acq_datetime',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- Natural key for idempotent backfill: the same pixel from the same sensor at
-- the same instant is the same observation, however many times we fetch it.
-- Keyed on the raw lat/lon doubles rather than geom -- geometry equality under
-- a btree opclass is bounding-box based and would collapse distinct pixels.
-- TimescaleDB additionally requires the partitioning column in any unique index.
CREATE UNIQUE INDEX IF NOT EXISTS detections_natural_key
    ON detections (sensor, acq_datetime, latitude, longitude);

-- ===========================================================================
-- sources — the registry. One row per persistent thermal source.
--
-- fingerprint: the feature vector Model 1 classifies on (thermal + temporal
--              only; no location features, or the weak-supervision labels leak).
-- baselines:   nested by "sensor|daynight|season" -> {med, mad, p99, n}.
--              Per SOURCE, never per source type.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS sources (
    source_id     SERIAL PRIMARY KEY,
    geom          GEOMETRY(Point, 4326) NOT NULL,
    footprint     GEOMETRY(Polygon, 4326),
    state_code    TEXT,
    n_detections  INTEGER NOT NULL DEFAULT 0,
    first_seen    DATE,
    last_seen     DATE,
    cls           TEXT,
    cls_conf      REAL,
    label_source  TEXT,
    osm_ref       BIGINT,
    osm_name      TEXT,
    fingerprint   JSONB,
    baselines     JSONB,
    provisional   BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- events — one row per real-world incident, not per detection.
-- source_id is NULL for Road A events (no prior history at that location).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS events (
    event_id       SERIAL PRIMARY KEY,
    source_id      INTEGER REFERENCES sources(source_id) ON DELETE SET NULL,
    centroid       GEOMETRY(Point, 4326) NOT NULL,
    footprint      GEOMETRY(Polygon, 4326),
    first_seen     TIMESTAMPTZ NOT NULL,
    last_seen      TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'dormant', 'closed')),
    event_class    TEXT,
    peak_frp       REAL,
    n_detections   INTEGER NOT NULL DEFAULT 0,
    risk_score     REAL,
    risk_breakdown JSONB,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- observability — were we even looking?
--
-- Written on EVERY pass, whether or not anything burned. Two jobs: it stops
-- "no detection" being silently read as "no fire", and it is the denominator
-- for the persistence feature. Annual observability runs ~64%, dropping to
-- ~29% in the monsoon; using calendar nights instead understates every source
-- by about a third.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS observability (
    cell_id      BIGINT      NOT NULL,
    obs_date     DATE        NOT NULL,
    sensor       TEXT        NOT NULL,
    clear_looks  SMALLINT    NOT NULL DEFAULT 0,
    total_passes SMALLINT    NOT NULL DEFAULT 0,
    PRIMARY KEY (cell_id, obs_date, sensor)
);

-- ===========================================================================
-- critical_assets — STATIC and hand-compiled, never derived from thermal data.
--
-- Nuclear plants dump waste heat into cooling water at 30-40 C, far below
-- satellite detection; ammunition depots and LPG bottling plants are likewise
-- thermally invisible in normal operation. Deriving criticality from the
-- thermal archive would score exactly the highest-consequence assets at zero.
-- Seeded from PESO, CEA and MoPNG listings.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS critical_assets (
    asset_id    SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    geom        GEOMETRY(Point, 4326) NOT NULL,
    asset_type  TEXT NOT NULL,
    criticality REAL NOT NULL CHECK (criticality BETWEEN 0 AND 1),
    operator    TEXT,
    state_code  TEXT,
    source_ref  TEXT
);

-- ===========================================================================
-- Context layers
-- ===========================================================================
CREATE TABLE IF NOT EXISTS osm_industrial (
    osm_id BIGINT PRIMARY KEY,
    name   TEXT,
    tag    TEXT NOT NULL,
    geom   GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS forest_boundary (
    id     SERIAL PRIMARY KEY,
    name   TEXT,
    geom   GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS fsi_alerts (
    alert_id     BIGSERIAL PRIMARY KEY,
    geom         GEOMETRY(Point, 4326) NOT NULL,
    alert_date   DATE NOT NULL,
    state_code   TEXT,
    UNIQUE (geom, alert_date)
);
