-- 001: triage notes, the work queue's leases, the LLM spend ledger and the
-- runbook index. Everything lives in schema "triage": this service never
-- writes another service's tables, and reads cases only through their API.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS triage;

-- One note per case: the latest triage. input_hash covers what the note was
-- made from (prompt version, model, the bundles); a case is triaged again
-- only when that changes, so new alerts on a case don't each cost a call.
CREATE TABLE IF NOT EXISTS triage.notes (
    case_id          BIGINT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN ('done', 'failed', 'skipped')),
    input_hash       TEXT NOT NULL,
    case_last_alert  TIMESTAMPTZ,
    bundle_ids       TEXT[] NOT NULL DEFAULT '{}',
    note             JSONB,
    checks           JSONB NOT NULL DEFAULT '{}',
    retrieved        TEXT[] NOT NULL DEFAULT '{}',
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    input_tokens     INT NOT NULL DEFAULT 0,
    output_tokens    INT NOT NULL DEFAULT 0,
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms       INT NOT NULL DEFAULT 0,
    attempts         INT NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    next_attempt_at  TIMESTAMPTZ,
    trace_id         TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notes_updated ON triage.notes (updated_at DESC);

-- Leases: a replica works on a case only while it holds an unexpired lease,
-- so two replicas never triage (and pay for) the same case at once. A crashed
-- replica's lease simply runs out.
CREATE TABLE IF NOT EXISTS triage.leases (
    case_id     BIGINT PRIMARY KEY,
    owner       TEXT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL
);

-- Every LLM request, successful or not. The daily budget is a sum over this
-- table, so it holds across replicas and restarts.
CREATE TABLE IF NOT EXISTS triage.llm_calls (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    case_id       BIGINT,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms    INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_calls_at ON triage.llm_calls (at);

-- Runbook chunks with an embedding (vector search) and a tsvector (keyword
-- search). The corpus is a few dozen chunks, so a sequential scan beats any
-- index; the vector column has no fixed dimension so the embedding model
-- can change without a migration.
CREATE TABLE IF NOT EXISTS triage.runbook_chunks (
    chunk_id     TEXT PRIMARY KEY,
    runbook_id   TEXT NOT NULL,
    title        TEXT NOT NULL,
    section      TEXT NOT NULL,
    content      TEXT NOT NULL,
    alert_codes  TEXT[] NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    embedder     TEXT NOT NULL,
    embedding    vector NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || section || ' ' || content)) STORED,
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runbook_chunks_tsv ON triage.runbook_chunks USING gin (tsv);
