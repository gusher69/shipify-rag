-- ============================================================
-- Migration 014: AI Knowledge Analyzer metadata columns
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Stores the output of services.knowledge_analyzer.KnowledgeAnalyzerService
-- (runs after extraction, before chunking) on the file record it
-- analyzed. This is retrieval-enhancement metadata ONLY — the original
-- extracted text/chunks are never replaced by anything here; see
-- services/knowledge_analyzer.py's module docstring.
-- ============================================================

ALTER TABLE knowledge_files
  ADD COLUMN IF NOT EXISTS knowledge_type          TEXT,
  ADD COLUMN IF NOT EXISTS knowledge_type_confidence NUMERIC,
  ADD COLUMN IF NOT EXISTS chunk_strategy           TEXT,
  ADD COLUMN IF NOT EXISTS summary_short            TEXT,
  ADD COLUMN IF NOT EXISTS summary_long             TEXT,
  ADD COLUMN IF NOT EXISTS topics                   JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS suggested_questions       JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS document_structure        JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS quality_issues            JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS ai_suggestions            JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS ai_audience               TEXT,
  ADD COLUMN IF NOT EXISTS ai_department             TEXT,
  ADD COLUMN IF NOT EXISTS ai_difficulty             TEXT,
  ADD COLUMN IF NOT EXISTS ai_visibility             TEXT,
  ADD COLUMN IF NOT EXISTS ai_priority               TEXT,
  ADD COLUMN IF NOT EXISTS analysis_version          TEXT;

CREATE INDEX IF NOT EXISTS idx_kf_knowledge_type ON knowledge_files (knowledge_type);
CREATE INDEX IF NOT EXISTS idx_kf_chunk_strategy ON knowledge_files (chunk_strategy);
