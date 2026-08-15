"""Customer Tier Service — Phase 3.3 (2026-08-05, Conversation Intelligence
sprint). Deterministic, non-AI classification of a LINE customer's
CONVERSATION behavior into exactly 4 tiers: cold / warm / hot / negative.

This is a SEPARATE axis from profiles/manager.py::calc_segment() (the
existing purchase/order-based cold/warm/hot field) — see
migrations/035_phase3_conversation_intelligence.sql for why the two are
kept independent (segment answers "has this person bought before?";
conversation_tier answers "how is this conversation relationship going?").
Drives Prompt Studio's Customer Tier Prompt selection
(services/prompt_builder.py::get_active_prompt_for_tier).

No AI model computes this score — every input is a plain counter already
accumulated by profiles/manager.py::update_profile_from_turn(), combined
with a fixed, documented formula (per CLAUDE.md: deterministic
post-processing must stay separably labeled from AI-generated output).
Re-evaluated after every conversation turn, so a tier can move in either
direction as behavior changes — it never needs a human to manually
reclassify a customer.
"""
import re
from typing import Dict, Optional

TIERS = ("cold", "warm", "hot", "negative")

# Tunable, documented weights — not derived from any model, safe to
# adjust here if real production data shows the bands are miscalibrated.
_FREQUENCY_WEIGHT = 2.0       # per conversation (capped at 10)
_ERP_USAGE_WEIGHT = 1.0       # per ERP-backed request (capped at 10) — proxy for purchase intent
_TOPIC_BREADTH_WEIGHT = 1.0   # per distinct topic asked about (capped at 10)
_POSITIVE_WEIGHT = 3.0        # per turn classified positive (see profiles/manager.py)
_NEGATIVE_WEIGHT = 5.0        # per turn classified negative
_COMPLAINT_WEIGHT = 10.0      # per detected complaint/legal-threat keyword hit

_HOT_THRESHOLD = 15.0
_WARM_THRESHOLD = 5.0


def compute_tier(profile: Dict) -> Dict:
    """Returns {"tier": str, "score": float} from a user_profiles row.
    Never raises — a malformed/partial profile just yields the lowest
    tier ("cold") with score 0.

    A customer with any complaint (and not clearly outweighed by positive
    turns), or more negative than positive turns overall, is always
    "negative" regardless of how frequently they engage — per spec,
    "Complaint should simply increase the Negative score," never let
    frequency alone promote a complaining customer into "hot"."""
    conversation_count = profile.get("conversation_count") or 0
    erp_requests_count = profile.get("erp_requests_count") or 0
    topic_count = len(profile.get("interested_topics") or [])
    positive_count = profile.get("positive_count") or 0
    negative_count = profile.get("negative_count") or 0
    complaint_count = profile.get("complaint_count") or 0

    score = (
        _FREQUENCY_WEIGHT * min(conversation_count, 10)
        + _ERP_USAGE_WEIGHT * min(erp_requests_count, 10)
        + _TOPIC_BREADTH_WEIGHT * min(topic_count, 10)
        + _POSITIVE_WEIGHT * positive_count
        - _NEGATIVE_WEIGHT * negative_count
        - _COMPLAINT_WEIGHT * complaint_count
    )

    if (complaint_count > 0 and negative_count >= positive_count) or \
       (negative_count > positive_count and negative_count > 0):
        tier = "negative"
    elif score >= _HOT_THRESHOLD:
        tier = "hot"
    elif score >= _WARM_THRESHOLD:
        tier = "warm"
    else:
        tier = "cold"

    return {"tier": tier, "score": round(score, 2)}


