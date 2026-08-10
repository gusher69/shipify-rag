-- Phase 3 — Conversation Intelligence & Knowledge Isolation.
-- Purely additive: extends ai_sessions/ai_session_messages/ai_session_traces
-- (services/session_service.py, already used by the AI Playground) so the
-- SAME Conversation->Turn model now also captures real LINE OA traffic,
-- extends user_profiles with conversation-behavior stats, adds a NEW
-- conversation_tier axis (kept separate from the existing purchase-based
-- `segment` — confirmed unused elsewhere, per-decision 2026-08-05), adds
-- per-tier prompt assignment (mirrors ai_prompt_assignments), and adds
-- Knowledge Collections to stop unrelated knowledge bases (e.g. the
-- confirmed-contaminating "ZWIZ.AI_SME" file) from being searchable
-- together with Shipify's own content. Idempotent — safe to run multiple
-- times.

-- ── 1. ai_sessions — which channel produced this conversation, and which
--    LINE user it belongs to (NULL for AI Playground sessions) ──────────
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'playground';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS line_user_id TEXT;
-- Denormalized snapshot of the tier at the time of the session's last
-- turn — authoritative value always lives on user_profiles.conversation_tier;
-- this is only for cheap dashboard filtering without a join.
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS conversation_tier TEXT;

CREATE INDEX IF NOT EXISTS idx_ai_sessions_line_user_id ON ai_sessions(line_user_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_channel ON ai_sessions(channel);

-- ── 2. ai_session_messages — lightweight, aggregatable columns needed by
--    the Analytics Dashboard (Routing/Intent/Escalation statistics). Kept
--    as plain columns (not JSONB) specifically so GROUP BY queries stay
--    simple and fast — the heavier nested detail still lives in
--    ai_session_traces below, unchanged. ────────────────────────────────
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS broad_intent TEXT;
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS actionable_intent TEXT;
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS routing_type TEXT;
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS selected_business_action TEXT;
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS escalated BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE ai_session_messages ADD COLUMN IF NOT EXISTS escalation_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_ai_session_messages_routing_type ON ai_session_messages(routing_type);
CREATE INDEX IF NOT EXISTS idx_ai_session_messages_escalated ON ai_session_messages(escalated) WHERE escalated;

-- ── 3. ai_session_traces — ERP request/response (ALREADY masked/sanitized
--    by the SAME helpers action_executor.py uses before this is ever
--    written — never the raw secret/PII payload) + attachment metadata. ──
ALTER TABLE ai_session_traces ADD COLUMN IF NOT EXISTS erp_request JSONB NOT NULL DEFAULT '{}';
ALTER TABLE ai_session_traces ADD COLUMN IF NOT EXISTS erp_response JSONB NOT NULL DEFAULT '{}';
ALTER TABLE ai_session_traces ADD COLUMN IF NOT EXISTS attachment_metadata JSONB NOT NULL DEFAULT '{}';

-- ── 4. user_profiles — incremental conversation-behavior stats (Phase
--    3.2) + the NEW, separate conversation_tier axis (Phase 3.3). Table
--    itself is created by supabase_setup.sql on a fresh install; these
--    ADD COLUMN IF NOT EXISTS statements bring an existing deployment's
--    table up to date the same way every other migration in this repo
--    does for tables defined outside migrations/. ─────────────────────
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS message_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS conversation_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avg_confidence NUMERIC(5,4);
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avg_response_time_ms NUMERIC(12,2);
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS interested_topics JSONB NOT NULL DEFAULT '[]';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS products_mentioned JSONB NOT NULL DEFAULT '[]';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS erp_requests_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS complaint_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS positive_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS negative_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS escalation_count INT NOT NULL DEFAULT 0;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]';
-- `segment` (cold/warm/hot, order/spend-based, profiles/manager.py::calc_segment)
-- is UNCHANGED and stays purchase-only. `conversation_tier` is the new,
-- independent conversation-behavior axis (cold/warm/hot/negative) that
-- Prompt Studio's Customer Tier Prompt section actually consumes.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS conversation_tier TEXT NOT NULL DEFAULT 'cold'
    CHECK (conversation_tier IN ('cold', 'warm', 'hot', 'negative'));
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS tier_score NUMERIC(6,2) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_user_profiles_conversation_tier ON user_profiles(conversation_tier);

