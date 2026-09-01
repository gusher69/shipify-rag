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
    """True when the wording names 2+ transport modes (e.g. "ทางรถกับ
    ทางเรือระยะเวลากี่วัน") — the answer must cover them all. Reads the
    shared transport-facet source of truth
    (rag/query_resolution.requested_transport_modes) instead of an
    ad-hoc substring check, so this layer can never disagree with query
    resolution / canonical query about how many modes were asked for."""
    from rag.query_resolution import requested_transport_modes
    return len(requested_transport_modes(question)) >= 2


# ── P2 — Contextual follow-up ("correct answer + ONE useful next question")
# Deterministic, no LLM. OFF by default; ON only for a small set of
# semantic/intent + RequestSpec triggers. ERP / clarification / slot-
# filling / no-information / conflict turns never reach these (the caller
# gates), and each trigger is additionally repetition-guarded against the
# last 1-2 assistant turns. See docs/customer-service/02_CS_RESPONSE_POLICY.md
# (NEXT_STEP state).
_IMPORT_INTEREST_RE = re.compile(
    r"อยาก.{0,6}(นำเข้า|สั่งของ|สั่งซื้อ|ชิป|ขนส่ง|ใช้บริการ)"
    r"|สนใจ.{0,6}(นำเข้า|บริการ|สั่งของ)"
    r"|ต้องการ.{0,8}นำเข้า"
    r"|อยากนำเข้า|สนใจนำเข้า|ขอใช้บริการนำเข้า|อยากใช้บริการ")
_AIR_QUERY_RE = re.compile(r"เครื่องบิน|ทางอากาศ|air\s*freight|แอร์คาร์โก", re.IGNORECASE)
# Residual after stripping intent / origin / mode words is one of these
# (or empty) -> the customer has NOT named a concrete product yet.
_GENERIC_GOODS_RE = re.compile(
    r"^(สินค้า|ของ|สิ่งของ|พัสดุ|อะไร|อะไรบ้าง|บางอย่าง|หลายอย่าง|ทั่วไป|จากจีน)?$")
# A common product category named in the message -> product IS known.
# Deliberately no bare 2-char terms (e.g. "ยา" is a substring of "อยาก") —
# same Thai-no-word-boundary caution as rag/query_resolution.py.
_NAMED_PRODUCT_HINT_RE = re.compile(
    r"เสื้อผ้า|เครื่องสำอาง|น้ำหอม|อาหาร|ขนม|แบตเตอรี่|อะไหล่|เฟอร์นิเจอร์|มือถือ|โทรศัพท์|"
    r"ของเล่น|เครื่องมือ|เวชภัณฑ์|อุปกรณ์|รองเท้า|กระเป๋า|เครื่องใช้ไฟฟ้า|เครื่องประดับ|นาฬิกา|"
    r"เครื่องหนัง|สินค้าแบรนด์|อาหารเสริม")

_FOLLOWUP_QUESTION_TH = {
    "elicit_product_type": "คุณลูกค้าต้องการนำเข้าสินค้าประเภทไหนคะ",
    "offer_alternative_product": "มีสินค้าอย่างอื่นที่ต้องการให้ช่วยเช็กไหมคะ",
    # P5 — WARM decision-support: one missing decision-relevant fact.
    "elicit_transport_mode": "สนใจส่งทางรถหรือทางเรือคะ",
}
_FOLLOWUP_GOAL = {
    "elicit_product_type": ("ask, in ONE short natural question, what type / kind of product "
                             "the customer wants to import or ship"),
    "offer_alternative_product": ("offer, in ONE short natural question, to check whether "
                                   "another product can be imported — never ask for size, "
                                   "quantity or details of the item already ruled out"),
    "elicit_transport_mode": ("ask, in ONE short natural question, whether the customer wants "
                               "road or sea freight — never ask for quantity, weight or size"),
}
_FOLLOWUP_PURPOSE_MARKERS = {
    "elicit_product_type": re.compile(
        r"สินค้าประเภท|ประเภทไหน|ประเภทอะไร|สินค้าอะไร|นำเข้าสินค้าอะไร|ขนส่งสินค้าประเภท|"
        r"สินค้าชนิดไหน|ของประเภทไหน"),
    "offer_alternative_product": re.compile(
        r"สินค้า.{0,6}อื่น|ตัวอื่น|รายการอื่น|อย่างอื่น.{0,14}(เช็ก|เช็ค|ตรวจสอบ|ดู|นำเข้า)|"
        r"มี.{0,12}อื่น.{0,10}(นำเข้า|เช็ก|เช็ค|ตรวจ)|นำเข้าเพิ่มไหม|ตรวจสอบการนำเข้าไหม"),
    "elicit_transport_mode": re.compile(
        r"ทางรถหรือทางเรือ|รถหรือเรือ|เรือหรือรถ|ส่งทางไหน|ขนส่งแบบไหน|เลือกขนส่ง"),
}

