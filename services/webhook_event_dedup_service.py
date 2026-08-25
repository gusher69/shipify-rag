"""Webhook Redelivery Dedup — generic, channel-agnostic protection
against a channel (LINE today, any future channel) redelivering the SAME
webhook event and having it reprocessed as if it were new (Rapid-Message
Concurrency investigation, 2026-08-25). See migrations/044_webhook_event_dedup.sql.

Concurrency finding this fix responds to: the LINE webhook process is a
single worker with a fully synchronous handler chain, so two DIFFERENT
events can never be processed with overlapping business logic — but nothing
previously checked `webhookEventId`, so a genuine redelivery (the channel
not receiving an HTTP response before its own timeout, most plausible when
a slow ERP/RAG/LLM turn delays that response) would silently re-run the
full turn.

`claim_event` uses INSERT-then-detect-unique-violation (never check-then-
insert) as the actual dedup mechanism, so it stays correct even if this
process is ever scaled to multiple workers — the database's UNIQUE
constraint is the single source of truth, not an in-process check."""
from typing import Optional

from postgrest.exceptions import APIError

_TABLE = "webhook_processed_events"

_UNIQUE_VIOLATION = "23505"


class WebhookEventDedupService:
    def __init__(self, sb):
        self._sb = sb

    def claim_event(self, *, tenant_id: str, channel: str, webhook_event_id: str,
                     conversation_key: Optional[str] = None) -> bool:
        """Returns True the FIRST time this (tenant_id, channel,
        webhook_event_id) is seen — the caller should proceed with normal
        processing. Returns False if this exact event was already claimed
        (a redelivery) — the caller must skip processing entirely.

        Fails OPEN (returns True, i.e. "proceed") on any error other than
        a genuine duplicate — this table existing/reachable must never be
        a precondition for answering a real customer message, matching
        every other best-effort persistence call in this codebase (see
        services/session_service.py's own "treating as no history: db
        down" degrade pattern)."""
        if not webhook_event_id:
            # No id to dedup against (e.g. a synthetic/test event) —
            # nothing to claim, never block processing over this.
            return True
        try:
            self._sb.table(_TABLE).insert({
                "tenant_id": tenant_id, "channel": channel,
                "webhook_event_id": webhook_event_id, "conversation_key": conversation_key,
            }).execute()
            return True
        except APIError as e:
            if getattr(e, "code", None) == _UNIQUE_VIOLATION:
                return False
            print(f"[webhook_event_dedup] claim failed (non-fatal, proceeding): {e}")
            return True
        except Exception as e:
            print(f"[webhook_event_dedup] claim failed (non-fatal, proceeding): {e}")
            return True


_instance: Optional[WebhookEventDedupService] = None


def get_webhook_event_dedup_service(sb=None) -> WebhookEventDedupService:
    """Named factory, same pattern as services/pending_confirmation_service.py
    ::get_pending_confirmation_service(sb). A module-level singleton is
    cached only when no sb is given."""
    global _instance
    if sb is not None:
        return WebhookEventDedupService(sb)
    if _instance is None:
        from admin.routes import get_sb
        _instance = WebhookEventDedupService(get_sb())
    return _instance