-- ── 5. ai_prompt_tier_assignments — mirrors ai_prompt_assignments
--    (migration 017) exactly, but keyed by conversation_tier instead of
--    channel. Only one active assignment per tier, enforced at the DB
--    level, same pattern as uniq_active_assignment_per_channel. ────────
CREATE TABLE IF NOT EXISTS ai_prompt_tier_assignments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tier                TEXT NOT NULL CHECK (tier IN ('cold', 'warm', 'hot', 'negative')),
    prompt_template_id  UUID NOT NULL REFERENCES ai_prompt_templates(id),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_assignment_per_tier
    ON ai_prompt_tier_assignments(tier) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_ai_prompt_tier_assignments_tier ON ai_prompt_tier_assignments(tier);

ALTER TABLE ai_prompt_tier_assignments ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY service_role_all_ai_prompt_tier_assignments ON ai_prompt_tier_assignments FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 6. Knowledge Collections (Phase 3.5) — a first-class grouping above
--    knowledge_files.category (which stays untouched, free-text,
--    unchanged) so retrieval can be scoped to "only these collections"
--    instead of searching every uploaded file regardless of source
--    project. Each collection MAY carry its own prompt/policy override
--    (nullable — falls back to the tier/channel/global resolution chain
--    unchanged when not set). ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_collections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    description         TEXT,
    prompt_template_id  UUID REFERENCES ai_prompt_templates(id),
    policy_set_id       UUID REFERENCES ai_policy_sets(id),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_default          BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_knowledge_collections_name
    ON knowledge_collections(name) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_knowledge_collections_deleted_at ON knowledge_collections(deleted_at);

ALTER TABLE ai_prompt_tier_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_collections ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY service_role_all_knowledge_collections ON knowledge_collections FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS collection_id UUID REFERENCES knowledge_collections(id);
CREATE INDEX IF NOT EXISTS idx_knowledge_files_collection_id ON knowledge_files(collection_id);

-- Seed two collections on a fresh install / first run of this migration:
-- "Shipify" (the default, active collection every LINE/Playground query
-- searches unless the admin picks otherwise) and "Uncategorized" (a
-- landing spot for every file that predates Collections, so nothing
-- silently disappears from retrieval the moment this migration runs).
DO $$
DECLARE
    v_shipify_id UUID;
    v_uncategorized_id UUID;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM knowledge_collections WHERE name = 'Shipify' AND deleted_at IS NULL) THEN
        INSERT INTO knowledge_collections (name, description, is_active, is_default)
        VALUES ('Shipify', 'Shipify import/logistics knowledge — the default collection for LINE OA and the AI Playground.', true, true)
        RETURNING id INTO v_shipify_id;
    ELSE
        SELECT id INTO v_shipify_id FROM knowledge_collections WHERE name = 'Shipify' AND deleted_at IS NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM knowledge_collections WHERE name = 'Uncategorized' AND deleted_at IS NULL) THEN
        INSERT INTO knowledge_collections (name, description, is_active, is_default)
        VALUES ('Uncategorized', 'Files not yet assigned to a collection.', true, false)
        RETURNING id INTO v_uncategorized_id;
    ELSE
        SELECT id INTO v_uncategorized_id FROM knowledge_collections WHERE name = 'Uncategorized' AND deleted_at IS NULL;
    END IF;

    -- Every existing file that has no collection yet defaults into
    -- "Shipify" (the pre-Phase-3 behavior was "everything is searchable
    -- together", so this preserves current retrieval behavior for every
    -- file EXCEPT the ones explicitly quarantined below).
    UPDATE knowledge_files SET collection_id = v_shipify_id WHERE collection_id IS NULL;

    -- Quarantine the confirmed-contaminating file found during the live
    -- incident trace (2026-08-05): "(TH) ZWIZ.AI_SME 2023 ver.1.pdf" is a
    -- product demo/brochure for the ZWIZ.AI vendor platform itself, not
    -- Shipify content, and was reproducibly retrieved for customer
    -- questions containing the word "แพ็กเกจ" (package). Moving it out of
    -- the default Shipify collection removes it from LINE/default
    -- retrieval without deleting the file.
    UPDATE knowledge_files SET collection_id = v_uncategorized_id
    WHERE filename ILIKE '%ZWIZ%' AND collection_id = v_shipify_id;
END $$;