def update_tier_for_profile(line_user_id: str, *, message: Optional[str] = None) -> Dict:
    """Re-computes and persists the tier for a LINE user, reading the
    freshest profile row (so it reflects stats update_profile_from_turn()
    just wrote in the same request). Never raises.

    Customer Intelligence V1 (2026-08-15) — when `message` (the CURRENT
    turn's text) is supplied, also runs classify_message_stage() and
    merges it with the existing aggregate compute_tier() above (see
    _merge_stage for the exact rule), then persists stage_reason/
    stage_confidence/handoff_recommended/handoff_reason alongside the
    SAME conversation_tier/tier_score columns this function already
    wrote — no new stage column, no second cold/warm/hot/negative axis."""
    from profiles.manager import get_profile, supabase, TABLE
    profile = get_profile(line_user_id) or {}
    aggregate = compute_tier(profile)
    result = dict(aggregate)
    update = {"conversation_tier": aggregate["tier"], "tier_score": aggregate["score"]}

    if message is not None:
        message_stage = classify_message_stage(message)
        merged = _merge_stage(aggregate["tier"], message_stage)
        result = {"tier": merged["stage"], "score": aggregate["score"],
                   "confidence": merged["confidence"], "reason": merged["reason"]}
        handoff = compute_handoff_recommendation(merged["stage"], message)
        update.update({
            "conversation_tier": merged["stage"],
            "stage_reason": merged["reason"], "stage_confidence": merged["confidence"],
            "handoff_recommended": handoff["recommended"], "handoff_reason": handoff["reason"],
        })

    try:
        supabase.table(TABLE).update(update).eq("line_user_id", line_user_id).execute()
    except Exception as e:
        print(f"[customer_tier_service] update_tier_for_profile failed: {e}")
    return result


# ── Customer Intelligence V1 (2026-08-15), Phase 5/6 — per-message stage
# classification + Handoff Recommendation metadata. Deterministic,
# keyword-driven only (no AI model) — reuses the SAME complaint/legal-
# threat/explicit-human-request signals services/decision_engine.py and
# services/slot_filling_engine.py already use elsewhere in this codebase,
# rather than maintaining a second copy of those keyword lists. ─────────

# Complaint/distress phrasing not already covered by decision_engine.py's
# own _COMPLAINT_RE/_LEGAL_THREAT_RE (which this module deliberately
# reuses rather than duplicating -- see classify_message_stage below).
# Checked ALONGSIDE those, before the HOT check, so e.g. "ของหายค่ะ
# รบกวนติดต่อกลับด้วย" (item lost, please call back) is never
# misclassified as hot just because it also asks for a callback.
_NEGATIVE_STRONG_EXTRA_RE = re.compile(
    r"ของหาย|พัสดุหาย|สินค้าหาย|ติดต่อใครไม่ได้|ติดต่อไม่ได้เลย|ไม่มีใครรับสาย|ไม่มีใครตอบ",
    re.IGNORECASE,
)
_NEGATIVE_MILD_RE = re.compile(r"ยังไม่ถึง|ยังไม่ได้รับ|ไม่มาถึงสักที|ของช้า", re.IGNORECASE)
_HOT_RE = re.compile(
    r"สนใจ|อยากเริ่ม|อยากใช้บริการ|อยากเป็นลูกค้า|ขอราคา|ขอใบเสนอราคา|สมัครใช้บริการ",
    re.IGNORECASE,
)
# A narrower sub-signal of _HOT_RE -- an explicit ask for a SALES
# callback specifically, not just general interest and not just any
# mention of "contact/call back" (a bare "ติดต่อกลับ"/"โทรกลับ" also
# appears in unrelated contexts, e.g. an agent explicitly invoking the
# SendLineNotiCS Business Action itself -- "ช่วยแจ้ง CS ว่าลูกค้าต้องการ
# ให้ติดต่อกลับ" -- which must NOT be hijacked into a Customer
# Intelligence handoff before ever reaching that action's own
# confirmation-gate flow). Requiring "เซลส์" keeps this specific to the
# spec's own worked examples ("ให้เซลส์โทรกลับ", "ขอให้เซลส์ติดต่อกลับ").
_HOT_CONTACT_REQUEST_RE = re.compile(r"เซลส์.{0,4}ติดต่อ|ให้เซลส์", re.IGNORECASE)
_WARM_RE = re.compile(
    r"ค่าบริการ|ค่าใช้จ่าย|คิดค่า|คิดราคา|ค่าขนส่ง|ระยะเวลา|ใช้เวลานาน"
    r"|ขั้นตอน|วิธีการนำเข้า|นำเข้า.{0,10}ยังไง|นำเข้า.{0,10}อย่างไร",
    re.IGNORECASE,
)

_STAGE_RANK = {"cold": 0, "warm": 1, "hot": 2, "negative": 3}


