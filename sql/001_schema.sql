-- FireWatch schema
-- Idempotent: safe to re-run.
--
-- TimescaleDB is optional. The docker image ships it, and there detections
-- becomes a hypertable; on a plain PostgreSQL + PostGIS install (the fallback
-- while Docker is unavailable) detections stays an ordinary table. At a few
-- million rows either is fine -- nothing downstream depends on the difference.

CREATE EXTENSION IF NOT EXISTS postgis;

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb unavailable (%): detections stays a plain table', SQLERRM;
END
$$;

-- ===========================================================================
-- detections — one row per satellite hot-pixel observation
--
-- bt4 / bt5 are the NORMALISED brightness temperatures. MODIS emits
-- brightness / bright_t31 and VIIRS emits bright_ti4 / bright_ti5; ingestion
-- maps both onto these two columns so downstream joins cannot silently drop
-- half the archive.
--
-- Provenance, set by firewatch/ingest/normalize.py:
--   instrument  VIIRS | MODIS -- baselines are keyed by instrument, not
--               satellite, because S-NPP delivery ends on 1 Nov 2026.
--   product     SP (archive, science-quality) | NRT (live). SP supersedes NRT
--               for any sensor-day both cover, so no pixel is counted twice.
--   firms_type  FIRMS' own inferred type (0 vegetation, 1 volcano, 2 static
--               land source, 3 offshore); archive only, NULL for NRT. It is
--               derived from recurrence, so it is an EVALUATION COMPARATOR ONLY:
--               never a model feature and never a training label.
--
-- vnf_* arrive from the optional VIIRS Nightfire join and are NULL for most
-- rows (night-only product, licence-gated, Planck-fit failures on cooler
-- sources). Never impute them.
--
-- road: A = 1 (no known source), B = 2 (normal), C = 3 (anomaly).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS detections (
    detection_id  BIGINT GENERATED ALWAYS AS IDENTITY,
    acq_datetime  TIMESTAMPTZ NOT NULL,
    latitude      DOUBLE PRECISION NOT NULL,
    longitude     DOUBLE PRECISION NOT NULL,
    geom          GEOMETRY(Point, 4326) NOT NULL,
    sensor        TEXT        NOT NULL,
    instrument    TEXT        NOT NULL CHECK (instrument IN ('VIIRS', 'MODIS')),
    satellite     TEXT,
    product       TEXT        NOT NULL CHECK (product IN ('SP', 'NRT')),
    version       TEXT,
    daynight      CHAR(1)     CHECK (daynight IN ('D', 'N')),
    frp           REAL,
    bt4           REAL,
    bt5           REAL,
    scan          REAL,
    track         REAL,
    confidence    TEXT,
    firms_type    SMALLINT    CHECK (firms_type IN (0, 1, 2, 3)),
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

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable(
            'detections', 'acq_datetime',
            chunk_time_interval => INTERVAL '30 days',
            if_not_exists => TRUE
        );
    END IF;
END
$$;

-- Natural key for idempotent backfill: the same pixel from the same sensor at
-- the same instant is the same observation, however many times we fetch it.
-- Keyed on the raw lat/lon doubles rather than geom, so equality is exact.
-- TimescaleDB additionally requires the partitioning column in any unique index.
-- It does NOT catch an SP/NRT pair for the same pixel -- reprocessing moves the
-- position slightly -- which is why SP supersedes NRT by sensor-day instead.
CREATE UNIQUE INDEX IF NOT EXISTS detections_natural_key
    ON detections (sensor, acq_datetime, latitude, longitude);

-- ===========================================================================
-- sources — the registry. One row per persistent thermal source.
--
-- Built from 375 m cells that pass a multi-year recurrence gate, never from
-- raw detections: raw clustering chains whole farm and forest landscapes into
-- single "sources".
--
-- fingerprint: the feature vector Model 1 classifies on (thermal + temporal
--              only; no location features, or the weak-supervision labels leak).
-- baselines:   nested by "instrument|daynight|season" -> {med, mad, p99, n}.
--              Per SOURCE, never per source type.
-- provisional: promoted from Road A. Keeps alerting until it looks like stable
--              infrastructure -- an accident must never become "normal".
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
-- Two jobs: it stops "no detection" being silently read as "no fire", and it
-- is the denominator for persistence (nights detected / nights observable).
--
-- FIRMS publishes detections only -- no swath footprints, no cloud masks -- so
-- this comes from ERA5 cloud cover at the overpass time, via Open-Meteo, per
-- 0.25 degree ERA5 grid cell. Expected clear nights = sum(1 - cloud_frac). It
-- is a reanalysis proxy, not a satellite measurement. Written for every
-- cell-date whether or not anything burned. obs_date is the UTC date, the
-- same convention as detections.acq_datetime.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS observability (
    cell_id     BIGINT   NOT NULL,
    obs_date    DATE     NOT NULL,
    daynight    CHAR(1)  NOT NULL CHECK (daynight IN ('D', 'N')),
    cloud_frac  REAL     NOT NULL CHECK (cloud_frac BETWEEN 0 AND 1),
    source      TEXT     NOT NULL DEFAULT 'era5',
    PRIMARY KEY (cell_id, obs_date, daynight)
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
--
-- fsi_alerts are FIRMS points inside forest boundaries, with feedback from
-- state forest departments on only some of them: weak forest labels, not
-- ground truth.
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
