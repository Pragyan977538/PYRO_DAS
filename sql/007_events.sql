-- PYRO_DAS Stage 6: event assembly.
-- Idempotent, like every migration.

-- ===========================================================================
-- events -- one row per real-world incident. Stage 0 declared the geometry and
-- lifecycle; these columns say what kind of incident it is and why.
--   kind      anomaly    Road C at a registry source
--             new_source a provisional source's whole incident
--             fire       Road A: a fire where nothing burns normally
--   category  industrial | forest | agricultural | other_natural | unclassified
--   alert     the strongest alert tier in it: confirmed > provisional, or
--             new_source; NULL for a Road A fire
--   reason    one sentence for the map
--   run_id    the inference run it was assembled from
-- ===========================================================================
ALTER TABLE events ADD COLUMN IF NOT EXISTS kind     TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS alert    TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS reason   TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS run_id   INTEGER;
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check
    CHECK (kind IN ('anomaly', 'new_source', 'fire'));
CREATE INDEX IF NOT EXISTS events_kind ON events (kind);
CREATE INDEX IF NOT EXISTS events_first_seen ON events (first_seen);
