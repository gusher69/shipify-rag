-- ============================================================
-- Migration 013: Smart Excel Importer enrichment columns
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Adds the columns the "smart" importer needs to persist per-row
-- enrichment it now generates automatically when a customer's Excel file
-- doesn't provide Category/Tags/Alternative Questions/Language columns
-- itself: category (inferred from sheet/file name), tags (keyword-based),
-- alt_questions (best-effort LLM paraphrases, used to improve retrieval),
-- and language (th/en/zh, detected from the row's own text).
--
-- knowledge_attachments.retry_count records how many download attempts an
-- attachment needed (0 = succeeded first try) — purely observational, the
-- importer already retries in-process before ever writing a row.
-- ============================================================

ALTER TABLE knowledge_items
  ADD COLUMN IF NOT EXISTS category      TEXT,
  ADD COLUMN IF NOT EXISTS tags          JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS alt_questions JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS language      TEXT;

ALTER TABLE knowledge_attachments
  ADD COLUMN IF NOT EXISTS retry_count   INT DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ki_category ON knowledge_items (category);
CREATE INDEX IF NOT EXISTS idx_ki_language ON knowledge_items (language);
