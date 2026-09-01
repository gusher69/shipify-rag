-- Migration 047: user_profiles sentiment status (P4.1 Negative detection, 2026-09-01)
--
-- Sentiment is NORMAL | NEGATIVE — an analytical side-channel, SEPARATE
-- from lead_stage (migration 046) and from conversation_tier (Phase 3.3).
-- It is NOT a fourth lead stage. Written after every real LINE turn by
-- services/sentiment_service.py (detached post-reply bookkeeping) and
-- surfaced read-only in the "LINE User Profile" admin viewer.
--
-- negative_last_detected_at drives the 24h retention/reset boundary;
-- negative_last_alert_at bounds the one-alert-per-incident dedup.
--
-- Idempotent — safe to run multiple times.

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS sentiment_status TEXT NOT NULL DEFAULT 'NORMAL';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS sentiment_reasons JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS sentiment_updated_at TIMESTAMPTZ;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS negative_last_detected_at TIMESTAMPTZ;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS negative_last_alert_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_user_profiles_sentiment_status ON user_profiles(sentiment_status);
