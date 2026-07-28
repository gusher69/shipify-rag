-- ============================================================
-- Migration 021: Retrieval Settings + Query Synonyms.
-- Shipify AI Agent — deliberately separate from Prompt Studio
-- (migration 017 / ai_prompt_templates) — these are Search/Retrieval
-- Engine settings, not LLM prompt settings. Idempotent — safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS rag_retrieval_settings (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type                TEXT NOT NULL DEFAULT 'global',  -- 'global' today; 'channel'/'file' reserved for future per-scope overrides
    scope_id                  TEXT,
    preset                    TEXT NOT NULL DEFAULT 'balanced',  -- balanced | strict | high_recall | custom
    search_strategy           TEXT NOT NULL DEFAULT 'hybrid',    -- vector_only | hybrid | hybrid_graph | graph_only
    candidate_top_k           INT NOT NULL DEFAULT 12,
    final_context_top_k       INT NOT NULL DEFAULT 5,
    absolute_minimum_score    NUMERIC(4,3) NOT NULL DEFAULT 0.15,
    relative_to_best_ratio    NUMERIC(4,3) NOT NULL DEFAULT 0.55,
    minimum_candidates        INT NOT NULL DEFAULT 3,
    maximum_candidates        INT NOT NULL DEFAULT 12,
    semantic_weight           NUMERIC(4,3) NOT NULL DEFAULT 0.50,
    keyword_weight            NUMERIC(4,3) NOT NULL DEFAULT 0.20,
    heading_weight            NUMERIC(4,3) NOT NULL DEFAULT 0.20,
    graph_weight              NUMERIC(4,3) NOT NULL DEFAULT 0.10,
    query_expansion_enabled   BOOLEAN NOT NULL DEFAULT true,
    heading_boost_enabled     BOOLEAN NOT NULL DEFAULT true,
    graph_search_enabled      BOOLEAN NOT NULL DEFAULT false,
    reranker_provider         TEXT NOT NULL DEFAULT 'heuristic', -- none | heuristic | llm | cross_encoder
    heading_exact_boost       NUMERIC(4,3) NOT NULL DEFAULT 0.30,
    heading_synonym_boost     NUMERIC(4,3) NOT NULL DEFAULT 0.22,
    heading_partial_boost     NUMERIC(4,3) NOT NULL DEFAULT 0.12,
    important_heading_boost   NUMERIC(4,3) NOT NULL DEFAULT 0.08,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_rag_retrieval_settings_scope
    ON rag_retrieval_settings(scope_type, COALESCE(scope_id, ''));

CREATE TABLE IF NOT EXISTS rag_query_synonyms (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_term  TEXT NOT NULL,
    synonym         TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'th',  -- th | en | mixed
    category        TEXT,                         -- e.g. 'mission', 'vision', 'location', 'service'
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rqs_canonical ON rag_query_synonyms(canonical_term);
CREATE INDEX IF NOT EXISTS idx_rqs_active    ON rag_query_synonyms(is_active);

ALTER TABLE rag_retrieval_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag_query_synonyms     ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_rag_retrieval_settings" ON rag_retrieval_settings;
CREATE POLICY "service_role_all_rag_retrieval_settings" ON rag_retrieval_settings
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_rag_query_synonyms" ON rag_query_synonyms;
CREATE POLICY "service_role_all_rag_query_synonyms" ON rag_query_synonyms
  FOR ALL USING (auth.role() = 'service_role');

-- Seed the exact global-default row so get_active_settings() finds a row
-- immediately after this migration runs (matches services/retrieval_settings.py's
-- RetrievalSettings() dataclass defaults).
INSERT INTO rag_retrieval_settings (scope_type, scope_id)
SELECT 'global', NULL
WHERE NOT EXISTS (SELECT 1 FROM rag_retrieval_settings WHERE scope_type = 'global');
