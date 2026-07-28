-- ============================================================
-- Migration 011: Storage abstraction metadata on knowledge_files
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- knowledge_files previously only recorded `filename` and `size`, with
-- every read/write assuming "the file is on local disk under
-- KNOWLEDGE_DIR." These columns let a file be stored under ANY
-- StorageService provider (local/Supabase Storage/Google Drive/S3/MinIO)
-- and record exactly where/how, so switching STORAGE_PROVIDER later never
-- requires touching this table's shape again — only the values change.
--
-- knowledge_attachments already has storage_provider/storage_path/
-- public_url/checksum (migrations 004, 007) — this adds the remaining
-- provider-agnostic fields (bucket, object id, generic metadata) to both
-- tables for symmetry.
-- ============================================================

ALTER TABLE knowledge_files
  ADD COLUMN IF NOT EXISTS storage_provider  TEXT DEFAULT 'local',
  ADD COLUMN IF NOT EXISTS storage_bucket    TEXT,
  ADD COLUMN IF NOT EXISTS storage_path      TEXT,
  ADD COLUMN IF NOT EXISTS storage_object_id TEXT,
  ADD COLUMN IF NOT EXISTS storage_url       TEXT,
  ADD COLUMN IF NOT EXISTS original_filename TEXT,
  ADD COLUMN IF NOT EXISTS checksum          TEXT,
  ADD COLUMN IF NOT EXISTS storage_metadata  JSONB DEFAULT '{}'::jsonb;

ALTER TABLE knowledge_attachments
  ADD COLUMN IF NOT EXISTS storage_bucket    TEXT,
  ADD COLUMN IF NOT EXISTS storage_object_id TEXT;

-- Backfill existing rows: they're all on local disk under knowledge/<filename>.
UPDATE knowledge_files
   SET storage_provider = 'local',
       storage_path     = filename,
       original_filename = COALESCE(original_filename, filename)
 WHERE storage_path IS NULL;

CREATE INDEX IF NOT EXISTS idx_kf_storage_provider ON knowledge_files (storage_provider);
CREATE INDEX IF NOT EXISTS idx_kf_checksum         ON knowledge_files (checksum);
