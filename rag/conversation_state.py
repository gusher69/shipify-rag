"""Conversation Intelligence Phase 1 — Conversation Memory & State Engine.

A thin, deterministic (no LLM call) layer inserted BETWEEN Conversation
Resolver 2.0 (rag/query_resolution.py::resolve_conversation — entity
tracking/follow-up resolution, unchanged) and Canonical Query Rewrite
(rag/canonical_query.py::rewrite_canonical_query, unchanged). It never
re-implements entity extraction or follow-up detection; it adds exactly
ONE new thing neither existing module tracks: a normalized conversation
TOPIC BUCKET (warehouse/shipping/payment/tracking/promotion/coupon/
order/erp/invoice/product/unknown) plus an explicit topic-transition
decision (new/same/switch), so the Playground's Explainability tab and
the Answer/Attachment Planners can reason about "what is this
conversation about right now" without re-deriving it from raw text.

Retrieval, Prompt Studio, and AI Policies are untouched — this module is
read-only with respect to those; its only side effect on the pipeline is
that services/playground_orchestrator.py uses its (topic-switch-aware)
entities to build the dict passed into rewrite_canonical_query(), instead
of naively carrying every entity forward regardless of topic.
"""
import re
from typing import Dict, List, Optional

from rag.query_resolution import resolve_conversation, extract_entities, accumulate_entities

TOPIC_BUCKETS = (
    "warehouse", "shipping", "payment", "tracking", "promotion", "coupon",
    "order", "erp", "invoice", "product", "unknown",
)

# Checked in this order — narrower/more distinctive vocabulary first, so
# e.g. a "ค่าส่ง" (shipping fee) question is never misread as "payment"
# just because both concern money, and "ใบกำกับภาษี" (tax invoice) is never
# misread as generic "payment" just because both concern money/billing.
_TOPIC_BUCKET_PATTERNS: Dict[str, re.Pattern] = {
    "promotion": re.compile(r"โปรโมชั่น|โปรโม|promotion", re.IGNORECASE),
    "coupon": re.compile(r"คูปอง|ส่วนลด|โค้ดส่วนลด", re.IGNORECASE),
    "invoice": re.compile(r"ใบกำกับ|ใบเสร็จ|ภาษี|\bvat\b", re.IGNORECASE),
    "tracking": re.compile(r"ติดตามพัสดุ|เช็คสถานะ|ตรวจสอบสถานะ|เลขพัสดุ|tracking", re.IGNORECASE),
    "payment": re.compile(
        r"จ่ายบิล|ชำระบิล|ชำระเงิน|วิธีจ่าย|วิธีชำระ|บัตรเครดิต|ค่าธรรมเนียม|บัตร",
        re.IGNORECASE,
    ),
    "erp": re.compile(r"\berp\b", re.IGNORECASE),
    "order": re.compile(r"สั่งซื้อ|ออเดอร์|\border\b", re.IGNORECASE),
    "warehouse": re.compile(r"โกดัง|warehouse", re.IGNORECASE),
    "shipping": re.compile(
        r"เรท|ขนส่ง|ค่าส่ง|ทางเรือ|ทางรถ|ทางอากาศ|ทางเครื่องบิน|shipping", re.IGNORECASE,
    ),
    "product": re.compile(r"สินค้า|product", re.IGNORECASE),
}
_TOPIC_BUCKET_ORDER = ["promotion", "coupon", "invoice", "tracking", "payment",
                       "erp", "order", "warehouse", "shipping", "product"]

# Fallback bucket when NEITHER the current nor the previous turn's raw
# text names an explicit topic keyword, but the Unified Intent
# Classifier (rag/query_understanding.py) still inferred a specific
# actionable_intent from tracked entities alone.
_TOPIC_BUCKET_BY_INTENT = {
    "warehouse_location": "warehouse", "warehouse_map": "warehouse", "warehouse_contact": "warehouse",
    "shipping_rate": "shipping", "shipping_duration": "shipping", "shipping_calculation": "shipping",
    "payment_instruction": "payment", "payment_policy": "payment",
    "coupon_policy": "coupon", "invoice_policy": "invoice",
    "prohibited_goods": "product", "tracking_status": "tracking",
}

