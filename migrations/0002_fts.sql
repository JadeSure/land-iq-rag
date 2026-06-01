-- Full-text search support: add a generated tsvector column to chunk.
-- Postgres automatically keeps it in sync on INSERT/UPDATE — no trigger needed.
-- GIN index makes FTS queries fast even at large data volumes.

ALTER TABLE chunk
    ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_chunk_fts ON chunk USING GIN(fts);
