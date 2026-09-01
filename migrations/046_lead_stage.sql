-- Migration 046: user_profiles lead stage (P4 Cold / Warm / Hot, 2026-09-01)
--
-- An ANALYTICAL side-channel. Deliberately SEPARATE from
-- user_profiles.conversation_tier / tier_score (Phase 3.3), which drive
-- Prompt Studio's per-tier prompt selection — P4 must not change any AI
-- response, so it gets its own columns that nothing in the reply path
-- reads. Written after every real LINE turn by
-- services/lead_stage_service.py (detached post-reply bookkeeping),
-- surfaced read-only in the "LINE User Profile" admin viewer.
--
-- Idempotent — safe to run multiple times.

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lead_stage TEXT NOT NULL DEFAULT 'COLD';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lead_score INT NOT NULL DEFAULT 0;
-- Small bounded machine-readable reason list, e.g.
--   ["product_identified", "rate_interest", "duration_interest"]
-- Never conversation text, never model reasoning.
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lead_reasons JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lead_stage_updated_at TIMESTAMPTZ;

-- Cheap filter for the admin list (real LINE users are already few).
CREATE INDEX IF NOT EXISTS idx_user_profiles_lead_stage ON user_profiles(lead_stage);
