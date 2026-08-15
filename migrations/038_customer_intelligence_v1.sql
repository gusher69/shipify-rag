-- Migration 038: Customer Intelligence V1 (2026-08-15)
--
-- Purely additive, on the EXISTING user_profiles table (migration 035
-- already added conversation_tier/tier_score — the cold/warm/hot/negative
-- axis this sprint's "customer_stage" reuses unchanged, see
-- services/customer_tier_service.py; no new stage column is introduced
-- here to avoid a second, competing cold/warm/hot/negative field).
--
-- Adds: (1) deterministic identifier memory so a customer doesn't have to
-- repeat their CustCode/OrderCode/ShipmentCode/Tracking within the same
-- relationship, (2) the latest turn's primary_intent, (3) stage
-- explainability (stage_reason/stage_confidence — conversation_tier/
-- tier_score already store the label+score, these add the "why"), and
-- (4) Handoff Recommendation metadata — a recommendation SIGNAL only,
-- deliberately separate from ai_sessions.handoff_status/handoff_reason
-- (migration 037), which tracks whether a REAL SendLineNotiCS
-- notification was actually sent for an active conversation. This
-- sprint never sends that notification; it only prepares the metadata a
-- future Human Handoff integration will consume.

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS cust_code TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_order_code TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_shipment_code TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_tracking TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS primary_intent TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS stage_reason TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS stage_confidence NUMERIC(4,3);
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS handoff_recommended BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS handoff_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_user_profiles_cust_code ON user_profiles(cust_code);
CREATE INDEX IF NOT EXISTS idx_user_profiles_handoff_recommended ON user_profiles(handoff_recommended) WHERE handoff_recommended;
