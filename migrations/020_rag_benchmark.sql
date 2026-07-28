-- ============================================================
-- Migration 020: RAG Benchmark & Evaluation module.
-- Shipify AI Agent — evaluation infrastructure only; does NOT touch
-- knowledge_chunks, embeddings, retrieval, or answer-generation tables.
-- Idempotent — safe to run multiple times.
-- ============================================================

CREATE TABLE IF NOT EXISTS rag_benchmark_datasets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_benchmark_cases (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id             UUID NOT NULL REFERENCES rag_benchmark_datasets(id) ON DELETE CASCADE,
    question               TEXT NOT NULL,
    language               TEXT NOT NULL DEFAULT 'English',  -- Thai | English | Mixed Thai-English
    expected_answer        TEXT,
    expected_file          TEXT,
    expected_section       TEXT,
    expected_answerability TEXT,  -- direct_answer | partial_answer | no_information
    must_include           JSONB NOT NULL DEFAULT '[]'::jsonb,
    must_not_include       JSONB NOT NULL DEFAULT '[]'::jsonb,
    prohibited_files       JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes                  TEXT,
    is_active              BOOLEAN NOT NULL DEFAULT true,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_benchmark_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id              UUID REFERENCES rag_benchmark_datasets(id) ON DELETE SET NULL,
    run_name                TEXT NOT NULL,
    mode                    TEXT NOT NULL,  -- RETRIEVAL_ONLY | FULL_RAG
    status                  TEXT NOT NULL DEFAULT 'queued',  -- queued|running|completed|failed|cancelled
    embedding_provider      TEXT,
    embedding_model         TEXT,
    embedding_dimensions    INT,
    llm_model               TEXT,
    prompt_template_id      TEXT,
    prompt_template_version TEXT,
    raw_candidate_count     INT,
    final_top_k             INT,
    config_snapshot         JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    total_cases             INT NOT NULL DEFAULT 0,
    passed_cases            INT NOT NULL DEFAULT 0,
    failed_cases            INT NOT NULL DEFAULT 0,
    average_latency_ms      NUMERIC(12,2),
    p95_latency_ms          NUMERIC(12,2),
    input_tokens            INT NOT NULL DEFAULT 0,
    output_tokens           INT NOT NULL DEFAULT 0,
    estimated_cost          NUMERIC(12,6) NOT NULL DEFAULT 0,
    error_message           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_benchmark_results (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             UUID NOT NULL REFERENCES rag_benchmark_runs(id) ON DELETE CASCADE,
    case_id            UUID REFERENCES rag_benchmark_cases(id) ON DELETE SET NULL,
    -- Snapshots of the case at run time — historical runs must NOT change
    -- meaning if the source case is later edited or deleted (case_id can
    -- go NULL, but these columns preserve what was actually tested).
    question           TEXT NOT NULL,
    expected_answer    TEXT,
    actual_answer      TEXT,
    expected_file      TEXT,
    expected_section   TEXT,
    retrieved_chunks   JSONB NOT NULL DEFAULT '[]'::jsonb,
    selected_evidence  JSONB NOT NULL DEFAULT '[]'::jsonb,
    citations          JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
    answerability      TEXT,
    retrieval_metrics  JSONB NOT NULL DEFAULT '{}'::jsonb,
    answer_metrics     JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms         NUMERIC(12,2),
    input_tokens       INT NOT NULL DEFAULT 0,
    output_tokens      INT NOT NULL DEFAULT 0,
    estimated_cost     NUMERIC(12,6) NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'pass',  -- pass | fail | error
    failure_type       TEXT,  -- retrieval_miss | ranking_failure | wrong_evidence | unsupported_claim |
                               -- wrong_citation | language_failure | partial_answer | system_error
    failure_reason     TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rbc_dataset_id   ON rag_benchmark_cases(dataset_id);
CREATE INDEX IF NOT EXISTS idx_rbc_is_active    ON rag_benchmark_cases(is_active);
CREATE INDEX IF NOT EXISTS idx_rbr_dataset_id   ON rag_benchmark_runs(dataset_id);
CREATE INDEX IF NOT EXISTS idx_rbr_status       ON rag_benchmark_runs(status);
CREATE INDEX IF NOT EXISTS idx_rbres_run_id     ON rag_benchmark_results(run_id);
CREATE INDEX IF NOT EXISTS idx_rbres_case_id    ON rag_benchmark_results(case_id);
CREATE INDEX IF NOT EXISTS idx_rbres_status     ON rag_benchmark_results(status);

ALTER TABLE rag_benchmark_datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag_benchmark_cases    ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag_benchmark_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag_benchmark_results  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_rag_benchmark_datasets" ON rag_benchmark_datasets;
CREATE POLICY "service_role_all_rag_benchmark_datasets" ON rag_benchmark_datasets
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_rag_benchmark_cases" ON rag_benchmark_cases;
CREATE POLICY "service_role_all_rag_benchmark_cases" ON rag_benchmark_cases
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_rag_benchmark_runs" ON rag_benchmark_runs;
CREATE POLICY "service_role_all_rag_benchmark_runs" ON rag_benchmark_runs
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_rag_benchmark_results" ON rag_benchmark_results;
CREATE POLICY "service_role_all_rag_benchmark_results" ON rag_benchmark_results
  FOR ALL USING (auth.role() = 'service_role');
