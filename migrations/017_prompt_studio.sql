-- Prompt Studio — DB-backed, admin-editable AI system prompts (replaces
-- the hardcoded LINE OA prompt in line_bot/tone.py and the in-memory
-- registry in services/prompt_builder.py). Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS ai_prompt_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT,
    channel         TEXT,                       -- informational only; real routing is via ai_prompt_assignments
    language        TEXT NOT NULL DEFAULT 'th',
    tone            TEXT,
    system_prompt   TEXT NOT NULL,
    response_rules  JSONB NOT NULL DEFAULT '{}',
    fallback_rules  JSONB NOT NULL DEFAULT '{}',
    safety_rules    JSONB NOT NULL DEFAULT '{}',
    is_active       BOOLEAN NOT NULL DEFAULT false,  -- "usable/live" flag, independent of channel assignment
    is_default      BOOLEAN NOT NULL DEFAULT false,  -- the one Global Default — exactly one should ever be true
    version         INT NOT NULL DEFAULT 1,
    parent_id       UUID REFERENCES ai_prompt_templates(id),  -- version lineage; NULL = original
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ai_prompt_assignments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel             TEXT NOT NULL,   -- Global | LINE OA | Website | Shopee | Lazada | TikTok Shop | Facebook | Instagram
    prompt_template_id  UUID NOT NULL REFERENCES ai_prompt_templates(id),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one ACTIVE assignment per channel — enforced at the DB level, not
-- just in application code, per the spec's "Important: Only one active
-- prompt assignment per channel."
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_assignment_per_channel
    ON ai_prompt_assignments(channel) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_ai_prompt_templates_deleted_at ON ai_prompt_templates(deleted_at);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_templates_parent_id ON ai_prompt_templates(parent_id);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_assignments_channel ON ai_prompt_assignments(channel);

ALTER TABLE ai_prompt_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_prompt_assignments ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY service_role_all_ai_prompt_templates ON ai_prompt_templates FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE POLICY service_role_all_ai_prompt_assignments ON ai_prompt_assignments FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── Seed default prompts (only if this is a fresh install — never
-- overwrites/duplicates on re-run) ──────────────────────────────
DO $$
DECLARE
    v_global_id UUID;
    v_line_id   UUID;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ai_prompt_templates WHERE name = 'Global Default' AND deleted_at IS NULL) THEN
        INSERT INTO ai_prompt_templates (name, description, channel, language, tone, system_prompt, is_active, is_default)
        VALUES (
            'Global Default',
            'The platform-wide fallback prompt used when no channel-specific prompt is assigned.',
            'Global', 'en', 'Professional + Friendly',
            E'You are Shipify AI Customer Service Assistant.\nAnswer politely, accurately, and only based on approved knowledge.',
            true, true
        ) RETURNING id INTO v_global_id;

        INSERT INTO ai_prompt_assignments (channel, prompt_template_id, is_active)
        VALUES ('Global', v_global_id, true);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM ai_prompt_templates WHERE name = 'LINE OA Default' AND deleted_at IS NULL) THEN
        INSERT INTO ai_prompt_templates (name, description, channel, language, tone, system_prompt, is_active)
        VALUES (
            'LINE OA Default',
            'Default assistant persona for the LINE OA customer support channel.',
            'LINE OA', 'th', 'Warm + Concise',
            E'You are Shipify AI for LINE OA customer support.\nReply in Thai by default.\nBe polite, warm, concise, and helpful.\nUse customer-friendly language.\nDo not hallucinate policies, prices, shipping fees, or tracking status.\nIf knowledge is missing, say that the team will check and follow up.\nIf attachments are available, send text first then attachment images/files.',
            true
        ) RETURNING id INTO v_line_id;

        INSERT INTO ai_prompt_assignments (channel, prompt_template_id, is_active)
        VALUES ('LINE OA', v_line_id, true);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM ai_prompt_templates WHERE name = 'ตอบสุภาพ' AND deleted_at IS NULL) THEN
        INSERT INTO ai_prompt_templates (name, description, language, tone, system_prompt, is_active)
        VALUES (
            'ตอบสุภาพ', 'Formal, highly polite response style.', 'th', 'Formal + Polite',
            E'You are Shipify AI Customer Service Assistant.\nตอบด้วยภาษาที่สุภาพมาก เป็นทางการ ใช้คำลงท้าย ค่ะ/ครับ เสมอ\nตอบอย่างถูกต้องตามข้อมูลที่มีเท่านั้น',
            true
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM ai_prompt_templates WHERE name = 'ตอบใส่ใจมาก ๆ' AND deleted_at IS NULL) THEN
        INSERT INTO ai_prompt_templates (name, description, language, tone, system_prompt, is_active)
        VALUES (
            'ตอบใส่ใจมาก ๆ', 'Extra empathetic, caring tone for sensitive situations.', 'th', 'Empathetic + Warm',
            E'You are Shipify AI Customer Service Assistant.\nตอบด้วยความใส่ใจ เห็นอกเห็นใจลูกค้ามาก ๆ แสดงความเข้าใจในความรู้สึกของลูกค้าก่อนตอบคำถาม\nใช้น้ำเสียงอบอุ่น เป็นกันเอง แต่ยังคงความถูกต้องของข้อมูล',
            true
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM ai_prompt_templates WHERE name = 'ตอบสั้น กระชับ' AND deleted_at IS NULL) THEN
        INSERT INTO ai_prompt_templates (name, description, language, tone, system_prompt, is_active)
        VALUES (
            'ตอบสั้น กระชับ', 'Short, to-the-point answers with minimal elaboration.', 'th', 'Concise',
            E'You are Shipify AI Customer Service Assistant.\nตอบสั้น กระชับ ตรงประเด็น ไม่ต้องอธิบายยืดยาว ใช้ประโยคสั้น ๆ ชัดเจน',
            true
        );
    END IF;
END $$;
