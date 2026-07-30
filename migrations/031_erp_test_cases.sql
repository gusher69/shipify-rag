-- ERP Action Test Harness (Part 13) — persists a saved test case for a
-- Business Action so its correctness can be re-checked later. Purely
-- additive, new table, no changes to any existing Business Action /
-- Provider / Executor / Decision Engine / Credential Store table.
-- Explicitly NOT the full Benchmark dashboard (rag_benchmark_* tables) —
-- this is a lightweight, per-action regression record only.

CREATE TABLE IF NOT EXISTS erp_test_cases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id           UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    question            TEXT NOT NULL,
    conversation_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_parameters    JSONB NOT NULL DEFAULT '{}'::jsonb,
    expected_response_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    test_mode           TEXT NOT NULL DEFAULT 'simulation',   -- intent_param|simulation|live
    actual_erp_status   TEXT,
    actual_normalized_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    actual_answer       TEXT,
    pass_fail_status    TEXT NOT NULL DEFAULT 'warning',      -- pass|warning|fail
    total_latency_ms    NUMERIC,
    notes               TEXT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_erp_test_cases_action ON erp_test_cases(action_id);

ALTER TABLE erp_test_cases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS erp_test_cases_service_role ON erp_test_cases;
CREATE POLICY erp_test_cases_service_role ON erp_test_cases
    FOR ALL USING (true) WITH CHECK (true);