_SUBTOPIC_BY_ACTIONABLE_INTENT = {
    "warehouse_map": "map",
    "warehouse_location": "location",
    "warehouse_contact": "contact",
    "shipping_rate": "rate",
    "shipping_duration": "duration",
    "shipping_calculation": "calculation",
    "payment_instruction": "instruction",
    "payment_policy": "policy",
    "coupon_policy": "policy",
    "invoice_policy": "policy",
    "prohibited_goods": "prohibited_list",
    "tracking_status": "status",
    "attachment_request": "attachment",
    "human_agent_request": "handoff",
    "summary": "summary",
}


def detect_topic_bucket(text: Optional[str]) -> Optional[str]:
    """Returns the first matching topic bucket for `text` ALONE (no
    conversation context) — or None if nothing matches. None means "this
    turn names no explicit topic of its own," never "unknown topic";
    callers use None to mean "inherit whatever topic was already
    active," which is the whole point of a follow-up in the first place.
    """
    if not text:
        return None
    for bucket in _TOPIC_BUCKET_ORDER:
        if _TOPIC_BUCKET_PATTERNS[bucket].search(text):
            return bucket
    return None


def subtopic_for(actionable_intent: Optional[str], attribute: Optional[str]) -> Optional[str]:
    """A finer-grained label under the topic bucket (e.g. topic=warehouse,
    subtopic=map/location/contact) — derived from the actionable_intent
    already computed by rag/query_understanding.py::classify_actionable_intent
    (never a second classifier); falls back to the raw `attribute` entity
    (rag/query_resolution.py) when no specific mapping exists."""
    if actionable_intent in _SUBTOPIC_BY_ACTIONABLE_INTENT:
        return _SUBTOPIC_BY_ACTIONABLE_INTENT[actionable_intent]
    return attribute


