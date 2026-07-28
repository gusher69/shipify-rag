-- ============================================================
-- Migration 007: Attachment soft-delete + integrity fields
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Adds the columns needed to route attachments through a storage-provider
-- abstraction (StorageService) instead of assuming local disk / Drive
-- directly, and to soft-delete attachment records (consistent with how
-- knowledge_files is soft-deleted elsewhere in this system) instead of
-- hard-deleting them.
-- ============================================================

ALTER TABLE knowledge_attachments
  ADD COLUMN IF NOT EXISTS original_filename TEXT,
  ADD COLUMN IF NOT EXISTS checksum          TEXT,
  ADD COLUMN IF NOT EXISTS deleted_at        TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_katt_deleted_at ON knowledge_attachments (deleted_at);
CREATE INDEX IF NOT EXISTS idx_katt_checksum   ON knowledge_attachments (checksum);

-- Backfill original_filename from filename for existing rows so nothing
-- reads a NULL where it previously read filename.
UPDATE knowledge_attachments
   SET original_filename = filename
 WHERE original_filename IS NULL;
