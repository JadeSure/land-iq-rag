-- LandIQ RAG core schema (model-agnostic tables).
-- Per-model embedding tables are created at runtime by ensure_embedding_table()
-- so that switching the active model (F8) needs no migration edit.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Address: the partition + access-control unit. Cross-address leakage is a
-- correctness defect (PRD 8.1), enforced by a mandatory filter in the query layer.
CREATE TABLE IF NOT EXISTS address (
    address_id   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Document: one logical document per (address_id, upload_id). content_hash gates
-- the unchanged-skip path (F10); storage_ref recovers the original file (5.2).
CREATE TABLE IF NOT EXISTS document (
    document_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    address_id    TEXT NOT NULL REFERENCES address(address_id) ON DELETE CASCADE,
    upload_id     TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    storage_ref   TEXT NOT NULL,
    original_name TEXT,
    live_version  INT  NOT NULL DEFAULT 0,   -- atomic-switch pointer (0 = not yet live)
    ingest_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (address_id, upload_id)
);
CREATE INDEX IF NOT EXISTS idx_document_address ON document(address_id);

-- Chunk: carries provenance (page/paragraph/char span) sufficient for human
-- verification (F7). doc_version + ordinal make re-ingest duplicate-proof.
CREATE TABLE IF NOT EXISTS chunk (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    address_id      TEXT NOT NULL REFERENCES address(address_id) ON DELETE CASCADE,
    doc_version     INT  NOT NULL,
    ordinal         INT  NOT NULL,
    page_number     INT,
    paragraph_index INT,
    char_start      INT,
    char_end        INT,
    text            TEXT NOT NULL,
    token_count     INT,
    UNIQUE (document_id, doc_version, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunk_address  ON chunk(address_id);
CREATE INDEX IF NOT EXISTS idx_chunk_document ON chunk(document_id);
CREATE INDEX IF NOT EXISTS idx_chunk_doc_ver  ON chunk(document_id, doc_version);

-- Ingestion job: the F12 status record AND the local queue (claimed via SKIP LOCKED).
DO $$ BEGIN
    CREATE TYPE ingest_state AS ENUM
        ('pending','extracting','chunking','embedding','done','skipped','failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS ingest_job (
    task_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID REFERENCES document(document_id) ON DELETE CASCADE,
    address_id      TEXT NOT NULL,
    upload_id       TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    doc_version     INT  NOT NULL,
    job_type        TEXT NOT NULL DEFAULT 'ingest',  -- 'ingest' | 'rebuild'
    state           ingest_state NOT NULL DEFAULT 'pending',
    chunk_count     INT DEFAULT 0,
    embedding_count INT DEFAULT 0,
    attempts        INT DEFAULT 0,
    max_attempts    INT DEFAULT 5,
    failed_step     TEXT,
    error_type      TEXT,
    error_message   TEXT,
    tokens_embedded BIGINT DEFAULT 0,
    cost_usd        NUMERIC(12,6) DEFAULT 0,
    worker_id       TEXT,
    claimed_at      TIMESTAMPTZ,
    lease_until     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_job_claimable ON ingest_job(created_at)
    WHERE state IN ('pending','extracting','chunking','embedding');
CREATE INDEX IF NOT EXISTS idx_job_address ON ingest_job(address_id);
CREATE INDEX IF NOT EXISTS idx_job_document ON ingest_job(document_id);

-- Runtime config (active model + chunk params); admin-editable (F8).
CREATE TABLE IF NOT EXISTS rag_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Registry of per-model embedding tables that have been created.
CREATE TABLE IF NOT EXISTS embedding_model (
    model_id   TEXT PRIMARY KEY,
    dim        INT  NOT NULL,
    table_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-query observability (8.3).
CREATE TABLE IF NOT EXISTS query_log (
    query_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    address_id          TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    latency_ms          INT,
    candidates_examined INT,
    chunks_returned     INT,
    cost_usd            NUMERIC(12,6),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