def classify_message_stage(message: str) -> Dict:
    """Customer Stage Analysis for the CURRENT message only (Phase 5) --
    a SEPARATE, per-turn signal from compute_tier()'s whole-relationship
    aggregate score above. Returns {"stage", "confidence", "reason"}.
    Fixed rule order (never a probabilistic/AI classifier, so the reason
    string is always exactly why this message got this stage):
    legal threat / complaint / explicit human request -> negative
    (strong); a shipment-not-arrived-yet concern with no complaint
    wording -> negative (mild, does NOT imply handoff -- see
    compute_handoff_recommendation); clear purchase/contact intent ->
    hot; service-cost/process evaluation -> warm; anything else -> cold."""
    message = message or ""
    from services.decision_engine import _COMPLAINT_RE, _LEGAL_THREAT_RE
    from services.slot_filling_engine import _HUMAN_REQUEST_RE

    if _LEGAL_THREAT_RE.search(message) or _COMPLAINT_RE.search(message) or _HUMAN_REQUEST_RE.search(message) \
       or _NEGATIVE_STRONG_EXTRA_RE.search(message):
        return {"stage": "negative", "confidence": 0.9,
                "reason": "complaint, dissatisfaction, or an explicit request to speak with a human agent"}
    if _NEGATIVE_MILD_RE.search(message):
        return {"stage": "negative", "confidence": 0.6,
                "reason": "customer flagged a possible service issue (e.g. shipment not yet arrived) "
                          "without an explicit complaint"}
    if _HOT_RE.search(message) or _HOT_CONTACT_REQUEST_RE.search(message):
        return {"stage": "hot", "confidence": 0.85,
                "reason": "clear purchase, sign-up, or contact intent"}
    if _WARM_RE.search(message):
        return {"stage": "warm", "confidence": 0.7,
                "reason": "specific interest in service cost or process -- evaluating the service"}
    return {"stage": "cold", "confidence": 0.5,
            "reason": "general information question, no purchase or complaint signal detected"}


def _merge_stage(aggregate_tier: str, message_result: Dict) -> Dict:
    """Merges the whole-relationship aggregate tier with THIS message's
    immediate stage. A decisive single-message signal (hot or negative)
    always wins outright -- a brand-new customer's very first "สนใจใช้
    บริการ ขอให้เซลส์ติดต่อกลับ" must read as hot immediately, it can't
    wait for enough accumulated history to raise the aggregate score.
    Otherwise (message is cold/warm) an already-earned higher aggregate
    tier is never downgraded by one neutral follow-up message."""
    stage = message_result["stage"]
    if stage in ("hot", "negative"):
        return message_result
    if _STAGE_RANK.get(aggregate_tier, 0) > _STAGE_RANK.get(stage, 0):
        return {"stage": aggregate_tier, "confidence": 0.5,
                "reason": f"this message alone reads as {stage}, kept at the customer's existing "
                          f"'{aggregate_tier}' relationship-level tier"}
    return message_result


def compute_handoff_recommendation(stage: str, message: str) -> Dict:
    """Handoff Recommendation METADATA only (Phase 6) -- never sends a
    real notification itself; services/human_handoff_service.py (via the
    Decision Engine's own HUMAN_HANDOFF routing) still owns that. Customer
    Stage != Human Handoff: hot does not automatically mean handoff, and
    negative does not automatically mean handoff unless the message
    itself complains strongly or explicitly asks for a person -- a
    shipment-status question a Business Action can resolve is not
    escalated just because its stage is negative."""
    message = message or ""
    from services.decision_engine import _COMPLAINT_RE, _LEGAL_THREAT_RE
    from services.slot_filling_engine import _HUMAN_REQUEST_RE

    if stage == "hot" and _HOT_CONTACT_REQUEST_RE.search(message):
        return {"recommended": True,
                "reason": "customer explicitly asked for a callback/contact while showing strong purchase intent"}
    if stage == "negative" and (_HUMAN_REQUEST_RE.search(message) or _LEGAL_THREAT_RE.search(message)
                                 or _COMPLAINT_RE.search(message) or _NEGATIVE_STRONG_EXTRA_RE.search(message)):
        return {"recommended": True,
                "reason": "complaint or explicit request to speak with a human agent"}
    if stage == "negative":
        return {"recommended": False,
                "reason": "service concern that an ERP/Business Action lookup can likely resolve"}
    if stage == "hot":
        return {"recommended": False,
                "reason": "strong interest but no explicit contact/callback request yet"}
    return {"recommended": False,
            "reason": "no purchase or complaint signal strong enough to warrant escalation"}
