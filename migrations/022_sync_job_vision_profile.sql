-- Migration 022: persist the Vision/OCR analysis profile actually used
-- per sync job/file, so debugging which profile a given import ran under
-- is unambiguous (see admin/routes.py's _resolve_vision_ocr_profile()).
--
-- knowledge_sync_jobs already has a `metadata JSONB` column (migration
-- 002); knowledge_sync_job_files does not — added here so per-file
-- profile info (a job can in theory mix profiles across files) has a
-- home without overloading existing typed columns.

ALTER TABLE knowledge_sync_job_files
    ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
