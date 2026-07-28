-- AI Policies — simple, business-user-friendly policy sets. Mirrors the
-- shape of migrations/017_prompt_studio.sql (name/description/is_active/
-- is_default/soft-delete) but has NO version lineage (no policy version
-- comparison per spec) — a "Save" always edits the row in place.
--
-- `config` is an internal JSONB blob the ADMIN UI translates to/from
-- simple toggles/dropdowns/short text fields — it is never shown to a
-- user as raw JSON. Shape (all keys optional; missing = use the
-- service-layer default):
-- {
--   "business_rules":   {"no_guess_prices": bool, "no_guess_delivery_status": bool,
--                          "no_answer_unavailable_info": bool, "require_approved_knowledge": bool},
--   "knowledge_rules":  {"use_rag_first": bool, "cite_source": bool,
--                          "say_if_no_info": bool, "prefer_latest_document": bool},
--   "escalation_rules": {"enabled": bool, "confidence_threshold": number (0-1),
--                          "message": text, "escalate_on_no_answer": bool,
--                          "escalate_on_dissatisfaction": bool},
--   "attachment_rules": {"send_image_if_available": bool, "send_file_link_if_available": bool,
--                          "skip_broken_attachments": bool, "text_first": bool},
--   "channel_rules":    {"line_response_length": "short"|"standard"|"detailed",
--                          "emoji_usage": "never"|"sometimes"|"allowed",
--                          "formality": "formal"|"friendly"|"neutral"}
-- }

CREATE TABLE IF NOT EXISTS ai_policy_sets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    description  TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    is_default   BOOLEAN NOT NULL DEFAULT false,
    config       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

-- Only one ACTIVE default policy set — same pattern as
-- migration 018's uniq_active_embedding_version.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_default_policy_set
    ON ai_policy_sets(is_default) WHERE is_default;

CREATE INDEX IF NOT EXISTS idx_ai_policy_sets_deleted_at ON ai_policy_sets(deleted_at);

ALTER TABLE ai_policy_sets ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY service_role_all_ai_policy_sets ON ai_policy_sets FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Seed one sensible-default policy set, only on a fresh install.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ai_policy_sets WHERE name = 'Standard Policy' AND deleted_at IS NULL) THEN
        INSERT INTO ai_policy_sets (name, description, is_active, is_default, config)
        VALUES (
            'Standard Policy',
            'Sensible default rules for accuracy, escalation, attachments, and channel tone.',
            true, true,
            '{
              "business_rules": {"no_guess_prices": true, "no_guess_delivery_status": true,
                                   "no_answer_unavailable_info": true, "require_approved_knowledge": true},
              "knowledge_rules": {"use_rag_first": true, "cite_source": true,
                                    "say_if_no_info": true, "prefer_latest_document": true},
              "escalation_rules": {"enabled": true, "confidence_threshold": 0.5,
                                     "message": "ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ",
                                     "escalate_on_no_answer": true, "escalate_on_dissatisfaction": true},
              "attachment_rules": {"send_image_if_available": true, "send_file_link_if_available": true,
                                     "skip_broken_attachments": true, "text_first": true},
              "channel_rules": {"line_response_length": "standard", "emoji_usage": "sometimes", "formality": "friendly"}
            }'::jsonb
        );
    END IF;
END $$;
