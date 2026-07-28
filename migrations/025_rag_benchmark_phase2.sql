-- ============================================================
-- Migration 025: AI Evaluation Phase 2 — Query Understanding /
-- Conversation Scenario / Critical Facts / Grounding.
-- Purely additive (ADD COLUMN IF NOT EXISTS) on the EXISTING
-- rag_benchmark_cases / rag_benchmark_results tables from migration
-- 020 — no new tables, no renamed/removed columns, safe to run on a
-- database that already has historical benchmark runs. `mode` on
-- rag_benchmark_runs stays a free-form TEXT column (already supports
-- "RETRIEVAL_ONLY"/"FULL_RAG"); the two new modes
-- ("QUERY_UNDERSTANDING"/"CONVERSATION_SCENARIO") are just new string
-- values, no schema change needed there.
-- Idempotent — safe to run multiple times.
-- ============================================================

ALTER TABLE rag_benchmark_cases
    ADD COLUMN IF NOT EXISTS expected_topic               TEXT,
    ADD COLUMN IF NOT EXISTS expected_subtopic             TEXT,
    ADD COLUMN IF NOT EXISTS expected_intent                TEXT,
    ADD COLUMN IF NOT EXISTS expected_entities              JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_excluded_entities     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_canonical_query       TEXT,
    ADD COLUMN IF NOT EXISTS expected_resolved_query        TEXT,
    ADD COLUMN IF NOT EXISTS expected_transition            TEXT,
    ADD COLUMN IF NOT EXISTS expected_conversation_state    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS critical_facts                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Conversation Scenario grouping — cases sharing the same
    -- scenario_key run sequentially (ordered by turn_index) in ONE
    -- session, per Part 2 ("do not evaluate turns independently").
    ADD COLUMN IF NOT EXISTS scenario_key                   TEXT,
    ADD COLUMN IF NOT EXISTS turn_index                     INT,
    -- Only used when a case needs simulated prior turns OUTSIDE a full
    -- scenario run (e.g. a standalone Query Understanding Only case
    -- testing a follow-up in isolation).
    ADD COLUMN IF NOT EXISTS previous_messages              JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_rbc_scenario_key ON rag_benchmark_cases(scenario_key);

ALTER TABLE rag_benchmark_results
    ADD COLUMN IF NOT EXISTS query_understanding_metrics    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS conversation_metrics           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS critical_fact_metrics          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS grounding_metrics               JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Which session_id (services/session_service.py) a Conversation
    -- Scenario turn's result was recorded under — lets the Failure
    -- Inspector reconstruct the full turn sequence for a scenario.
    ADD COLUMN IF NOT EXISTS scenario_key                   TEXT,
    ADD COLUMN IF NOT EXISTS turn_index                     INT;

CREATE INDEX IF NOT EXISTS idx_rbres_scenario_key ON rag_benchmark_results(scenario_key);
