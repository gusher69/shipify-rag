"""Answer Planner — selects and organizes which FACT LABELS (never fact
VALUES) the LLM should focus the answer on, given a retrieved evidence
set that may contain more attributes than the customer actually asked
about (e.g. a warehouse FAQ row also has business hours and pickup
instructions when the customer only asked for the phone number).

This module NEVER generates or invents a fact — it only picks from a
small, fixed vocabulary of fact LABELS (see rag/query_understanding.py's
REQUESTED_ATTRIBUTES_BY_INTENT) and instructs the prompt which of those
labels are required/optional/excluded. The actual VALUES still come
100% from Retrieved Context, exactly as the existing STRICT_GROUNDING_RULES
already require — this module changes what the LLM is told to focus on,
never what it's allowed to claim as fact.

Deterministic, pure Python, no LLM call.
"""
import re
from typing import Dict, List, Optional

_ORDER_BILL_MARKER_RE = re.compile(r"บิลสั่งซื้อ|ค่าสินค้า")
_SHIPPING_BILL_MARKER_RE = re.compile(r"บิลค่าขนส่ง|ค่าขนส่ง")

# Company-overview/company-summary fix (P0, 2026-07-21): a question
# explicitly asking for MORE detail overrides the summary's default,
# concise exclusion list (Part 3 of the task spec) — never for
# company_overview, which always stays concise regardless of wording.
_DETAILED_SUMMARY_RE = re.compile(r"แบบละเอียด|อย่างละเอียด|รายละเอียด|detailed", re.IGNORECASE)


def _wants_detailed_summary(question: str) -> bool:
    return bool(_DETAILED_SUMMARY_RE.search(question or ""))


def _wants_both_transport_modes(question: str) -> bool:
    """A question naming BOTH land and sea transport (e.g. "ทางรถกับ
    ทางเรือระยะเวลากี่วัน") is asking to cover BOTH, not just whichever
    rag/query_resolution.py::_extract_transport() happened to match first
    (_TRANSPORT_RE.search() only ever returns the FIRST occurrence in the
    text, so entities["transport"] is a single scalar value, never a
    list). Confirmed live: this exact question was answered with land-
    only duration despite the retrieved chunk having both durations,
    because entities["transport"] came back as just "รถ" and the goal/
    response_shape below narrowed the answer to that one mode alone."""
    q = question or ""
    return "รถ" in q and "เรือ" in q

_CLARIFICATION_TEMPLATES = {
    "warehouse_ambiguous": "ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ",
    "bill_ambiguous": "ต้องการชำระบิลสั่งซื้อหรือบิลค่าขนส่งคะ",
}

# Intent-specific phrasing for the warehouse-country clarification — the
# generic "warehouse_ambiguous" template above always said "ที่อยู่"
# (address) even when the actual question was about the phone number or
# the map, which then made the clarification ITSELF misleading about
# what's actually being asked. Keyed by actionable_intent so the
# question the customer sees always matches what they asked for.
_WAREHOUSE_CLARIFICATION_SUBJECT = {
    "warehouse_contact": "เบอร์ติดต่อ",
    "warehouse_map": "แผนที่",
    "warehouse_location": "ที่อยู่",
}

