"""Pending Confirmation Service — generic, channel-agnostic persistence
for the "confirm before executing a COMMAND-type Business Action" flow
(LINE Confirmation Flow sprint, 2026-08-10). A Channel Adapter (LINE
today, any future channel) uses this to:

  1. persist a pending confirmation when the Decision Engine returns a
     confirmation-required result (see services/decision_engine.py::
     _requires_confirmation / _generate_confirmation_question),
  2. look up the active (not expired, not yet resolved) pending
     confirmation for the current tenant/channel/conversation on the
     customer's NEXT message,
  3. mark it confirmed/cancelled/executed, with an audit timestamp for
     each transition.

Table: migrations/036_pending_confirmations.sql. NEVER stores a
credential_store/secret_configuration-sourced parameter value (e.g.
SecretCode) — see _strip_secret_parameters() below, enforced at write
time regardless of what the caller passes in.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import config

_TABLE = "pending_confirmations"


def _strip_secret_parameters(action: Dict, parameters: Dict) -> Dict:
    """Defense in depth: even if a caller accidentally includes a
    credential_store/secret_configuration-sourced value in `parameters`,
    it is dropped before ever reaching the database — pending_parameters
    is customer-facing collected values ONLY, never a secret."""
    secret_names = {
        p["name"] for p in (action.get("parameters") or [])
        if p.get("input_source") in ("credential_store", "secret_configuration")
    }
    return {k: v for k, v in (parameters or {}).items() if k not in secret_names}


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class PendingConfirmationService:
    def __init__(self, sb):
        self._sb = sb

    def create(self, *, tenant_id: str, channel: str, conversation_key: str, action: Dict,
               parameters: Dict, original_message: str, question_text: str,
               confirmation_required: bool = True,
               ttl_seconds: int = config.PENDING_CONFIRMATION_TIMEOUT_SECONDS) -> Dict:
        # A customer can only ever have ONE active pending confirmation
        # per conversation — an earlier still-pending row for this SAME
        # tenant/channel/conversation is superseded, never left dangling
        # alongside a new one (also how "customer revises the message
        # before confirming" is implemented: the stale row is cancelled,
        # a fresh one takes its place with the corrected parameters).
        self.cancel_all_active(tenant_id=tenant_id, channel=channel, conversation_key=conversation_key,
                                source="superseded_by_new_pending_confirmation")

        now = datetime.now(timezone.utc)
        row = {
            "tenant_id": tenant_id, "channel": channel, "conversation_key": conversation_key,
            "pending_action_id": action.get("id"),
            "pending_action_name": action.get("action_key") or action.get("name"),
            "pending_parameters": _strip_secret_parameters(action, parameters),
            "original_message": original_message, "question_text": question_text,
            "confirmation_required": confirmation_required, "status": "pending",
            "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "confirmation_requested_at": now.isoformat(),
        }
        return self._sb.table(_TABLE).insert(row).execute().data[0]

    def get_active(self, *, tenant_id: str, channel: str, conversation_key: str) -> Optional[Dict]:
        """Returns the current pending confirmation for THIS EXACT
        tenant/channel/conversation only (never another user's — see
        _strip... no, see the .eq() scoping below), or None if there
        isn't one, or it has expired (an expired row is marked 'expired'
        here — auditable, not silently dropped)."""
        rows = (self._sb.table(_TABLE).select("*")
                .eq("tenant_id", tenant_id).eq("channel", channel)
                .eq("conversation_key", conversation_key).eq("status", "pending")
                .execute().data or [])
        if not rows:
            return None
        row = rows[0]
        expires_at = _parse_ts(row.get("expires_at"))
        if expires_at and datetime.now(timezone.utc) > expires_at:
            self._sb.table(_TABLE).update({"status": "expired"}).eq("id", row["id"]).execute()
            return None
        return row

    def get_most_recent(self, *, tenant_id: str, channel: str, conversation_key: str) -> Optional[Dict]:
        """Regardless of status — used only to give an expired
        confirmation a helpful "please start again" reply instead of
        silently falling through to an unrelated fresh-turn answer."""
        rows = (self._sb.table(_TABLE).select("*")
                .eq("tenant_id", tenant_id).eq("channel", channel).eq("conversation_key", conversation_key)
                .order("created_at", desc=True).execute().data or [])
        return rows[0] if rows else None

    def cancel_all_active(self, *, tenant_id: str, channel: str, conversation_key: str, source: str) -> None:
        rows = (self._sb.table(_TABLE).select("*")
                .eq("tenant_id", tenant_id).eq("channel", channel)
                .eq("conversation_key", conversation_key).eq("status", "pending")
                .execute().data or [])
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            self._sb.table(_TABLE).update(
                {"status": "cancelled", "confirmation_cancelled_at": now, "confirmation_source": source}
            ).eq("id", row["id"]).execute()

    def mark_confirmed(self, pending_id: str, *, source: str) -> Dict:
        now = datetime.now(timezone.utc).isoformat()
        return self._sb.table(_TABLE).update(
            {"status": "confirmed", "confirmation_confirmed_at": now, "confirmation_source": source}
        ).eq("id", pending_id).execute().data[0]

    def mark_executed(self, pending_id: str) -> Dict:
        # Duplicate-send protection: once a confirmed pending row is
        # marked executed, get_active() can never return it again
        # (status is no longer 'pending') — a repeated "ยืนยัน" reply
        # finds nothing to confirm, so it can never trigger a second
        # real execution for the SAME pending action.
        return self._sb.table(_TABLE).update({"status": "executed"}).eq("id", pending_id).execute().data[0]

    def mark_cancelled(self, pending_id: str, *, source: str) -> Dict:
        now = datetime.now(timezone.utc).isoformat()
        return self._sb.table(_TABLE).update(
            {"status": "cancelled", "confirmation_cancelled_at": now, "confirmation_source": source}
        ).eq("id", pending_id).execute().data[0]


# ── Generic confirmation-reply recognition (deterministic, no LLM;
# consistent with every other conversation-intelligence module in this
# codebase — e.g. services/decision_engine.py::_REFUSAL_RE) ───────────

_TRAILING_PARTICLES_RE = re.compile(r"(นะคะ|นะครับ|ค่ะ|ครับ|น่ะ|จ้า|จ้ะ|นะ)+$")

_CONFIRM_PHRASES = {
    "ใช่", "ยืนยัน", "ตกลง", "ส่งเลย", "โอเค", "ดำเนินการเลย",
    "yes", "confirm", "ok", "okay", "proceed", "send it",
}
_CANCEL_PHRASES = {
    "ไม่", "ไม่ต้อง", "ยกเลิก",
    "cancel", "no",
}


def _normalize_reply(message: str) -> str:
    text = (message or "").strip()
    text = _TRAILING_PARTICLES_RE.sub("", text).strip()
    text = text.rstrip("!.?, ")
    return text.lower()


def classify_confirmation_reply(message: str) -> str:
    """Recognizer for a customer's reply to a pending confirmation
    question — returns 'confirm', 'cancel', or 'other'. Exact-match only
    (after stripping whitespace/trailing Thai politeness particles/
    punctuation and lowercasing) — a substring-anywhere check would risk
    matching a much longer, unrelated sentence that merely happens to
    CONTAIN one of these short words (e.g. a revision request), which
    must fall through to 'other' rather than being misread as a
    decision."""
    normalized = _normalize_reply(message)
    if not normalized:
        return "other"
    if normalized in _CANCEL_PHRASES:
        return "cancel"
    if normalized in _CONFIRM_PHRASES:
        return "confirm"
    return "other"


_instance: Optional[PendingConfirmationService] = None


def get_pending_confirmation_service(sb=None) -> PendingConfirmationService:
    """Named factory, same pattern as services/business_action_registry.py
    ::get_registry(sb). A module-level singleton is cached only when no
    sb is given."""
    global _instance
    if sb is not None:
        return PendingConfirmationService(sb)
    if _instance is None:
        from admin.routes import get_sb
        _instance = PendingConfirmationService(get_sb())
    return _instance
