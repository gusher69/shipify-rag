-- ============================================================
-- Migration 026: Business Action Center — platform-first registry of
-- configurable Business Actions (RAG / API / Tool / Workflow /
-- Notification / Human Handoff / Webhook). Evaluation/registry
-- infrastructure only — does NOT touch knowledge_chunks, retrieval,
-- prompts, policies, or benchmark tables. No execution routing lives
-- here; this is ONLY the registry the future Decision Engine will read.
-- Idempotent — safe to run multiple times.
-- ============================================================

CREATE TABLE IF NOT EXISTS business_actions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_key           TEXT NOT NULL UNIQUE,   -- stable slug, e.g. "tracking_lookup"
    name                 TEXT NOT NULL,
    display_name         TEXT,
    description          TEXT,
    enabled              BOOLEAN NOT NULL DEFAULT true,
    version              INT NOT NULL DEFAULT 1,

    -- Classification
    action_type          TEXT NOT NULL,          -- RAG | API | TOOL | WORKFLOW | NOTIFICATION | HUMAN_HANDOFF | WEBHOOK
    category             TEXT,
    priority             INT NOT NULL DEFAULT 0,

    -- AI Metadata (feeds Action Embeddings later — see business_action_embeddings)
    ai_description        TEXT,   -- "Use this action when the customer wants to know the shipment status."
    business_description  TEXT,
    search_keywords        JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Workflow-level rules (parameter-level detail lives in
    -- business_action_parameters; these are action-wide policies)
    retry_rules           JSONB NOT NULL DEFAULT '{}'::jsonb,
    escalation_rules       JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Response prompts
    success_prompt        TEXT,
    failure_prompt        TEXT,
    follow_up_prompt      TEXT,

    -- Developer
    test_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    debug_notes           TEXT,

    -- Audit
    created_by            TEXT,
    updated_by            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at             TIMESTAMPTZ  -- soft delete, same convention as knowledge_items
);

CREATE TABLE IF NOT EXISTS business_action_examples (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id    UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    example_type TEXT NOT NULL DEFAULT 'question',  -- question | trigger
    example_text TEXT NOT NULL,
    sort_order   INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_action_parameters (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id        UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,             -- e.g. "tracking_number"
    display_name     TEXT,
    required         BOOLEAN NOT NULL DEFAULT true,
    validation_type  TEXT,                      -- references business_action_validation.validation_type/rule by name
    example_value    TEXT,
    description      TEXT,
    slot_type        TEXT,                      -- future Information Collection Engine slot-type binding
    sort_order       INT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(action_id, name)
);

CREATE TABLE IF NOT EXISTS business_action_execution (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id         UUID NOT NULL UNIQUE REFERENCES business_actions(id) ON DELETE CASCADE,
    execution_target  TEXT,                     -- free-form label, e.g. "ERP Adapter: Tracking"
    endpoint          TEXT,
    http_method       TEXT NOT NULL DEFAULT 'GET',  -- GET | POST | PUT | PATCH | DELETE
    headers           JSONB NOT NULL DEFAULT '{}'::jsonb,
    auth_type         TEXT NOT NULL DEFAULT 'none', -- none | bearer | api_key | custom_header | oauth (future)
    auth_config       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- secret values — NEVER returned verbatim by the API; see registry masking
    timeout_seconds   INT NOT NULL DEFAULT 10,
    retry_policy      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_action_response_mapping (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id     UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    json_path     TEXT NOT NULL,        -- e.g. "$.status"
    mapped_label  TEXT NOT NULL,        -- e.g. "Shipment Status"
    sort_order    INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_action_validation (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id        UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    parameter_name   TEXT,             -- NULL = action-wide validation rule
    validation_type  TEXT NOT NULL,    -- regex | length | checksum (future) | external (future)
    rule             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_action_tags (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id  UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    UNIQUE(action_id, tag)
);

-- Action Embeddings — architecture prepared, NOT computed/used for
-- routing in this task ("Prepare the architecture only"). embedding
-- stays NULL until a future Decision Engine task computes it, matching
-- knowledge_chunks' own VECTOR(3072) convention (migrations/019).
CREATE TABLE IF NOT EXISTS business_action_embeddings (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id             UUID NOT NULL UNIQUE REFERENCES business_actions(id) ON DELETE CASCADE,
    embedding_source_text TEXT NOT NULL,   -- concatenated name+description+ai_description+examples+keywords
    embedding             VECTOR(3072),
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending | computed | stale
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ba_action_key       ON business_actions(action_key);
CREATE INDEX IF NOT EXISTS idx_ba_enabled          ON business_actions(enabled);
CREATE INDEX IF NOT EXISTS idx_ba_action_type      ON business_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_ba_category         ON business_actions(category);
CREATE INDEX IF NOT EXISTS idx_bae_action_id       ON business_action_examples(action_id);
CREATE INDEX IF NOT EXISTS idx_bap_action_id       ON business_action_parameters(action_id);
CREATE INDEX IF NOT EXISTS idx_bam_action_id       ON business_action_response_mapping(action_id);
CREATE INDEX IF NOT EXISTS idx_bav_action_id       ON business_action_validation(action_id);
CREATE INDEX IF NOT EXISTS idx_bat_action_id       ON business_action_tags(action_id);
CREATE INDEX IF NOT EXISTS idx_bat_tag             ON business_action_tags(tag);
CREATE INDEX IF NOT EXISTS idx_bae2_action_id      ON business_action_embeddings(action_id);
CREATE INDEX IF NOT EXISTS idx_bae2_status         ON business_action_embeddings(status);

ALTER TABLE business_actions                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_examples          ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_parameters        ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_execution         ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_response_mapping  ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_validation        ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_tags              ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_action_embeddings        ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_business_actions" ON business_actions;
CREATE POLICY "service_role_all_business_actions" ON business_actions
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_examples" ON business_action_examples;
CREATE POLICY "service_role_all_business_action_examples" ON business_action_examples
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_parameters" ON business_action_parameters;
CREATE POLICY "service_role_all_business_action_parameters" ON business_action_parameters
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_execution" ON business_action_execution;
CREATE POLICY "service_role_all_business_action_execution" ON business_action_execution
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_response_mapping" ON business_action_response_mapping;
CREATE POLICY "service_role_all_business_action_response_mapping" ON business_action_response_mapping
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_validation" ON business_action_validation;
CREATE POLICY "service_role_all_business_action_validation" ON business_action_validation
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_tags" ON business_action_tags;
CREATE POLICY "service_role_all_business_action_tags" ON business_action_tags
  FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_business_action_embeddings" ON business_action_embeddings;
CREATE POLICY "service_role_all_business_action_embeddings" ON business_action_embeddings
  FOR ALL USING (auth.role() = 'service_role');
