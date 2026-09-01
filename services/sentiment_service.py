"""P4.1 — Negative customer detection + one immediate Admin LINE alert.

An ANALYTICAL side-channel, SEPARATE from the P4 lead stage. Sentiment is
NORMAL | NEGATIVE and never becomes a fourth lead stage — lead_stage and
sentiment_status persist independently.

Detection is deterministic (no LLM, no network for the decision). It
REUSES the pipeline's existing negative signals —
services/customer_tier_service.py::classify_message_stage (which already
folds in decision_engine._COMPLAINT_RE / _LEGAL_THREAT_RE and
slot_filling_engine._HUMAN_REQUEST_RE) — and adds a small bounded layer
for dissatisfaction the base classifier does not name (repeated wrong
answers, long waits, unresolved issues, strong negative language).

Negation is NOT negative sentiment: a leading correction ("ไม่ได้ถาม…"),
a reassurance ("ไม่เป็นไร"), or a factual question that merely contains
"ไม่ได้/ไม่มี" ("น้ำหอมนำเข้าไม่ได้ใช่ไหม") stays NORMAL.

Alert: on a NORMAL -> NEGATIVE transition (or a genuinely new incident
after a 24h cooldown) ONE LINE message is pushed to Admin through the
SAME configured NOTIFY Business Action services/human_handoff_service.py
already uses (SendLineNotiCS) — no new LINE client, no hardcoded
recipient. Never spams: bounded by negative_last_alert_at.
"""
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

SENTIMENTS = ("NORMAL", "NEGATIVE")

# Bounded machine reason keys (never conversation text).
NEGATIVE_REASONS = (
    "complaint", "repeated_wrong_answer", "unresolved_issue",
    "service_dissatisfaction", "human_requested",
    "business_action_failed_repeatedly", "strong_negative_language",
)
_MAX_REASONS = 6
_INCIDENT_COOLDOWN_HOURS = 24.0     # reset + re-alert boundary
_ALERT_COOLDOWN_HOURS = 24.0

REASON_LABELS_TH: Dict[str, str] = {
    "complaint": "ลูกค้าร้องเรียน",
    "repeated_wrong_answer": "แจ้งว่าระบบตอบผิดซ้ำ",
    "unresolved_issue": "ปัญหายังไม่ได้รับการแก้ไข",
    "service_dissatisfaction": "ไม่พอใจการบริการ (รอนาน/ตอบไม่ตรง)",
    "human_requested": "ขอคุยกับเจ้าหน้าที่",
    "business_action_failed_repeatedly": "ระบบทำรายการให้ล้มเหลวซ้ำ",
    "strong_negative_language": "ใช้ถ้อยคำไม่พอใจรุนแรง",
}

# ── Negation / correction / reassurance — force NORMAL ────────────────
_NON_NEGATIVE_GUARD_RE = re.compile(
    r"ไม่เป็นไร|ไม่ได้ถาม|ไม่ได้หมายถึง|ไม่ได้จะ|ไม่ได้ว่า|ไม่ได้พูด|เข้าใจผิดเอง|"
    r"พิมพ์ผิดเอง|ถามผิดเอง")

# ── P4.1 dissatisfaction layer (semantic, not an exhaustive dictionary) ─
_WRONG_ANSWER_RE = re.compile(
    r"ตอบผิด|ตอบไม่ตรง|ตอบมั่ว|ตอบไม่ถูก|ตอบไม่โอเค|ให้ข้อมูลผิด|บอกผิด|ข้อมูลผิด|ตอบไม่รู้เรื่อง")
_REPEAT_CUE_RE = re.compile(
    r"อีกแล้ว|อีกรอบ|อีกครั้ง|ตลอด|ทุกที|ทุกครั้ง|ซ้ำ ?ๆ?|หลายรอบ|หลายครั้ง|กี่รอบแล้ว")
