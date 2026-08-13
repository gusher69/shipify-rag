-- Migration 037: Human Handoff state (2026-08-13)
--
-- Reuses the EXISTING ai_sessions row as the conversation object (see
-- services/session_service.py::get_or_create_active_conversation — one
-- ai_sessions row per LINE user's active 24h conversation burst,
-- migration 016/035) rather than introducing a new table. Tracks whether
-- this active conversation has already notified CS, so a repeated
-- "ขอคุยกับเจ้าหน้าที่"-style message never creates duplicate notifications
-- for the same unresolved handoff.
--
-- NONE      — no handoff requested yet (default)
-- PENDING   — a handoff trigger fired this turn, notification in flight
-- NOTIFIED  — CS has already been notified for this active conversation
-- RESOLVED  — reserved for a future admin/CS action; not set by this sprint

ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS handoff_status TEXT NOT NULL DEFAULT 'NONE';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS handoff_reason TEXT;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS handoff_notified_at TIMESTAMPTZ;

DO $$ BEGIN
    ALTER TABLE ai_sessions ADD CONSTRAINT ai_sessions_handoff_status_check
        CHECK (handoff_status IN ('NONE', 'PENDING', 'NOTIFIED', 'RESOLVED'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_ai_sessions_handoff_status
    ON ai_sessions(handoff_status) WHERE handoff_status != 'NONE';
