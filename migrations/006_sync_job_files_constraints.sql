-- ============================================================
-- Migration 006: Data-integrity constraints for sync job/file linkage
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Fixes: knowledge_sync_job_files.file_id had no index and no FK, so a
-- lookup mistake or stale reference could silently attach a job_files row
-- to the wrong (or a deleted) knowledge_files row with no DB-level guard.
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_ksjf_file_id ON knowledge_sync_job_files (file_id);

-- Clean up orphaned references BEFORE adding the FK — these are
-- job_files rows whose file_id points at a knowledge_files row that was
-- hard-deleted (or never existed) at some point in the past, from before
-- this constraint existed. NULL them out rather than deleting the rows,
-- since job_files rows are historical/audit records for Sync Activity.
UPDATE knowledge_sync_job_files ksjf
SET file_id = NULL
WHERE file_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_files kf WHERE kf.id = ksjf.file_id
  );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ksjf_file_id'
    ) THEN
        ALTER TABLE knowledge_sync_job_files
            ADD CONSTRAINT fk_ksjf_file_id FOREIGN KEY (file_id)
            REFERENCES knowledge_files(id) ON DELETE SET NULL;
    END IF;
END $$;