_WAIT_RE = re.compile(
    r"รอนาน|รอมานาน|นานมากแล้ว|ช้ามาก|ช้าจัง|เมื่อไหร่จะเสร็จ|เมื่อไรจะเสร็จ|"
    r"เมื่อไหร่จะได้|ยังไม่เสร็จสักที")
_UNRESOLVED_RE = re.compile(
    r"ยังไม่ได้เรื่อง|ยังไม่ได้รับการแก้|ยังไม่แก้|แก้[^\n]{0,12}ยังไม่ได้|"
    r"ยังไม่เรียบร้อยสักที|ยังไม่ได้สักที|ทำไมยังไม่|ยังผิดอยู่|ยังไม่หายสักที")
_STRONG_NEG_RE = re.compile(
    r"แย่มาก|ห่วยมาก|บริการแย่|ไม่โอเคเลย|ผิดหวังมาก|รับไม่ได้|เลวร้าย|โคตรแย่|"
    r"ไม่ไหวแล้ว|งานไม่ได้เรื่อง|ทำงานไม่ได้เรื่อง")
_COMPLAINT_INTENT_RE = re.compile(r"ร้องเรียน|เรียกร้องค่าเสียหาย|จะเอาเรื่อง")

try:
    from services.slot_filling_engine import _HUMAN_REQUEST_RE
except Exception:                                   # pragma: no cover - defensive
    _HUMAN_REQUEST_RE = re.compile(r"คุยกับเจ้าหน้าที่|ขอคุยกับคน|ติดต่อเจ้าหน้าที่")

try:
    from services.lead_stage_service import is_real_line_user_id