# P5 stage-aware follow-up depth: which purposes each lead stage may use.
# COLD/WARM keep the existing P2 exploratory purposes; HOT gets none of
# them (its next step is operational, decided by the ERP workflow at a
# higher priority, never an exploratory sales question here).
_STAGE_ALLOWED_PURPOSES = {
    "COLD": {"elicit_product_type"},
    "WARM": {"elicit_product_type", "offer_alternative_product", "elicit_transport_mode"},
    "HOT": set(),
}
_RATE_DURATION_INTENTS = ("shipping_rate", "shipping_calculation", "shipping_duration")


_FOLLOWUP_LEADING_VERB_RE = re.compile(
    r"^(นำเข้า|สั่งซื้อ|สั่งของ|สั่ง|ส่งของ|ส่ง|ชิป|ขนส่ง|ฝากสั่ง|ฝากนำเข้า|ใช้บริการ|บริการ|"
    r"อยาก|ต้องการ|สนใจ|จะ)\s*")


def _followup_residual_product(text: str) -> str:
    core = re.sub(r"(อยาก|ต้องการ|สนใจ|จะ|ขอ|ช่วย|หน่อย|ครับ|ค่ะ|คะ|นะ|น่ะ|ด้วย)\s*", "", text or "")
    core = re.sub(r"(จากจีน|จากประเทศจีน|ประเทศจีน|จีน|มาไทย|เข้าไทย|มาที่ไทย)\s*", "", core).strip()
    for _ in range(4):  # peel stacked verbs: "ใช้บริการนำเข้า" -> "นำเข้า" -> ""
        stripped = _FOLLOWUP_LEADING_VERB_RE.sub("", core).strip()
        if stripped == core:
            break
        core = stripped
    return core.strip()


def _import_interest_without_product(text: str) -> bool:
    if not _IMPORT_INTEREST_RE.search(text or ""):
        return False
    if _NAMED_PRODUCT_HINT_RE.search(text or ""):
        return False
    residual = _followup_residual_product(text)
    return not residual or bool(_GENERIC_GOODS_RE.match(residual))


def _purpose_recently_served(purpose: str, history: Optional[List[Dict]]) -> bool:
    marker = _FOLLOWUP_PURPOSE_MARKERS.get(purpose)
    if not marker or not history:
        return False
    recent_assistant = [t.get("content") or "" for t in history if t.get("role") == "assistant"][-2:]
    return any(marker.search(t) for t in recent_assistant)


def _product_known(request_spec, entities: Optional[Dict], raw_question: str) -> bool:
    """The conversation already has a concrete product — from this turn's
    RequestSpec entities, a carried session topic, or a named-product hint
    in the wording. Used so P5 never re-asks a fact already known."""
    if list(getattr(request_spec, "entities", []) or []):
        return True
    topic = (entities or {}).get("topic")
    if topic and topic not in ("บริษัท", "โกดัง", "Tracking", "ใบกำกับ"):
        return True
    return bool(_NAMED_PRODUCT_HINT_RE.search(raw_question or ""))


def _transport_known(request_spec, entities: Optional[Dict]) -> bool:
    return bool(list(getattr(request_spec, "transport_modes", []) or [])
               or (entities or {}).get("transport"))


