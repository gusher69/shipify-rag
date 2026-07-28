-- Migration 005: extend knowledge_files with richer metadata fields

ALTER TABLE knowledge_files
  ADD COLUMN IF NOT EXISTS title        TEXT,
  ADD COLUMN IF NOT EXISTS language     TEXT    DEFAULT 'Thai',
  ADD COLUMN IF NOT EXISTS visibility   TEXT    DEFAULT 'Internal Only',
  ADD COLUMN IF NOT EXISTS department   TEXT    DEFAULT '',
  ADD COLUMN IF NOT EXISTS extra_metadata JSONB DEFAULT '{}';

-- Backfill: copy existing category → department where department is empty
UPDATE knowledge_files
   SET department = category
 WHERE department IS NULL OR department = '';