except Exception:                                   # pragma: no cover - defensive
    _RID = re.compile(r"^U[0-9a-f]{32}$")
    def is_real_line_user_id(x):                    # type: ignore
        return bool(_RID.match(x or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_since(ts: Optional[str], now: datetime) -> Optional[float]:
    d = _parse_ts(ts)
    return None if d is None else (now - d).total_seconds() / 3600.0


# ── Detection (pure) ────────────────────────────────────────────────

def detect_negative_reasons(question: str, *, action_failed_repeatedly: bool = False) -> List[str]:
    """Machine reason keys for meaningful dissatisfaction this turn, or []
    for NORMAL. Pure; no I/O."""
    q = question or ""
    if _NON_NEGATIVE_GUARD_RE.search(q):
        return []

    reasons: List[str] = []

    def add(r):
        if r not in reasons:
            reasons.append(r)

    # Reuse the existing base classifier (complaint / legal threat / human request).
    try:
        from services.customer_tier_service import classify_message_stage
        base = classify_message_stage(q)
    except Exception:                              # pragma: no cover - defensive
        base = {"stage": "cold", "reason": ""}
    if base.get("stage") == "negative":
        if "human" in (base.get("reason") or "").lower():
            add("human_requested")
        add("complaint")

    if _WRONG_ANSWER_RE.search(q):
        add("repeated_wrong_answer" if _REPEAT_CUE_RE.search(q) else "service_dissatisfaction")
    if _WAIT_RE.search(q):
        add("service_dissatisfaction")
    if _UNRESOLVED_RE.search(q):
        add("unresolved_issue")
    if _STRONG_NEG_RE.search(q):
        add("strong_negative_language")
    if _COMPLAINT_INTENT_RE.search(q):
        add("complaint")
    if _HUMAN_REQUEST_RE.search(q):
        add("human_requested")

    # An internal technical failure alone is NOT dissatisfaction — only
    # counts when the customer also expressed something negative this turn.
    if action_failed_repeatedly and reasons:
        add("business_action_failed_repeatedly")

    return reasons


def _merge_reasons(prev: Optional[List[str]], new: List[str]) -> List[str]:
    out: List[str] = []
    for r in list(new) + list(prev or []):
        if r in NEGATIVE_REASONS and r not in out:
            out.append(r)
    return out[:_MAX_REASONS]


def reason_labels(reasons: Optional[List[str]]) -> List[str]:
    return [REASON_LABELS_TH.get(r, r) for r in (reasons or []) if r]


# ── Admin LINE alert (reuses the configured NOTIFY Business Action) ────

def _format_alert(*, display_name: Optional[str], cust_code: Optional[str],
                  lead_stage: Optional[str], reasons: List[str],
                  last_message: str, when: datetime) -> str:
    reason_txt = "; ".join(reason_labels(reasons)) or "ตรวจพบความไม่พอใจของลูกค้า"
    msg = (last_message or "").strip().replace("\n", " ")
    if len(msg) > 300:
        msg = msg[:300] + "…"
    ts = when.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    return "\n".join([
        "⚠️ ลูกค้าต้องการการดูแล",
        "",
        f"ลูกค้า: {display_name or '—'}",
        f"CustCode: {cust_code or '—'}",
        f"Lead Stage: {(lead_stage or 'COLD').upper()}",
        "Sentiment: NEGATIVE",
        "",
        f"เหตุผล: {reason_txt}",
        "",
        "ข้อความล่าสุด:",
        f"\"{msg}\"",
        "",
        f"เวลา: {ts}",
    ])


def send_admin_negative_alert(*, display_name: Optional[str], cust_code: Optional[str],
                              lead_stage: Optional[str], reasons: List[str],
                              last_message: str, when: Optional[datetime] = None) -> Dict:
    """One concise LINE push to Admin via the SAME NOTIFY Business Action
    (SendLineNotiCS) services/human_handoff_service.py uses — no new LINE
    client, recipient lives in that action's own config. Returns
    {"sent": bool, "error": str|None}. Never raises."""
    when = when or _now()
    text = _format_alert(display_name=display_name, cust_code=cust_code, lead_stage=lead_stage,
                         reasons=reasons, last_message=last_message, when=when)
    try:
        from services.human_handoff_service import find_notification_action
        from services.action_executor import get_action_executor
        action = find_notification_action()
        if not action:
            return {"sent": False, "error": "no_notification_action_configured"}
        result = get_action_executor().execute(
            action["id"], context={"collected_slots": {"Message": text}})
        if result.get("status") == "success":
            return {"sent": True, "error": None}
        return {"sent": False, "error": result.get("error") or "execution_failed"}
    except Exception as e:                          # pragma: no cover - delivery best-effort
        return {"sent": False, "error": f"{type(e).__name__}: {e}"}


# ── Per-turn update + persistence ────────────────────────────────────

def _decide(prev_status: str, prev_reasons: List[str], turn_reasons: List[str],
            negative_last_detected_at: Optional[str], negative_last_alert_at: Optional[str],
            now: datetime) -> Dict:
    """Pure transition logic. Returns
    {status, reasons, is_new_incident, should_alert, detected_now}."""
    prev_status = (prev_status or "NORMAL").upper()

    if turn_reasons:
        merged = _merge_reasons(prev_reasons, turn_reasons)
        alert_age = _hours_since(negative_last_alert_at, now)
        is_new_incident = (prev_status != "NEGATIVE")
        should_alert = (prev_status != "NEGATIVE"
                        or alert_age is None
                        or alert_age >= _ALERT_COOLDOWN_HOURS)
        return {"status": "NEGATIVE", "reasons": merged, "detected_now": True,
                "is_new_incident": is_new_incident, "should_alert": should_alert}

    # No negative signal this turn.
    if prev_status == "NEGATIVE":
        idle = _hours_since(negative_last_detected_at, now)
        if idle is not None and idle >= _INCIDENT_COOLDOWN_HOURS:
            return {"status": "NORMAL", "reasons": [], "detected_now": False,
                    "is_new_incident": False, "should_alert": False}
        # retention — stay NEGATIVE, keep existing reasons
        return {"status": "NEGATIVE", "reasons": list(prev_reasons or []),
                "detected_now": False, "is_new_incident": False, "should_alert": False}

    return {"status": "NORMAL", "reasons": [], "detected_now": False,
            "is_new_incident": False, "should_alert": False}


def update_sentiment_from_turn(line_user_id: str, *, question: str = "",
                               display_name: Optional[str] = None, cust_code: Optional[str] = None,
                               lead_stage: Optional[str] = None,
                               action_failed_repeatedly: bool = False,
                               handoff_active: bool = False,
                               now: Optional[datetime] = None) -> Optional[Dict]:
    """Detect this real LINE turn's sentiment, persist
    sentiment_status / sentiment_reasons / sentiment_updated_at /
    negative_last_detected_at / negative_last_alert_at, and send ONE Admin
    LINE alert on a NORMAL->NEGATIVE transition or a post-cooldown new
    incident. Real LINE users only. Never raises (detached post-reply
    thread). Returns the persisted dict, or None if skipped.

    `handoff_active` = this turn already escalated to Human Handoff
    (line_bot/webhook.py routing_type == "HUMAN_HANDOFF"), which sends its
    own CS notification through the SAME NOTIFY Business Action. When true
    the P4.1 alert is suppressed and the incident is marked already-
    alerted (negative_last_alert_at) so exactly ONE notification per
    incident reaches CS — never two."""
    try:
        if not is_real_line_user_id(line_user_id):
            return None
        now = now or _now()

        turn_reasons = detect_negative_reasons(
            question, action_failed_repeatedly=action_failed_repeatedly)

        from profiles.manager import get_profile, supabase, TABLE, _profile_cache_clear
        profile = get_profile(line_user_id) or {}
        prev_reasons = profile.get("sentiment_reasons") or []
        if isinstance(prev_reasons, str):
            import json as _json
            try:
                prev_reasons = _json.loads(prev_reasons)
            except Exception:
                prev_reasons = []

        lead_stage = lead_stage or profile.get("lead_stage") or "COLD"

        d = _decide(profile.get("sentiment_status") or "NORMAL", prev_reasons, turn_reasons,
                    profile.get("negative_last_detected_at"),
                    profile.get("negative_last_alert_at"), now)

        row: Dict = {
            "sentiment_status": d["status"],
            "sentiment_reasons": d["reasons"],
            "sentiment_updated_at": now.isoformat(),
        }
        if d["detected_now"]:
            row["negative_last_detected_at"] = now.isoformat()

        # Human Handoff already notified CS for this turn (same NOTIFY
        # action) — suppress the P4.1 alert, but record the incident as
        # alerted so a follow-up negative turn within 24h stays quiet too.
        alert_result = {"sent": False, "error": "not_attempted"}
        if d["detected_now"] and handoff_active:
            row["negative_last_alert_at"] = now.isoformat()
            alert_result = {"sent": False, "error": "covered_by_human_handoff"}
        elif d["should_alert"]:
            alert_result = send_admin_negative_alert(
                display_name=display_name, cust_code=cust_code, lead_stage=lead_stage,
                reasons=d["reasons"], last_message=question, when=now)
            if alert_result.get("sent"):
                row["negative_last_alert_at"] = now.isoformat()
            else:
                print(f"[sentiment_service] admin alert not delivered "
                      f"({alert_result.get('error')}) — will retry on next negative turn")

        try:
            _profile_cache_clear(line_user_id)
            supabase.table(TABLE).update(row).eq("line_user_id", line_user_id).execute()
        except Exception as e:                      # pragma: no cover - persistence best-effort
            print(f"[sentiment_service] persist failed: {e}")

        return {**row, "alerted": bool(alert_result.get("sent")),
                "alert_error": alert_result.get("error")}
    except Exception as e:                          # pragma: no cover - never affect the turn
        print(f"[sentiment_service] update_sentiment_from_turn failed (non-fatal): {e}")
        return None
