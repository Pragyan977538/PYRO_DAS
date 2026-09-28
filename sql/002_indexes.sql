-- FireWatch indexes (Stage 0)
-- Split from the schema so indexes can be dropped and rebuilt after a bulk
-- backfill without touching table definitions. Idempotent.

-- ---------------------------------------------------------------------------
-- Spatial. GiST on every geometry column.
--
-- sources_geom_gist carries the hottest query in the system: the router runs
-- "nearest source within 500 m" against it on every incoming detection, so it
-- must stay an index scan. Without it the router degrades to a sequential scan
-- per detection and inference stops being real-time.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS detections_geom_gist      ON detections     USING GIST (geom);
CREATE INDEX IF NOT EXISTS sources_geom_gist         ON sources        USING GIST (geom);
CREATE INDEX IF NOT EXISTS sources_footprint_gist    ON sources        USING GIST (footprint);
CREATE INDEX IF NOT EXISTS events_centroid_gist      ON events         USING GIST (centroid);
CREATE INDEX IF NOT EXISTS events_footprint_gist     ON events         USING GIST (footprint);
CREATE INDEX IF NOT EXISTS assets_geom_gist          ON critical_assets USING GIST (geom);
CREATE INDEX IF NOT EXISTS osm_industrial_geom_gist  ON osm_industrial USING GIST (geom);
CREATE INDEX IF NOT EXISTS forest_boundary_geom_gist ON forest_boundary USING GIST (geom);
CREATE INDEX IF NOT EXISTS fsi_alerts_geom_gist      ON fsi_alerts     USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Foreign-key and filter lookups
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS detections_source_id  ON detections (source_id)
    WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS detections_event_id   ON detections (event_id)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS detections_road       ON detections (road)
    WHERE road IS NOT NULL;
-- Baselines are keyed by instrument, not satellite.
CREATE INDEX IF NOT EXISTS detections_instrument_dn ON detections (instrument, daynight);
-- supersede_nrt deletes NRT rows by sensor and time range after every SP load.
CREATE INDEX IF NOT EXISTS detections_product_sensor_time
    ON detections (product, sensor, acq_datetime);

-- A hypertable indexes its time column automatically; a plain table does not.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        CREATE INDEX IF NOT EXISTS detections_acq_datetime
            ON detections (acq_datetime DESC);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS sources_cls           ON sources (cls);
CREATE INDEX IF NOT EXISTS sources_state         ON sources (state_code);
CREATE INDEX IF NOT EXISTS sources_provisional   ON sources (provisional)
    WHERE provisional;

CREATE INDEX IF NOT EXISTS events_status         ON events (status);
CREATE INDEX IF NOT EXISTS events_risk           ON events (risk_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS events_last_seen      ON events (last_seen DESC);

CREATE INDEX IF NOT EXISTS assets_type          ON critical_assets (asset_type);
CREATE INDEX IF NOT EXISTS osm_industrial_tag   ON osm_industrial (tag);
CREATE INDEX IF NOT EXISTS fsi_alerts_date      ON fsi_alerts (alert_date);
