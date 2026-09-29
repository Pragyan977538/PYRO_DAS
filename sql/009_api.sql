-- FireWatch Stage 8: indexes the API's lookups need.
-- Idempotent, like every migration.

-- A source's detail page lists its events.
CREATE INDEX IF NOT EXISTS events_source ON events (source_id) WHERE source_id IS NOT NULL;
