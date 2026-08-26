from typing import Dict, Optional
from datetime import datetime, timezone
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE = "user_profiles"


def get_profile(line_user_id: str) -> Optional[Dict]:
    try:
        result = supabase.table(TABLE).select("*").eq("line_user_id", line_user_id).single().execute()
        return result.data
    except Exception:
        return None


def upsert_profile(line_user_id: str, data: Dict):
    try:
        row = {
            "line_user_id":  line_user_id,
            "display_name":  data.get("display_name", ""),
            "segment":       calc_segment(data.get("order_count", 0), data.get("total_spend", 0)),
            "order_count":   data.get("order_count", 0),
            "total_spend":   data.get("total_spend", 0),
            "chat_style":    data.get("chat_style", "short"),
            "last_active":   datetime.now(timezone.utc).isoformat(),
            "notes":         data.get("notes", ""),
        }
        supabase.table(TABLE).upsert(row, on_conflict="line_user_id").execute()
    except Exception as e:
        print(f"❌ upsert_profile: {e}")


def calc_segment(order_count: int, total_spend: float) -> str:
    """ปรับเกณฑ์ตามที่คลียร์กับ Mod"""
    if order_count == 0:
        return "cold"
    elif order_count <= 2 or total_spend < 5000:
        return "warm"
    else:
        return "hot"


# ── Phase 3.2 (2026-08-05) — incremental conversation-behavior stats ──────
# Deterministic counters/averages only, per "Do NOT use AI to overwrite
# user profile automatically" — every field below is a plain arithmetic
# update over what the SAME turn's decide() result and SessionService
# already computed, never a second interpretation of it. Uses .update()
# (not upsert_profile's .upsert()) because the row is guaranteed to
# already exist by the time this runs — a plain update only ever touches
# the columns named here, so it can never clobber order_count/total_spend/
# segment/notes the way re-supplying a full upsert row would.

_COMPLAINT_ALERT_TYPES = ("complaint", "legal_threat")


def update_profile_from_turn(line_user_id: str, *, decide_result: Dict, conversation_fields: Dict,
                              is_new_conversation: bool) -> Optional[Dict]:
    """Called once per real LINE conversation turn (line_bot/webhook.py),
    right after the turn is persisted to Conversation History. Returns the
    updated profile row (or None on failure — never raises, matching
    every other function in this module)."""
    try:
        profile = get_profile(line_user_id) or {}
        message_count = (profile.get("message_count") or 0) + 1
        conversation_count = (profile.get("conversation_count") or 0) + (1 if is_new_conversation else 0)

        confidence = conversation_fields.get("confidence")
        prev_avg_conf = profile.get("avg_confidence")
        avg_confidence = prev_avg_conf
        if confidence is not None:
            avg_confidence = confidence if prev_avg_conf is None else \
                (float(prev_avg_conf) * (message_count - 1) + confidence) / message_count

        latency_ms = conversation_fields.get("latency_ms")
        prev_avg_latency = profile.get("avg_response_time_ms")
        avg_response_time_ms = prev_avg_latency
        if latency_ms is not None:
            avg_response_time_ms = latency_ms if prev_avg_latency is None else \
                (float(prev_avg_latency) * (message_count - 1) + latency_ms) / message_count

        topics = list(profile.get("interested_topics") or [])
        for t in (conversation_fields.get("broad_intent"), conversation_fields.get("actionable_intent")):
            if t and t not in ("unknown", None) and t not in topics:
                topics.append(t)
        topics = topics[-20:]  # cap — most-recent 20 distinct topics, not a full history

        routing_type = conversation_fields.get("routing_type")
        erp_requests_count = (profile.get("erp_requests_count") or 0) + (
            1 if routing_type in ("API", "WEBHOOK", "HYBRID") and conversation_fields.get("erp_request") else 0)

        alert = decide_result.get("alert") or {}
        escalated = bool(conversation_fields.get("escalated"))
        is_complaint = alert.get("alert_type") in _COMPLAINT_ALERT_TYPES
        # Coarse, deterministic proxy for sentiment (no real sentiment
        # model in this codebase yet) — negative = a detected complaint/
        # legal-threat keyword OR an escalation; positive = the turn
        # produced a real answer with neither signal. Documented as an
        # approximation, not true sentiment analysis.
        reply_text = (decide_result.get("reply") or {}).get("text") or ""
        is_negative = is_complaint or escalated
        is_positive = (not is_negative) and bool(reply_text.strip()) and not decide_result.get("error")

        row = {
            "message_count": message_count,
            "conversation_count": conversation_count,
            "avg_confidence": avg_confidence,
            "avg_response_time_ms": avg_response_time_ms,
            "interested_topics": topics,
            "erp_requests_count": erp_requests_count,
            "complaint_count": (profile.get("complaint_count") or 0) + (1 if is_complaint else 0),
            "positive_count": (profile.get("positive_count") or 0) + (1 if is_positive else 0),
            "negative_count": (profile.get("negative_count") or 0) + (1 if is_negative else 0),
            "escalation_count": (profile.get("escalation_count") or 0) + (1 if escalated else 0),
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        if not profile.get("first_seen"):
            row["first_seen"] = datetime.now(timezone.utc).isoformat()

        # Identifier Memory persistence REMOVED (Task 06, 2026-08-26).
        # Customer Intelligence V1 (2026-08-15) used to write a customer-
        # TYPED CustCode/OrderCode/ShipmentCode/Tracking into this row as a
        # convenience cache, keyed only by line_user_id, with no ownership
        # verification -- meaning anyone who once typed another person's
        # identifier in chat had it permanently remembered and auto-applied
        # to future Business Action calls on THIS line_user_id ("type it
        # once, exploit forever"). A user-supplied identifier is never
        # proof of account ownership (IDENTIFIER != AUTHORIZATION -- see
        # services/authorization_service.py), so it must never be persisted
        # here as if it were. cust_code/last_order_code/last_shipment_code/
        # last_tracking are intentionally left untouched by this function
        # going forward; existing rows written before this fix are a
        # separate data-cleanup concern, not addressed by this code change.

        # Conversation Resolver (Final Conversational Correctness,
        # 2026-08-15; migration 039) -- remembers which Business Action
        # the customer was last genuinely using, so a topic-word-free
        # follow-up reference ("แล้วของถึงหรือยัง") can resume it (see
        # services/decision_engine.py::_resolve_conversation_reference).
        # Only a real ERP execution counts -- never overwritten by a RAG
        # answer or a WORKFLOW clarification question, and, like every
        # other identifier-memory field, never cleared just because this
        # turn didn't execute one.
        if conversation_fields.get("routing_type") in ("API", "WEBHOOK") and conversation_fields.get("selected_business_action"):
            row["last_business_action"] = conversation_fields["selected_business_action"]

        primary_intent = conversation_fields.get("actionable_intent") or conversation_fields.get("broad_intent")
        if primary_intent and primary_intent != "unknown":
            row["primary_intent"] = primary_intent

        res = supabase.table(TABLE).update(row).eq("line_user_id", line_user_id).execute()
        return (res.data or [None])[0]
    except Exception as e:
        print(f"❌ update_profile_from_turn: {e}")
        return None
