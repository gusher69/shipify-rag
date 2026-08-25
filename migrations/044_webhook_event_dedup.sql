-- Migration 044: webhook_processed_events (Webhook Redelivery Dedup fix,
-- 2026-08-25) — generic, channel-agnostic protection against LINE (or
-- any future channel) redelivering the SAME webhook event and having it
-- reprocessed as if it were new. Confirmed live investigation
-- (Rapid-Message Concurrency task): the LINE webhook process runs as a
-- single worker with a fully synchronous handler chain, so true
-- concurrent processing of two events is not possible — but nothing
-- anywhere in the codebase previously checked `webhookEventId` at all,
-- so a genuine platform-side redelivery (e.g. triggered by a slow ERP/
-- RAG/LLM turn delaying the webhook's HTTP response past the channel's
-- own timeout) would silently re-run the full Decision Engine turn,
-- re-attempt a reply, and re-record history/profile updates.
--
-- One row per (tenant_id, channel, webhook_event_id) claimed BEFORE any
-- processing begins — the unique constraint below is the actual dedup
-- mechanism (insert-or-detect-conflict), not merely a check-then-insert
-- race that a future multi-worker deployment could reintroduce.

CREATE TABLE IF NOT EXISTS webhook_processed_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    webhook_event_id TEXT NOT NULL,
    conversation_key TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, webhook_event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_processed_events_lookup
    ON webhook_processed_events (tenant_id, channel, webhook_event_id);