def decide_followup(actionable_intent: str, request_spec, raw_question: Optional[str],
                     history: Optional[List[Dict]], answerability: Optional[str],
                     conflicting_components: Optional[List[str]],
                     clarification_required: bool = False,
                     lead_stage: Optional[str] = None,
                     sentiment_status: Optional[str] = None,
                     entities: Optional[Dict] = None) -> Dict:
    """{"needed": bool, "purpose": Optional[str], "question_goal": Optional[str]}.
    OFF unless one narrow trigger fires AND its purpose was not already
    served in the last 1-2 assistant turns.

    P5 stage-aware depth (deterministic, no LLM): `lead_stage`
    (COLD/WARM/HOT) narrows WHICH purpose is appropriate — HOT gets no
    exploratory question here (its next step is operational, owned by the
    ERP workflow); WARM may additionally ask ONE missing decision-relevant
    fact (transport mode). `sentiment_status == "NEGATIVE"` suppresses
    every optional stage-based follow-up — complaint / apology / handoff
    behavior is unchanged and owned elsewhere. Facts, verdicts and ERP
    behavior are never touched by any of this."""
    off = {"needed": False, "purpose": None, "question_goal": None}
    if clarification_required or conflicting_components or answerability == "no_information":
        return off
    # P5 — NEGATIVE override: never append a sales/exploratory question
    # while the customer is dissatisfied.
    if (sentiment_status or "").upper() == "NEGATIVE":
        return off
    q = raw_question or ""
    spec_entities = list(getattr(request_spec, "entities", []) or [])
    stage = (lead_stage or "").upper()

    purpose: Optional[str] = None
    if _import_interest_without_product(q):
        purpose = "elicit_product_type"                    # A — import/service interest, no product
    elif _AIR_QUERY_RE.search(q) and not spec_entities and not _NAMED_PRODUCT_HINT_RE.search(q):
        purpose = "elicit_product_type"                    # B — air unavailable, redirect, product unknown
    elif actionable_intent == "prohibited_goods":
        purpose = "offer_alternative_product"              # C — prohibited item, offer another
    elif (stage == "WARM" and actionable_intent in _RATE_DURATION_INTENTS
          and _product_known(request_spec, entities, q)
          and not _transport_known(request_spec, entities)):
        purpose = "elicit_transport_mode"                  # D — WARM decision support, ONE missing fact

    if not purpose:
        return off
    # P5 — stage gate: only keep a purpose the current lead stage permits.
    # No stage known (playground / not yet scored) -> keep existing P2
    # behavior unchanged (all P2 purposes allowed).
    if stage in _STAGE_ALLOWED_PURPOSES and purpose not in _STAGE_ALLOWED_PURPOSES[stage]:
        return off
    if _purpose_recently_served(purpose, history):
        return off
    return {"needed": True, "purpose": purpose, "question_goal": _FOLLOWUP_GOAL[purpose]}


def render_followup_question(purpose: Optional[str]) -> str:
    """Purpose -> approved Thai question. Used ONLY on the deterministic
    FAQ-direct path (which has no synthesis call to phrase it); normal
    synthesis turns get `question_goal` in the prompt instead."""
    return _FOLLOWUP_QUESTION_TH.get(purpose or "", "")


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


# Named Thai pickup points (same low-collision convention as the keyword
# lists elsewhere — extend the list if trusted RAG gains another named
# warehouse; the aggregation itself stays count-agnostic). Used ONLY to
# tell a GENERIC "ขอเบอร์โกดัง" apart from a SPECIFIC "ขอเบอร์โกดังอ่อนนุช".
_WAREHOUSE_SUBLOCATION_RE = re.compile(r"อ่อนนุช|นนทบุรี|บางใหญ่|บางม่วง|ลาดกระบัง")


def _distinct_warehouse_locations_in_evidence(chunks: List[Dict]) -> int:
    names = set()
    for c in chunks:
        for m in _WAREHOUSE_SUBLOCATION_RE.finditer(c.get("text") or ""):
            names.add(m.group(0))
    return len(names)


def _faq_exact_question(chunks: List[Dict]) -> str:
    """The 'Question:' line of a single confirmed FAQ-exact chunk (rag/
    searcher.py builds its text as 'Question: <q>\\nAnswer: <a>'). '' when
    the chunk list is not a lone FAQ-exact hit."""
    if not _is_faq_exact_match(chunks):
        return ""
    first = (chunks[0].get("text") or "").split("\n", 1)[0]
    if first.startswith("Question:"):
        return first[len("Question:"):].strip()
    return chunks[0].get("section_title") or ""


