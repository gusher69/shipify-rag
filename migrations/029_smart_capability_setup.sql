-- Smart Capability Setup — stores type-specific setup metadata that
-- isn't a "parameter" (RAG knowledge scope, TOOL tool_name, HUMAN_HANDOFF/
-- NOTIFICATION triggers, WORKFLOW steps, plus conditions/warnings/
-- connected_system shown in the business-friendly Review screen).
-- Purely additive, same convention as migrations 027/028.
ALTER TABLE business_actions
    ADD COLUMN IF NOT EXISTS setup_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
