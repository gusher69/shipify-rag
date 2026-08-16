-- ============================================================
-- Migration 041: Golden Test Runs (immutable run history).
--
-- Golden Test Harness rebuild (2026-08-16, per the Golden Suite
-- Independent Audit) -- adds IMMUTABLE, version-controlled run storage,
-- separate from golden_test_registry (migration 040). golden_test_registry
-- stays exactly as it was: the human-reviewable "current known state per
-- golden_id" table, never touched by tests/golden/golden_runner.py.
--
-- golden_test_runs        -- one row per Golden Suite execution (never updated
--                             after completed_at is set, except by the SAME
--                             run finalizing its own summary counts).
-- golden_test_results      -- one row per (run_id, golden_id) -- INSERT only,
--                             never UPDATE, never DELETE, by any caller.
--                             UNIQUE(run_id, golden_id) makes a second write
--                             for the same run+case a hard constraint error
--                             rather than a silent overwrite.
--
-- This means a run's numbers can always be reproduced later: "which
-- dataset_version and which git_sha produced this PASS" is answered by
-- joining golden_test_results.run_id -> golden_test_runs.
--
-- Idempotent -- safe to run multiple times.
-- ============================================================

CREATE TABLE IF NOT EXISTS golden_test_runs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             TEXT NOT NULL UNIQUE,        -- e.g. "run-20260816-153000-a1b2c3d4"
    dataset_version    TEXT NOT NULL,                -- tests/golden/golden_cases.json's own "dataset_version"
    dataset_path       TEXT NOT NULL DEFAULT 'tests/golden/golden_cases.json',
    git_sha            TEXT,                          -- local/runner checkout HEAD at run time
    server_git_sha     TEXT,                          -- deployed server HEAD, when determinable
    total_cases        INT NOT NULL DEFAULT 0,
    pass_count         INT NOT NULL DEFAULT 0,
    fail_count         INT NOT NULL DEFAULT 0,
    pending_count      INT NOT NULL DEFAULT 0,
    error_count        INT NOT NULL DEFAULT 0,
    skipped_count      INT NOT NULL DEFAULT 0,
    started_at         TIMESTAMPTZ NOT NULL,
    completed_at       TIMESTAMPTZ,
    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS golden_test_results (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                  TEXT NOT NULL REFERENCES golden_test_runs(run_id) ON DELETE CASCADE,
    golden_id               TEXT NOT NULL,
    category                TEXT NOT NULL,
    session_id              UUID REFERENCES ai_sessions(id) ON DELETE SET NULL,
    user_journey            JSONB NOT NULL DEFAULT '[]'::jsonb,   -- resolved turn text actually sent (placeholders substituted)
    expected_route          TEXT,
    expected_action         TEXT,
    expected_behavior       TEXT,
    actual_route            TEXT,      -- last turn's routing_type
    actual_action            TEXT,      -- last turn's selected_business_action
    actual_answer            TEXT,      -- last turn's persisted assistant message content
    automatic_result        TEXT NOT NULL DEFAULT 'PENDING',  -- PASS | FAIL | PENDING | ERROR | SKIPPED
    automatic_result_reason TEXT,
    assertion_results       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{type, passed, detail, turn_index}, ...]
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, golden_id)
);

CREATE INDEX IF NOT EXISTS idx_golden_test_results_run_id ON golden_test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_golden_test_results_golden_id ON golden_test_results(golden_id);
CREATE INDEX IF NOT EXISTS idx_golden_test_results_automatic_result ON golden_test_results(automatic_result);
CREATE INDEX IF NOT EXISTS idx_golden_test_runs_started_at ON golden_test_runs(started_at DESC);

ALTER TABLE golden_test_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE golden_test_results ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_golden_test_runs" ON golden_test_runs;
CREATE POLICY "service_role_all_golden_test_runs" ON golden_test_runs
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_golden_test_results" ON golden_test_results;
CREATE POLICY "service_role_all_golden_test_results" ON golden_test_results
  FOR ALL USING (auth.role() = 'service_role');