# actionable_intent -> (required_facts, optional_facts, response_shape).
# Facts are LABELS, matching rag/query_understanding.py's
# REQUESTED_ATTRIBUTES_BY_INTENT vocabulary (kept in sync, not
# duplicated meaning — this table adds optional/excluded tiers and a
# response shape on top of that same label set).
_PLAN_TEMPLATES: Dict[str, Dict] = {
    "warehouse_map": {"required": ["warehouse_name", "address", "map_url"], "optional": ["telephone"],
                       "shape": "answer_then_links"},
    "warehouse_location": {"required": ["address"], "optional": ["map_url", "telephone"],
                            "shape": "answer_then_links"},
    "warehouse_contact": {"required": ["telephone"], "optional": [], "shape": "short_answer"},
    "shipping_rate": {"required": ["rate_per_kg", "rate_per_cbm"], "optional": ["transit_time"],
                       "shape": "answer_then_details"},
    "shipping_duration": {"required": ["duration_days"], "optional": [], "shape": "short_answer"},
    "shipping_calculation": {"required": ["calculated_rate"], "optional": [], "shape": "short_answer"},
    "payment_instruction": {"required": ["payment_steps"], "optional": ["attachment"], "shape": "steps"},
    "payment_policy": {"required": ["minimum_amount", "fee_percent", "invoice_restriction"], "optional": [],
                        "shape": "answer_then_details"},
    "coupon_policy": {"required": ["coupon_steps"], "optional": [], "shape": "steps"},
    "invoice_policy": {"required": ["invoice_conditions"], "optional": [], "shape": "answer_then_details"},
    "prohibited_goods": {"required": ["prohibited_list"], "optional": [], "shape": "answer_then_details"},
    "tracking_status": {"required": ["tracking_info"], "optional": [], "shape": "short_answer"},
    "attachment_request": {"required": [], "optional": ["attachment"], "shape": "attachment_only"},
    "human_agent_request": {"required": [], "optional": [], "shape": "short_answer"},
    "summary": {"required": ["summary"], "optional": [], "shape": "answer_then_details"},
    "service_information": {"required": ["general_info"], "optional": [], "shape": "answer_then_details"},
    "company_overview": {
        "required": ["core_business", "china_import_context", "core_services"],
        "optional": ["shipping_modes", "warehouse", "contact"],
        "shape": "company_overview_structured",
    },
    "company_summary": {
        "required": ["company_overview", "core_services"],
        "optional": ["shipping", "warehouse", "contact"],
        "shape": "company_summary_structured",
    },
    "unknown": {"required": [], "optional": [], "shape": "answer_then_details"},
}

_GOAL_TEMPLATES = {
    "warehouse_map": "Provide the {loc}warehouse map and address",
    "warehouse_location": "Provide the {loc}warehouse address",
    "warehouse_contact": "Provide the {loc}warehouse phone number",
    "shipping_rate": "Provide the {transport}shipping rate",
    "shipping_duration": "Provide the {transport}shipping duration",
    "shipping_calculation": "Provide the calculated shipping cost",
    "payment_instruction": "Explain how to complete payment",
    "payment_policy": "Explain the payment method's conditions",
    "coupon_policy": "Explain how to use a coupon",
    "invoice_policy": "Explain tax invoice conditions",
    "prohibited_goods": "List prohibited goods",
    "tracking_status": "Provide tracking status",
    "attachment_request": "Provide the requested image/file",
    "human_agent_request": "Acknowledge the request for a human agent",
    "summary": "Summarize the conversation/topic",
    "service_information": "Provide general service information",
    "company_overview": "Explain what the company primarily does",
    "company_summary": "Provide a readable, high-level company summary",
    "unknown": "Answer the question from the retrieved context",
}


# Direct FAQ Fidelity Mode (P0, 2026-07-21) — an approved FAQ answer's
# TYPE determines what facts it can legitimately be expected to provide.
# A navigation-instruction answer ("go to the website, menu X") never
# claims to contain a raw address/map URL itself — forcing it through a
# fixed required_facts=[address, map_url] schema is what previously made
# a perfectly correct approved answer read as "no information found."
# Generic, keyword-driven, never entity/company-specific.
ANSWER_TYPES = (
    "direct_fact", "instruction", "navigation_instruction", "procedure", "policy", "list",
    "calculation", "escalation_instruction", "attachment_instruction", "contact_information",
    "conditional_answer",
)

_NAVIGATION_RE = re.compile(r"เมนู|หน้าเว็บ|เข้าไปที่|เข้าเว็บ|เข้าแอป|เข้าระบบ")
# "กด" (press/tap a button) is a stronger, more specific signal of an
# in-app interactive PROCEDURE than a bare "เมนู"/"หน้าเว็บ" mention alone
# — checked before the navigation check so "เข้าเมนู X แล้วกด Y" (Part 9's
# own coupon example) classifies as procedure, while "ไปที่หน้าเว็บ...เมนู
# ...จากนั้นคัดลอก" (no button-press action, just "go look here") still
# classifies as navigation_instruction (Part 1's own warehouse example).
_BUTTON_ACTION_RE = re.compile(r"กดปุ่ม|กดเลือก|กดใช้|กดยืนยัน|กดที่|กดสั่ง|กดชำระ")
_PROCEDURE_RE = re.compile(r"ขั้นตอน|ตามรูป|กดปุ่ม|เลือก.*แล้ว|จากนั้น")
_CONTACT_RE = re.compile(r"0\d{1,2}[-\s]?\d{3}[-\s]?\d{3,4}|เบอร์")
_ESCALATION_RE = re.compile(r"ติดต่อเจ้าหน้าที่|แอดมิน|ทีมงานจะติดต่อ")
_ATTACHMENT_RE = re.compile(r"ตามรูป|ดูรูป|ภาพประกอบ|ไฟล์แนบ")
_POLICY_RE = re.compile(r"ไม่มีขั้นต่ำ|ไม่รับ|ไม่คุ้มครอง|เงื่อนไข|นโยบาย")
_CONDITIONAL_RE = re.compile(r"ถ้า|หาก|กรณี")
_LIST_MARKER_RE = re.compile(r"(^|\n)\s*([-•*]|\d+[.\)])\s+", re.MULTILINE)


