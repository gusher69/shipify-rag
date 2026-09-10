-- Migration 048: Structured conversation frame — SHADOW-WRITE state
-- (System-Wide Conversation Intelligence, P2 — 2026-09-10)
--
-- Purely ADDITIVE. Reuses the EXISTING ai_sessions row as the
-- conversation object (same choice migrations/037_handoff_state.sql
-- made) rather than a new table. One nullable JSONB column carries the
-- structured conversation frame that services/conversation_frame_store.py
-- builds from the P1 canonical ConversationResolution:
--
--   {
--     "version": 1,
--     "journey": "IMPORT_INTEREST",
--     "status": "ACTIVE" | "SUSPENDED" | "COMPLETED" | "CANCELLED" | "EXPIRED",
--     "requested_slot": "product",
--     "slots": { "<name>": {"value":..., "unit":..., "canonical_value":...,
--                            "canonical_unit":..., "raw":"..."} , ... },
--     "updated_at": "...", "turn_seq": 7, "source": "conversation_resolution"
--   }
--
-- P2 STAGE = SHADOW-WRITE ONLY. The runtime authority for journey/slot
-- reads stays with services/conversation_semantics.py::derive_active_frame
-- (legacy assistant-reply-text parsing). This column is written and
-- compared only; nothing routes off it yet (that is P2 read-cutover,
-- separately gated on >= 99% shadow parity).
--
-- Idempotent — safe to run multiple times.
--
-- ROLLBACK: none required. To disable, simply stop writing/reading the
-- column (feature is inert without code that touches it). To fully
-- remove:  ALTER TABLE ai_sessions DROP COLUMN IF EXISTS conversation_frame;
-- No data migration, no backfill: existing sessions keep NULL and fall
-- back to legacy text-based frame recovery exactly as before.

ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS conversation_frame JSONB;

COMMENT ON COLUMN ai_sessions.conversation_frame IS
  'P2 shadow-write structured conversation frame (journey/status/slots with '
  'units + canonical values). Written from the P1 ConversationResolution; '
  'NOT yet a runtime read source (legacy derive_active_frame stays '
  'authoritative until shadow parity >= 99%). NULL on pre-P2 / non-journey '
  'sessions.';
