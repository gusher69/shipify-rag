-- ============================================================
-- Migration 040: Golden Test Registry.
-- Permanent, human-reviewable evaluation records for the 50-conversation
-- Golden Test Suite (2026-08-16) — distinct from rag_benchmark_* (RAG-only
-- single-turn Q&A datasets) and erp_test_cases (single ERP-action cases):
-- this links to a REAL persisted multi-turn ai_sessions conversation
-- (any routing type: RAG/ERP/HYBRID/WORKFLOW/HUMAN_HANDOFF) and carries an
-- explicit manual_review_status a human works through in the Admin UI —
-- automatic checks (route/action match) are advisory only, never a
-- substitute for a human judging answer quality.
-- Idempotent — safe to run multiple times.
-- ============================================================

CREATE TABLE IF NOT EXISTS golden_test_registry (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    golden_id              TEXT NOT NULL UNIQUE,       -- e.g. "GOLDEN-001-RAG-SERVICES"
    session_id             UUID REFERENCES ai_sessions(id) ON DELETE SET NULL,
    category               TEXT NOT NULL,               -- RAG | CUSTOMER_ERP | ORDER | SHIPMENT | TRACKING |
                                                          -- HYBRID | CONTEXT | CUSTOMER_INTELLIGENCE |
                                                          -- HUMAN_HANDOFF | SAFETY | MULTI_USER_ISOLATION | NATURAL_LANGUAGE
    user_journey           JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ordered list of the turn questions actually sent
    expected_route         TEXT,                        -- RAG | API | WEBHOOK | HYBRID | WORKFLOW | HUMAN_HANDOFF
    expected_action        TEXT,                        -- business_action key, or NULL when not action-specific
    actual_route            TEXT,
    actual_action            TEXT,
    expected_behavior      TEXT NOT NULL,                -- free-text description of what "correct" looks like
    automatic_result       TEXT NOT NULL DEFAULT 'PENDING',  -- PASS | FAIL | PENDING (route/action match only)
    automatic_result_reason TEXT,
    manual_review_status   TEXT NOT NULL DEFAULT 'PENDING_REVIEW',  -- PENDING_REVIEW | PASS | FAIL
    manual_review_notes    TEXT,
    reviewed_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_golden_test_registry_session_id ON golden_test_registry(session_id);
CREATE INDEX IF NOT EXISTS idx_golden_test_registry_category ON golden_test_registry(category);
CREATE INDEX IF NOT EXISTS idx_golden_test_registry_manual_review_status ON golden_test_registry(manual_review_status);

ALTER TABLE golden_test_registry ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_golden_test_registry" ON golden_test_registry;
CREATE POLICY "service_role_all_golden_test_registry" ON golden_test_registry
  FOR ALL USING (auth.role() = 'service_role');