def detect_answer_type(answer_text: str) -> str:
    """Deterministic, keyword-driven classification of what KIND of
    answer an approved FAQ row's Answer text actually is — never entity/
    company-specific, works for any tenant's FAQ content. Checked in an
    order that prefers the most decisive, narrow signal first (contact/
    navigation/procedure) before broader ones (policy/conditional), so a
    navigation instruction that also happens to mention "หาก" isn't
    misclassified as merely conditional."""
    text = answer_text or ""
    if _CONTACT_RE.search(text):
        return "contact_information"
    if _BUTTON_ACTION_RE.search(text):
        return "procedure"
    if _NAVIGATION_RE.search(text):
        return "navigation_instruction"
    if _ESCALATION_RE.search(text):
        return "escalation_instruction"
    if _ATTACHMENT_RE.search(text):
        return "attachment_instruction"
    if _PROCEDURE_RE.search(text) or _LIST_MARKER_RE.search(text):
        return "procedure"
    if _POLICY_RE.search(text):
        return "policy"
    if _CONDITIONAL_RE.search(text):
        return "conditional_answer"
    return "direct_fact"


# answer_type -> (required, optional) FACT LABELS — deliberately narrow
# and type-appropriate (Part 2): a navigation instruction is never
# expected to name a raw address/map_url the way a direct_fact answer is.
_FACTS_BY_ANSWER_TYPE: Dict[str, Dict[str, List[str]]] = {
    "navigation_instruction": {"required": ["navigation_destination", "action_steps"],
                                "optional": ["attachment", "menu_name"]},
    "procedure": {"required": ["action_steps"], "optional": ["attachment"]},
    "contact_information": {"required": ["phone_or_contact"], "optional": []},
    "policy": {"required": ["policy_statement"], "optional": []},
    "conditional_answer": {"required": ["condition", "outcome"], "optional": []},
    "attachment_instruction": {"required": ["attachment"], "optional": ["action_steps"]},
    "escalation_instruction": {"required": ["escalation_message"], "optional": []},
    "list": {"required": ["list_items"], "optional": []},
    "calculation": {"required": ["calculated_value"], "optional": []},
    "direct_fact": {"required": [], "optional": []},  # falls back to requested_attributes
}


def _is_faq_exact_match(chunks: List[Dict]) -> bool:
    return len(chunks) == 1 and bool(chunks[0].get("is_faq_exact"))


def _chunk_text(chunks: List[Dict]) -> str:
    return " ".join(c.get("text") or "" for c in chunks)


def _needs_warehouse_clarification(actionable_intent: str, entities: Dict, chunks: List[Dict]) -> bool:
    if actionable_intent not in ("warehouse_location", "warehouse_map", "warehouse_contact"):
        return False
    # Deliberately does NOT look at what retrieval/FAQ matching happened
    # to guess — a near-exact FAQ match can silently pick ONE warehouse
    # row even when the customer's own wording never said which one (a
    # bare "ขอที่อยู่โกดัง" can score high enough against the Thai row's
    # Question alone to short-circuit retrieval without the customer
    # ever having specified a location). "Sufficient evidence" means an
    # EXPLICIT location entity — from this turn's own wording or a
    # conversation entity actually carried forward — never an
    # accidental retrieval match standing in for one.
    return not entities.get("location")