def _needs_warehouse_clarification(actionable_intent: str, entities: Dict, chunks: List[Dict],
                                   question: str = "") -> bool:
    if actionable_intent not in ("warehouse_location", "warehouse_map", "warehouse_contact"):
        return False
    # A named pickup point ("อ่อนนุช", "นนทบุรี") already resolves the
    # country ambiguity (every named point is a Thai one) — answer it
    # narrowly, never ask ไทย/จีน. (2026-09-01)
    if _WAREHOUSE_SUBLOCATION_RE.search(question or ""):
        return False
    # A confirmed exact/near-exact FAQ match whose OWN question already
    # stands alone — i.e. it is NOT a bare, country-less warehouse
    # question — is unambiguous and must never be overridden with a
    # ไทย/จีน clarification. Real regression (2026-09-01): "ขอเบอร์ติดต่อ"
    # sent right after a warehouse exchange inherits prev_topic="โกดัง",
    # gets reclassified warehouse_contact, and was asked
    # "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ" — even though it
    # exact-matched the generic company-contact FAQ row ("ขอเบอร์ติดต่อ",
    # no "โกดัง" in it). A genuine bare warehouse question ("ขอเบอร์โกดัง")
    # does NOT FAQ-exact match any row, so it still reaches the
    # clarification below.
    faq_q = _faq_exact_question(chunks)
    if faq_q and "โกดัง" not in faq_q:
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
    requested_components: Optional[List[str]] = None,
    comparison: Optional[str] = None,
    conflicting_components: Optional[List[str]] = None,
    history: Optional[List[Dict]] = None,
    request_spec: Optional[object] = None,
    answerability: Optional[str] = None,
    lead_stage: Optional[str] = None,
    sentiment_status: Optional[str] = None,
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
    requested_components = requested_components or []
    conflicting_components = conflicting_components or []
    _multi_component = len(requested_components) >= 2
    # P2 contextual follow-up — decided once here from data already
    # available this turn; OFF for every branch except the 3 narrow
    # triggers, and repetition-guarded. `raw_question` is the customer's
    # own wording (the trigger must be semantic, never phrase-specific).
    followup = decide_followup(
        actionable_intent, request_spec, raw_question or question, history,
        answerability, conflicting_components,
        lead_stage=lead_stage, sentiment_status=sentiment_status, entities=entities)

    if _needs_warehouse_clarification(actionable_intent, entities, chunks, raw_question or question):
        subject = _WAREHOUSE_CLARIFICATION_SUBJECT.get(actionable_intent, "ที่อยู่")
        return {
            "answer_goal": "Ask which warehouse location the customer means",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "response_shape": "clarification",
            "clarification_required": True,
            "clarification_question": f"ต้องการ{subject}โกดังไทยหรือโกดังจีนคะ",
            "followup": {"needed": False, "purpose": None, "question_goal": None},
        }
    if _needs_bill_clarification(actionable_intent, entities, chunks):
        return {
            "answer_goal": "Ask which bill the customer means",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "response_shape": "clarification",
            "clarification_required": True,
            "clarification_question": _CLARIFICATION_TEMPLATES["bill_ambiguous"],
            "followup": {"needed": False, "purpose": None, "question_goal": None},
        }

    # Named-sublocation narrowing (2026-09-01) — a request that names ONE
    # pickup point ("ขอแผนที่โกดังนนทบุรี") must be answered for THAT point
    # only. If the single evidence row happens to be a multi-location FAQ
    # (its verbatim text lists 2+ pickup points), do NOT return it
    # verbatim — fall through to synthesis with a goal scoped to the
    # named point so the other locations are left out.
    _named = _WAREHOUSE_SUBLOCATION_RE.search(raw_question or question or "")
    _named_sub = _named.group(0) if _named else None
    _multi_loc_evidence = _distinct_warehouse_locations_in_evidence(chunks) >= 2

    # P2A fidelity — a SINGLE-product eligibility question ("แชมพูนำเข้าได้ไหม")
    # can FAQ-exact match a DIFFERENT product's row via that row's curated
    # alt-questions (the shampoo alt on the ครีมอาบน้ำ / liquid row). The
    # row's Answer text then names ครีมอาบน้ำ, not the customer's แชมพู —
    # returning it verbatim presents another product as the customer's.
    # Route through synthesis so the answer names the actual item, using
    # the row + liquid policy as evidence. Only when the matched row's own
    # Question does not mention the customer's product.
    _single_elig_entity = (
        requested_components[0].split(" / ")[0].strip()
        if (len(requested_components) == 1 and requested_components[0].endswith("eligibility"))
        else None)
    _faq_names_other_product = bool(
        _single_elig_entity and _is_faq_exact_match(chunks)
        and _single_elig_entity not in _faq_exact_question(chunks))

    if (_is_faq_exact_match(chunks) and not (_named_sub and _multi_loc_evidence)
            and not _multi_component and not conflicting_components
            and not _faq_names_other_product):
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
            "followup": followup,
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

    # Multi-location warehouse aggregation (2026-09-01) — a GENERIC
    # collection-level warehouse request ("ขอเบอร์โกดัง" -> "ไทย":
    # warehouse intent, a country but NO specific pickup point named) must
    # return EVERY matching warehouse location the evidence carries, not
    # just the top chunk (the "telephone" / short_answer plan was
    # collapsing 2 Thai pickup points to 1 phone). Count-agnostic: the
    # goal says "every location in the evidence", so a future 3rd row is
    # covered automatically. A SPECIFIC request ("ขอเบอร์โกดังอ่อนนุช",
    # "โกดังนนทบุรีเปิดกี่โมง") names a pickup point and stays narrow.
    if (actionable_intent in ("warehouse_location", "warehouse_map", "warehouse_contact")
            and not _WAREHOUSE_SUBLOCATION_RE.search(question or "")
            and not _WAREHOUSE_SUBLOCATION_RE.search(raw_question or "")
            and _distinct_warehouse_locations_in_evidence(chunks) >= 2):
        _subj = {"warehouse_contact": "phone number",
                 "warehouse_map": "map and address",
                 "warehouse_location": "address"}[actionable_intent]
        goal = (f"List EVERY {loc}warehouse pickup location present in the evidence, each "
                f"with its {_subj} — name each location, do not stop at the first one")
        if "warehouse_name" not in required:
            required = ["warehouse_name"] + required
        if response_shape == "short_answer":
            response_shape = "answer_then_details"
    elif (actionable_intent in ("warehouse_location", "warehouse_map", "warehouse_contact")
          and _named_sub and _multi_loc_evidence):
        # Named ONE pickup point but the evidence lists several — scope the
        # answer to the named one only.
        _subj = {"warehouse_contact": "phone number",
                 "warehouse_map": "map and address",
                 "warehouse_location": "address"}[actionable_intent]
        goal = (f"Provide ONLY the โกดัง{_named_sub} location's {_subj} — the evidence also lists "
                f"other warehouse locations; ignore them, answer for โกดัง{_named_sub} only")
        if response_shape == "short_answer":
            response_shape = "answer_then_details"

    # Both-Transport-Modes fix (2026-08-31) — see _wants_both_transport_
    # modes' own docstring. Appends an explicit "cover both" instruction
    # instead of silently keeping whichever mode entities["transport"]
    # happened to match first. "short_answer" is widened to "answer_then_
    # details" — a genuine two-part answer is never a "short" one.
    #
    # Widened (2026-08-31, customer-acceptance pass): the customer's own
    # wording "ทางรถกับทางเรือระยะเวลากี่วัน" classifies as
    # actionable_intent="unknown" (rag/query_understanding.py does not tag
    # a bare road+sea duration question), so gating this on the two
    # shipping intents alone still dropped one mode for the exact case it
    # was built for. The trigger is now _wants_both_transport_modes alone
    # — already a narrow condition (BOTH "รถ" AND "เรือ" present in the raw
    # wording): a single-mode question ("ทางรถใช้เวลากี่วัน",
    # "ส่งทางรถได้ไหม") never contains "เรือ" and is untouched, and the
    # "เรือ" half also neutralises the "สามารถ"-contains-"รถ" substring
    # trap for this check.
    if _wants_both_transport_modes(raw_question or question):
        base_goal = (_GOAL_TEMPLATES[actionable_intent].format(loc=loc, transport="")
                     if actionable_intent in ("shipping_rate", "shipping_duration")
                     else "Provide the shipping duration/rate the customer asked about")
        goal = base_goal + " for BOTH road (รถ) and sea (เรือ) transport — the customer asked about both, cover both"
        if response_shape == "short_answer":
            response_shape = "answer_then_details"

    # Multi-component request (P1.2A) — generalises the road+sea "cover
    # both" instruction above to ANY set of requested components (several
    # products for an eligibility question, rate/duration/minimum facets,
    # two related sub-questions). Synthesis is told to answer EVERY
    # supported component and to mark ONLY unsupported ones as unconfirmed
    # — never a single blanket "no information" reply. Values still come
    # 100% from Retrieved Context (grounding rules unchanged).
    if _multi_component:
        goal = ("Answer EVERY requested component that trusted Retrieved Context supports. "
                "For any component with no support in the Context, name ONLY that specific "
                "component as unconfirmed (ยังไม่ยืนยัน) — never reply that there is no "
                "information for the whole question. Components: " + "; ".join(requested_components))
        if comparison:
            _dir = {"cheaper": "cheaper (ถูกกว่า)", "more_expensive": "more expensive (แพงกว่า)",
                    "faster": "faster (เร็วกว่า)", "slower": "slower (ช้ากว่า)"}.get(comparison, comparison)
            goal += (f". Then state which option is {_dir}, comparing ONLY the exact values present "
                     "in Retrieved Context — never a general/real-world assumption.")
            response_shape = "comparison"
        elif response_shape == "short_answer":
            response_shape = "answer_then_details"

    if conflicting_components:
        # P1.2B — trusted sources disagree on these; synthesis must not
        # pick a value. A conflict on one component never blocks the rest.
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
        "requested_components": list(requested_components),
        "conflicting_components": list(conflicting_components),
        "followup": followup,
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
