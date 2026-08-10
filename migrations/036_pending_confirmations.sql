-- Migration 036: pending_confirmations (LINE Confirmation Flow, 2026-08-10)
--
-- Generic, channel-agnostic pending-confirmation state for ANY Business
-- Action whose operation type requires confirmation before execution
-- (services/decision_engine.py::_requires_confirmation) — not specific
-- to SendLineNotiCS. A Channel Adapter (LINE today, any future channel)
-- persists one row when the Decision Engine returns a confirmation-
-- required result, and consults it on the customer's next message to
-- decide whether "ยืนยัน"/"yes"/etc. should re-invoke the Decision Engine
-- with context={"confirmed": True} for that SAME pending action.
--
-- NEVER stores credentials/secret values — pending_parameters is the
-- customer-facing collected parameter dict only (e.g. Message); a
-- credential_store/secret_configuration-sourced parameter (e.g.
-- SecretCode) is never written here (services/pending_confirmation_service.py
-- enforces this at write time, not just by convention).

CREATE TABLE IF NOT EXISTS pending_confirmations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    -- Scopes isolation (requirement 7: tenant + channel + conversation) —
    -- the LINE user id today; any future channel supplies its own
    -- equivalent conversation identity.
    conversation_key TEXT NOT NULL,
    pending_action_id UUID NOT NULL,
    pending_action_name TEXT,
    pending_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    original_message TEXT,
    -- The EXACT confirmation question text sent to the customer — stored
    -- verbatim so a later reply can be replayed through the Decision
    -- Engine's existing conversation-continuation matching
    -- (_resolve_continuation_action) without recomputing it and risking
    -- silent drift between what was asked and what gets matched.
    question_text TEXT,
    confirmation_required BOOLEAN NOT NULL DEFAULT true,
    -- pending | confirmed | cancelled | expired | executed
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmation_requested_at TIMESTAMPTZ,
    confirmation_confirmed_at TIMESTAMPTZ,
    confirmation_cancelled_at TIMESTAMPTZ,
    confirmation_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_confirmations_lookup
    ON pending_confirmations (tenant_id, channel, conversation_key, status);
