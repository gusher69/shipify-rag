-- ============================================================
-- Migration 019: Clean-reset migration to OpenAI text-embedding-3-large
-- (3072-dim), superseding 018_embedding_versions.sql.
--
-- Context: 018 introduced a versioned JSONB-based embedding table so
-- OLD 768-dim local embeddings could coexist with a not-yet-chosen new
-- model without ever overwriting them in place. That was never applied
-- to the real database, and the operator has since decided on a full
-- clean reset of all RAG test data (see tools/reset_knowledge_base.py)
-- rather than a side-by-side migration. With knowledge_chunks now
-- empty, the dual-storage complexity of 018 buys nothing here — a
-- direct ALTER to the final dimension is simpler and equally safe.
--
-- Run this ONLY after knowledge_chunks is empty (tools/reset_knowledge_base.py
-- --confirm). It will refuse to run destructively otherwise (see guard below).
-- Idempotent — safe to re-run.
-- ============================================================

DO $$
DECLARE
  remaining INT;
BEGIN
  SELECT count(*) INTO remaining FROM knowledge_chunks;
  IF remaining > 0 THEN
    RAISE EXCEPTION
      'knowledge_chunks has % row(s) — this migration changes the embedding '
      'column dimension and must only run against an EMPTY table (run '
      'tools/reset_knowledge_base.py --confirm first). Refusing to run.',
      remaining;
  END IF;
END $$;

DROP INDEX IF EXISTS knowledge_chunks_embedding_idx;

ALTER TABLE knowledge_chunks
  ALTER COLUMN embedding TYPE VECTOR(3072)
  USING embedding::text::vector(3072);

-- ivfflat has a 2000-dim limit in older pgvector builds; 3072-dim vectors
-- require pgvector >= 0.7 (HNSW) or no ANN index at all on very small
-- corpora. Given the corpus size here (tens of chunks), an ANN index adds
-- no benefit yet — a plain sequential scan on <=> is exact and fast at
-- this scale, so no index is created. Add one back (ivfflat or hnsw)
-- once the corpus is large enough to need it.

DROP FUNCTION IF EXISTS match_knowledge_chunks(vector, integer);

CREATE OR REPLACE FUNCTION match_knowledge_chunks(
  query_embedding VECTOR(3072),
  match_count     INT DEFAULT 5
)
RETURNS TABLE (
  id            UUID,
  file_id       UUID,
  content       TEXT,
  source        TEXT,
  intent        TEXT,
  is_active     BOOLEAN,
  metadata      JSONB,
  chunk_index   INT,
  page_number   INT,
  section_title TEXT,
  version       INT,
  similarity    FLOAT
)
LANGUAGE sql STABLE
AS $$
  SELECT
    kc.id,
    kc.file_id,
    kc.content,
    kc.source,
    kc.intent,
    kc.is_active,
    kc.metadata,
    kc.chunk_index,
    kc.page_number,
    kc.section_title,
    kc.version,
    1 - (kc.embedding <=> query_embedding) AS similarity
  FROM knowledge_chunks kc
  LEFT JOIN knowledge_files kf ON kf.id = kc.file_id
  WHERE
    kc.is_active = true
    AND (kc.file_id IS NULL OR (
      kf.is_active  = true
      AND kf.is_deleted = false
      AND kf.deleted_at IS NULL
    ))
  ORDER BY kc.embedding <=> query_embedding
  LIMIT match_count;
$$;
