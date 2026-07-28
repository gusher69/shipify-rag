-- ============================================================
-- Migration 001: Full metadata + is_active + indexes + RPC
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
-- ============================================================

-- ────────────────────────────────────────────────────────────
-- STEP 0: Fix embedding dimension mismatch
--   Original setup used VECTOR(1536) but the actual model
--   paraphrase-multilingual-mpnet-base-v2 outputs 768 dims.
--   We must change the column type before altering the index.
-- ────────────────────────────────────────────────────────────

-- Drop the old IVFFlat index first (it's tied to the column type)
DROP INDEX IF EXISTS knowledge_chunks_embedding_idx;

-- Change embedding column from VECTOR(1536) → VECTOR(768)
-- NOTE: This will fail if existing rows have 1536-dim vectors.
-- If you get a dimension error, run this first to clear old data:
--   DELETE FROM knowledge_chunks;
-- Then re-run this migration.
ALTER TABLE knowledge_chunks
  ALTER COLUMN embedding TYPE VECTOR(768)
  USING embedding::text::vector(768);

-- Recreate IVFFlat index for 768-dim cosine search
-- (lists=100 requires at least 100 rows; drop to lists=10 for small datasets)
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
  ON knowledge_chunks
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 10);


-- ────────────────────────────────────────────────────────────
-- STEP 1: knowledge_chunks — add new columns
-- ────────────────────────────────────────────────────────────

ALTER TABLE knowledge_chunks
  ADD COLUMN IF NOT EXISTS file_id       UUID,
  ADD COLUMN IF NOT EXISTS metadata      JSONB       DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS is_active     BOOLEAN     DEFAULT true,
  ADD COLUMN IF NOT EXISTS chunk_index   INT,
  ADD COLUMN IF NOT EXISTS page_number   INT,
  ADD COLUMN IF NOT EXISTS section_title TEXT,
  ADD COLUMN IF NOT EXISTS token_count   INT,
  ADD COLUMN IF NOT EXISTS content_hash  TEXT,
  ADD COLUMN IF NOT EXISTS version       INT         DEFAULT 1,
  ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ DEFAULT now();

-- Backfill: mark all pre-existing rows as active
UPDATE knowledge_chunks SET is_active = true WHERE is_active IS NULL;


-- ────────────────────────────────────────────────────────────
-- STEP 2: knowledge_files — create table if missing, add columns
-- ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS knowledge_files (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  filename      TEXT        NOT NULL,
  size          BIGINT,
  chunk_count   INT,
  uploaded_at   TIMESTAMPTZ DEFAULT now(),
  synced_at     TIMESTAMPTZ,
  deleted_at    TIMESTAMPTZ
);

ALTER TABLE knowledge_files
  ADD COLUMN IF NOT EXISTS original_filename TEXT,
  ADD COLUMN IF NOT EXISTS file_type         TEXT,
  ADD COLUMN IF NOT EXISTS storage_bucket    TEXT DEFAULT 'knowledge-base',
  ADD COLUMN IF NOT EXISTS storage_path      TEXT,
  ADD COLUMN IF NOT EXISTS document_title    TEXT,
  ADD COLUMN IF NOT EXISTS language          TEXT DEFAULT 'th',
  ADD COLUMN IF NOT EXISTS version           INT  DEFAULT 1,
  ADD COLUMN IF NOT EXISTS is_active         BOOLEAN     DEFAULT true,
  ADD COLUMN IF NOT EXISTS is_deleted        BOOLEAN     DEFAULT false,
  ADD COLUMN IF NOT EXISTS file_hash         TEXT,
  ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS uploaded_by       TEXT,
  -- from previous migration (safe to re-run ADD COLUMN IF NOT EXISTS)
  ADD COLUMN IF NOT EXISTS scope             TEXT DEFAULT 'Other',
  ADD COLUMN IF NOT EXISTS platform          TEXT DEFAULT 'Internal',
  ADD COLUMN IF NOT EXISTS category          TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS tags              TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS description       TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS status            TEXT DEFAULT 'uploaded',
  ADD COLUMN IF NOT EXISTS last_error        TEXT DEFAULT '';

-- Backfill derived columns from existing data
UPDATE knowledge_files
SET
  original_filename = COALESCE(original_filename, filename),
  file_type         = COALESCE(file_type, lower(regexp_replace(filename, '^.*\.', ''))),
  storage_path      = COALESCE(storage_path, 'knowledge/' || filename),
  is_active         = COALESCE(is_active, true),
  is_deleted        = COALESCE(is_deleted, CASE WHEN deleted_at IS NOT NULL THEN true ELSE false END);


-- ────────────────────────────────────────────────────────────
-- STEP 3: Indexes
-- ────────────────────────────────────────────────────────────

-- knowledge_chunks
CREATE INDEX IF NOT EXISTS idx_kc_file_id
  ON knowledge_chunks (file_id);

CREATE INDEX IF NOT EXISTS idx_kc_is_active
  ON knowledge_chunks (is_active);

CREATE INDEX IF NOT EXISTS idx_kc_metadata_gin
  ON knowledge_chunks USING gin (metadata);

CREATE INDEX IF NOT EXISTS idx_kc_content_hash
  ON knowledge_chunks (content_hash);

CREATE INDEX IF NOT EXISTS idx_kc_version
  ON knowledge_chunks (version);

-- knowledge_files
CREATE INDEX IF NOT EXISTS idx_kf_is_active
  ON knowledge_files (is_active);

CREATE INDEX IF NOT EXISTS idx_kf_is_deleted
  ON knowledge_files (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kf_file_hash
  ON knowledge_files (file_hash);


-- ────────────────────────────────────────────────────────────
-- STEP 4: updated_at auto-trigger
-- ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_kc_updated_at ON knowledge_chunks;
CREATE TRIGGER trg_kc_updated_at
  BEFORE UPDATE ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_kf_updated_at ON knowledge_files;
CREATE TRIGGER trg_kf_updated_at
  BEFORE UPDATE ON knowledge_files
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────────────────────────
-- STEP 5: RPC match_knowledge_chunks — updated
--   • Filters knowledge_chunks.is_active = true
--   • Joins knowledge_files to exclude inactive / deleted files
--   • Returns citation metadata from jsonb
--   • Uses correct VECTOR(768) dimension
-- ────────────────────────────────────────────────────────────

DROP FUNCTION IF EXISTS match_knowledge_chunks(vector, integer);

CREATE OR REPLACE FUNCTION match_knowledge_chunks(
  query_embedding VECTOR(768),
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
  -- Only join files table if file_id is set; orphan chunks still searchable
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


-- ────────────────────────────────────────────────────────────
-- STEP 6: RLS — allow service_role full access on knowledge_files
-- ────────────────────────────────────────────────────────────

ALTER TABLE knowledge_files ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_knowledge_files" ON knowledge_files;
CREATE POLICY "service_role_all_knowledge_files" ON knowledge_files
  FOR ALL USING (auth.role() = 'service_role');

-- Re-assert chunks policy (safe to re-run)
DROP POLICY IF EXISTS "service_role_all_knowledge" ON knowledge_chunks;
CREATE POLICY "service_role_all_knowledge" ON knowledge_chunks
  FOR ALL USING (auth.role() = 'service_role');
