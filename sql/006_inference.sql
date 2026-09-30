-- PYRO_DAS Stage 5: routing, Road A reasons, Road C alerts, promotion.
-- Idempotent, like every migration. Adding nullable columns is a metadata-only
-- change, so it is instant even on the full detections table.

-- ===========================================================================
-- detections: what the router decided, and why.
--   category  industrial | forest | agricultural | other_natural | unclassified
--   reason    the rule or the baseline comparison, in words, for the map
--   alert     provisional | confirmed (Road C at a registry source), or
--             new_source (any detection at a provisional source)
-- ===========================================================================
ALTER TABLE detections ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS reason   TEXT;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS alert    TEXT;
ALTER TABLE detections DROP CONSTRAINT IF EXISTS detections_alert_check;
ALTER TABLE detections ADD CONSTRAINT detections_alert_check
    CHECK (alert IN ('provisional', 'confirmed', 'new_source'));

-- ===========================================================================
-- sources: provisional sources come from promotion, not from the registry.
--   origin        registry | promotion
--   promoted_at   when a Road A site became a provisional source
--   retired_at    when it went quiet (90 days) and left the router
--   confirmed_at  when an analyst confirmed it as infrastructure -- the only
--                 way `provisional` is ever cleared
-- ===========================================================================
ALTER TABLE sources ADD COLUMN IF NOT EXISTS origin       TEXT NOT NULL DEFAULT 'registry';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS promoted_at  TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS retired_at   TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

-- ===========================================================================
-- source_passes -- every scored pass at a registry source: the unit of Road C.
-- frp is the pass's hottest pixel; baseline_key says which bucket judged it.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS source_passes (
    source_id     INTEGER     NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    sensor        TEXT        NOT NULL,
    pass_time     TIMESTAMPTZ NOT NULL,
    instrument    TEXT        NOT NULL,
    daynight      CHAR(1),
    n_det         INTEGER     NOT NULL,
    frp           REAL        NOT NULL,
    frp_sum       REAL        NOT NULL,
    baseline_key  TEXT,
    med           REAL,
    p99           REAL,
    z             REAL,
    breach        BOOLEAN     NOT NULL,
    extreme       BOOLEAN     NOT NULL,
    alert         TEXT CHECK (alert IN ('provisional', 'confirmed')),
    PRIMARY KEY (source_id, sensor, pass_time)
);
CREATE INDEX IF NOT EXISTS source_passes_time ON source_passes (pass_time);
CREATE INDEX IF NOT EXISTS source_passes_alert ON source_passes (alert) WHERE alert IS NOT NULL;

-- ===========================================================================
-- inference_runs -- one row per run or replay: window, thresholds, counts.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS inference_runs (
    run_id        SERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    thresholds    JSONB       NOT NULL,
    stats         JSONB       NOT NULL
);