def _needs_bill_clarification(actionable_intent: str, entities: Dict, chunks: List[Dict]) -> bool:
    if actionable_intent != "payment_instruction":
        return False
    text = _chunk_text(chunks)
    return bool(_ORDER_BILL_MARKER_RE.search(text) and _SHIPPING_BILL_MARKER_RE.search(text))


def plan_answer(
    question: str,
    actionable_intent: str,
    requested_attributes: Optional[List[str]] = None,
    entities: Optional[Dict] = None,
    chunks: Optional[List[Dict]] = None,
    retrieval_confidence: Optional[float] = None,
    policy_set: Optional[Dict] = None,
    raw_question: Optional[str] = None,
) -> Dict:
    """Returns:
        {
          "answer_goal": str,
          "required_facts": [str, ...],
          "optional_facts": [str, ...],
          "excluded_facts": [str, ...],
          "response_shape": str,
          "clarification_required": bool,
          "clarification_question": Optional[str],
        }
    Never touches `chunks` — read-only, purely to decide WHICH facts to
    focus on and whether the question is too ambiguous to answer safely
    without asking. `chunks` should be the FINAL retrieved/context-built
    evidence set.

    `raw_question` (2026-08-31) — the customer's own, never-rewritten
    wording, used ONLY by _wants_both_transport_modes. `question` here is
    normally the CANONICAL rewrite (services/playground_orchestrator.py
    passes canonical_question), and rag/query_resolution.py's entity
    model carries only ONE transport value even when the customer named
    both (_extract_transport()'s .search() keeps the first match) — so a
    canonical rewrite for "ทางรถกับทางเรือระยะเวลากี่วัน" legitimately
    drops "เรือ" from the rewritten text entirely, and checking `question`
    alone would silently miss this case. Falls back to `question` when
    not given, so every existing caller/test is unaffected."""
    entities = entities or {}
    chunks = chunks or []
    requested_attributes = requested_attributes or []

    if _needs_warehouse_clarification(actionable_intent, entities, chunks):
        subject = _WAREHOUSE_CLARIFICATION_SUBJECT.get(actionable_intent, "ที่อยู่")
        return {
            "answer_goal": "Ask which warehouse location the customer means",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "response_shape": "clarification",
            "clarification_required": True,
            "clarification_question": f"ต้องการ{subject}โกดังไทยหรือโกดังจีนคะ",
        }
    if _needs_bill_clarification(actionable_intent, entities, chunks):
        return {
            "answer_goal": "Ask which bill the customer means",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "response_shape": "clarification",
            "clarification_required": True,
            "clarification_question": _CLARIFICATION_TEMPLATES["bill_ambiguous"],
        }

    if _is_faq_exact_match(chunks):
        # A confirmed exact/near-exact FAQ row IS the answer — trust it
        # fully rather than second-guessing with a narrower fact list.
        # Direct FAQ Fidelity Mode (P0, 2026-07-21): the row's own answer
        # TYPE (not the generic actionable_intent's usual fact schema)
        # decides what facts to require — a navigation-instruction answer
        # must never be forced through a direct_fact schema like
        # [warehouse_name, address, map_url], which is exactly what
        # turned a correct "go to the website menu" answer into a false
        # "no information" response.
        answer_type = detect_answer_type(chunks[0].get("text") or "")
        type_facts = _FACTS_BY_ANSWER_TYPE.get(answer_type, {"required": [], "optional": []})
        required = type_facts["required"] or list(requested_attributes) or ["faq_answer"]
        return {
            "answer_goal": _GOAL_TEMPLATES.get(actionable_intent, _GOAL_TEMPLATES["unknown"]).format(
                loc="", transport=""),
            "answer_type": answer_type,
            "required_facts": required,
            "optional_facts": type_facts["optional"], "excluded_facts": [],
            "response_shape": "faq_direct",
            "clarification_required": False, "clarification_question": None,
        }

    template = _PLAN_TEMPLATES.get(actionable_intent, _PLAN_TEMPLATES["unknown"])
    required = list(template["required"]) or list(requested_attributes)
    optional = list(template["optional"])

    excluded: List[str] = []
    if actionable_intent in ("warehouse_location", "warehouse_map"):
        excluded.append("business_hours")
        if entities.get("location") == "ไทย":
            excluded.append("china_warehouse_info")
        elif entities.get("location") == "จีน":
            excluded.append("thai_warehouse_info")
    if actionable_intent == "warehouse_contact":
        excluded.extend(["address", "map_url", "business_hours"])
    if actionable_intent == "company_overview":
        # Always excluded by default (Part 4) — a company-overview answer
        # is about WHAT the business does, never its prices/contact
        # details/procedures, regardless of wording.
        excluded.extend(["prices", "phone_numbers", "opening_hours", "restricted_goods",
                          "detailed_procedures", "secondary_services"])
    if actionable_intent == "company_summary":
        if _wants_detailed_summary(question):
            # Explicit "แบบละเอียด"/"detailed" request — widen instead of
            # excluding, per Part 3's "only include ... when the user
            # explicitly requests a detailed summary."
            optional.extend(["exact_prices", "prohibited_goods_list", "phone_numbers", "operating_hours"])
        else:
            excluded.extend(["exact_prices", "prohibited_goods_list", "phone_numbers",
                              "operating_hours", "unrelated_faq_details"])

    loc = f"{entities['location']} " if entities.get("location") else ""
    transport = f"{entities['transport']} " if entities.get("transport") else ""
    goal = _GOAL_TEMPLATES.get(actionable_intent, _GOAL_TEMPLATES["unknown"]).format(loc=loc, transport=transport)
    response_shape = template["shape"]

    # Both-Transport-Modes fix (2026-08-31) — see _wants_both_transport_
    # modes' own docstring. Scoped to exactly the two intents whose goal
    # template narrows to a single {transport} value; drops that narrowing
    # (transport="") and appends an explicit "cover both" instruction
    # instead of silently keeping whichever mode entities["transport"]
    # happened to match first. "short_answer" is widened to "answer_then_
    # details" — a genuine two-part answer is never a "short" one — every
    # other intent/shape combination is completely unaffected.
    if actionable_intent in ("shipping_rate", "shipping_duration") and _wants_both_transport_modes(raw_question or question):
        goal = (_GOAL_TEMPLATES[actionable_intent].format(loc=loc, transport="")
                + " for BOTH road (รถ) and sea (เรือ) transport — the customer asked about both, cover both")
        if response_shape == "short_answer":
            response_shape = "answer_then_details"

    plan = {
        "answer_goal": goal,
        "required_facts": required,
        "optional_facts": optional,
        "excluded_facts": excluded,
        "response_shape": response_shape,
        "clarification_required": False,
        "clarification_question": None,
    }
    # Answer Plan Validator (Part 9, P0 2026-07-21) — defense in depth:
    # `goal` above is built ONLY by interpolating `entities` (already the
    # validated set this function itself received) into a fixed template,
    # so it structurally can't invent a fact on its own — this call
    # exists so a FUTURE template/goal string can never silently regress
    # that guarantee without a test catching it (see
    # validate_answer_plan's own docstring).
    validation = validate_answer_plan(plan, entities)
    if not validation["valid"]:
        # Regenerate conservatively — strip the unsupported location word
        # rather than fail the turn; the required/optional/excluded fact
        # LABELS (never values) are untouched, only the free-text goal
        # sentence is re-templated without the location placeholder.
        goal = _GOAL_TEMPLATES.get(actionable_intent, _GOAL_TEMPLATES["unknown"]).format(loc="", transport=transport)
        plan["answer_goal"] = goal
    return plan


def validate_answer_plan(plan: Dict, validated_entities: Dict) -> Dict:
    """Answer Plan Validator (Part 9) — checks that `plan["answer_goal"]`
    (the only free-text field this module produces) never names a
    location value that isn't present in `validated_entities` — i.e. the
    original-query/carried-context entity set the Semantic Invariant
    Guard already trusts, never a corrupted correction/rewrite/expansion
    variant or the previous ASSISTANT answer. Returns
    {"valid": bool, "reason": str}."""
    goal = (plan or {}).get("answer_goal") or ""
    validated_location = (validated_entities or {}).get("location")
    for location_word in ("ไทย", "จีน", "thailand", "china"):
        if location_word.lower() in goal.lower() and location_word != validated_location:
            return {"valid": False,
                    "reason": f"answer_goal names location '{location_word}' not present in validated entities"}
    return {"valid": True, "reason": "answer_goal entities are a subset of validated entities"}
