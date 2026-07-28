-- Phase 2 RAG repair — versioned embedding storage. Lets the app switch
-- embedding providers/models WITHOUT overwriting the existing 768-dim
-- local embeddings in `knowledge_chunks.embedding` — old embeddings stay
-- available for instant rollback, and only one version is ever "active"
-- for production search at a time (an atomic pointer flip, not a mass
-- UPDATE, not an in-place overwrite). Idempotent — safe to re-run.
--
-- Design note on embedding storage type: `knowledge_chunk_embeddings.
-- embedding` is JSONB (a plain float array), NOT a fixed-width pgvector
-- column. pgvector requires a column's dimension to be fixed at table-
-- creation time, but this table is meant to hold whichever
-- provider/model wins the Part 1 evaluation (768, 1536, or 3072+
-- dimensions depending on model/config) — locking in a dimension now,
-- before that evaluation has run, would mean another migration the
-- moment a different model is chosen. Similarity for this table is
-- computed in the application layer (services/embedding_service.py /
-- rag/searcher.py), which is correct and fast enough at this knowledge
-- base's current scale (tens of chunks). Once a model is selected and
-- proven in production, a follow-up migration can add a real
-- dimension-specific pgvector column + index for that one version if/when
-- the corpus grows large enough for it to matter.

CREATE TABLE IF NOT EXISTS embedding_versions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version       TEXT NOT NULL UNIQUE,           -- e.g. "local-mpnet-v1", "openai-te3l-v1"
    provider      TEXT NOT NULL,                  -- "local" | "openai"
    model         TEXT NOT NULL,
    dimensions    INT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT false,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at  TIMESTAMPTZ
);

-- At most one active version — switching is a single atomic UPDATE
-- (clear old, set new), never a partial/mixed state.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_embedding_version
    ON embedding_versions(is_active) WHERE is_active;

-- Register the pre-existing local model as version 1 so the app always
-- has a valid "active version" row, even before any OpenAI migration
-- work runs. This version's actual vectors live in the pre-existing
-- knowledge_chunks.embedding column (768-dim pgvector) — untouched by
-- this migration — not in knowledge_chunk_embeddings below.
INSERT INTO embedding_versions (version, provider, model, dimensions, is_active, notes)
SELECT 'local-mpnet-v1', 'local', 'paraphrase-multilingual-mpnet-base-v2', 768, true,
       'Pre-existing embedding, stored directly in knowledge_chunks.embedding (768-dim pgvector column). This IS the rollback target.'
WHERE NOT EXISTS (SELECT 1 FROM embedding_versions WHERE version = 'local-mpnet-v1');

-- Versioned per-chunk embeddings for any NEW provider/model. A chunk can
-- have MANY rows here, one per embedding_version it's ever been embedded
-- with, but only rows belonging to the currently-active version are ever
-- searched (enforced in application code by always filtering on
-- embedding_version = the active row from embedding_versions).
CREATE TABLE IF NOT EXISTS knowledge_chunk_embeddings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id          UUID NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
    embedding_version TEXT NOT NULL REFERENCES embedding_versions(version),
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    dimensions        INT NOT NULL,
    embedding         JSONB NOT NULL,  -- float array; dimension validated at the app layer against `dimensions` above before insert
    embedding_text    TEXT,            -- exact contextual text that was embedded (Document/Section/Content — see services/embedding_service.py); never shown to end users as source text
    is_active         BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_chunk_embedding_version
    ON knowledge_chunk_embeddings(chunk_id, embedding_version);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embeddings_version_active
    ON knowledge_chunk_embeddings(embedding_version, is_active);

ALTER TABLE embedding_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunk_embeddings ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY service_role_all_embedding_versions ON embedding_versions FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE POLICY service_role_all_knowledge_chunk_embeddings ON knowledge_chunk_embeddings FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