def build_conversation_state(question: str, history: Optional[List[Dict]] = None) -> Dict:
    """The full Conversation State for this turn, built ENTIRELY from
    modules that already exist:
      - rag/query_resolution.py::resolve_conversation() for follow-up
        detection, entity tracking, negation/exclusion, and replacement.
      - This module's own detect_topic_bucket()/subtopic_for() add the
        one genuinely new thing: a normalized topic bucket + explicit
        transition label.

    Callers that also run Unified Intent Classification afterward (e.g.
    services/playground_orchestrator.py, which needs the FINAL canonical
    question for that step) should overwrite the returned "intent"/
    "subtopic" fields with that later, more precise result — this
    function's own "intent" is a same-turn approximation good enough for
    the topic-transition decision and for Explainability before that
    later step runs.

    Returns:
        {
          "topic": str,                 # one of TOPIC_BUCKETS
          "subtopic": Optional[str],
          "intent": str,                 # actionable_intent (approx.)
          "location": Optional[str], "warehouse": Optional[str],
          "transport": Optional[str], "payment": Optional[str],
          "order_type": None, "tracking": None, "product": None,  # not
              yet tracked by any existing extractor — see Known Limitations
          "resolved_question": str,
          "state_changes": {slot: {"from":..., "to":...}, ...},
          "excluded_entities": {"location": [...], "transport": [...]},
          "transition": "new_topic" | "same_topic" | "switch_topic" | "unknown",
          "conversation_confidence": {
              "topic_confidence": float, "entity_confidence": float,
              "transition_confidence": float, "overall": float,
          },
        }
    """
    conversation = resolve_conversation(question, history)

    merged_entities: Dict[str, Optional[str]] = dict(conversation.get("entities_carried", {}))
    current_only_entities = extract_entities(question)
    for key, value in current_only_entities.items():
        if value:
            merged_entities[key] = value  # current wording always wins — same priority as every other module

    current_bucket = detect_topic_bucket(question)
    prev_bucket = detect_topic_bucket(conversation.get("prev_topic"))

    if current_bucket:
        if prev_bucket and current_bucket != prev_bucket:
            transition = "switch_topic"
        elif prev_bucket:
            transition = "same_topic"
        else:
            transition = "new_topic"
        topic = current_bucket
    elif prev_bucket:
        transition = "same_topic"
        topic = prev_bucket
        # The current turn names no explicit topic/attribute of its own
        # (e.g. "ขอเบอร์" alone doesn't match resolve_conversation()'s own
        # follow-up-marker vocabulary, so entities_carried came back
        # empty) — since the topic is staying the SAME, still inherit the
        # conversation's last-known location/transport rather than losing
        # them; a genuine topic switch is handled separately above/below
        # and never reaches this branch.
        history_entities = accumulate_entities(history)
        for key in ("location", "transport", "topic"):
            if not merged_entities.get(key) and history_entities.get(key):
                merged_entities[key] = history_entities[key]
    else:
        try:
            from rag.query_understanding import classify_actionable_intent
            approx_intent = classify_actionable_intent(question, entities=merged_entities)["actionable_intent"]
        except Exception:
            approx_intent = "unknown"
        inferred = _TOPIC_BUCKET_BY_INTENT.get(approx_intent)
        topic = inferred or "unknown"
        transition = "new_topic" if topic != "unknown" else "unknown"

    # Topic switch discards stale entities from the PREVIOUS topic (Part
    # 6: "location should not affect retrieval") — resolve_conversation
    # already doesn't carry them forward in a genuine topic switch (its
    # own follow-up-marker detection won't match an unrelated new
    # question), this just makes the discard explicit for every
    # downstream consumer of this state, including one this function
    # doesn't control (Canonical Query Rewrite's entities argument).
    if transition == "switch_topic":
        for key in ("location", "transport", "topic", "attribute"):
            if not current_only_entities.get(key):
                merged_entities[key] = None

    location = merged_entities.get("location")
    transport = merged_entities.get("transport")
    attribute = merged_entities.get("attribute")

    try:
        from rag.query_understanding import classify_actionable_intent
        intent_result = classify_actionable_intent(question, entities=merged_entities)
    except Exception:
        intent_result = {"actionable_intent": "unknown", "entities": {}}

    payment_method = intent_result.get("entities", {}).get("payment_method")
    if not payment_method and topic == "payment" and re.search(r"บัตร", question or ""):
        payment_method = "credit_card"

    subtopic = subtopic_for(intent_result["actionable_intent"], attribute)

    topic_confidence = 1.0 if current_bucket else (0.7 if prev_bucket else (0.5 if topic != "unknown" else 0.0))
    entity_confidence = conversation.get("confidence", 0.0) or 0.0
    transition_confidence = 1.0 if (current_bucket or conversation.get("followup_type")) else 0.5
    overall = round((topic_confidence + entity_confidence + transition_confidence) / 3, 2)

    return {
        "topic": topic,
        "subtopic": subtopic,
        "intent": intent_result["actionable_intent"],
        "location": location,
        "warehouse": location if topic == "warehouse" else None,
        "transport": transport,
        "payment": payment_method,
        "order_type": None,
        "tracking": None,
        "product": None,
        "resolved_question": conversation["resolved_question"],
        "state_changes": conversation.get("replaced_entities", {}),
        "excluded_entities": conversation.get("excluded_entities", {}),
        "transition": transition,
        "conversation_confidence": {
            "topic_confidence": round(topic_confidence, 2),
            "entity_confidence": round(entity_confidence, 2),
            "transition_confidence": round(transition_confidence, 2),
            "overall": overall,
        },
    }
