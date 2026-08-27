"""Information Collection Engine (Slot Filling Engine) — a NEW workflow
layer that sits BETWEEN Unified Intent Classification (rag/
query_understanding.py, frozen, read-only here) and ERP execution
(services/erp_adapter.py, mock only for now). It never touches
Retrieval, Hybrid Search, Prompt Builder, Prompt Studio, AI Policies,
Grounding, Benchmark, or Production Validation — this module is purely
additive.

Deterministic, pure Python, no LLM call — same architectural posture as
every other conversation-intelligence module in this codebase (rag/
query_resolution.py, rag/conversation_state.py).

Responsibilities (per the task spec):
  - determine required parameters for an ERP-backed intent
  - detect missing parameters
  - generate a natural Thai follow-up question
  - remember collected values across turns (recomputed from `history`,
    the same "no separate persistence table" pattern rag/
    conversation_state.py already uses)
  - know when enough information exists
  - expose the current collection status for Developer Mode

This module does NOT classify intents using rag/query_understanding.py
(frozen) — the six ERP intents here (tracking/order/customer/warranty/
invoice/payment) are a distinct, narrower vocabulary this module owns
itself, via `detect_erp_intent()`. A caller (e.g. services/
playground_orchestrator.py) may additionally cross-check against the
existing `actionable_intent` for its own routing decisions — that is
read-only reuse, never a modification of the frozen classifier.
"""
import re
from typing import Callable, Dict, List, Optional

# ── ERP intent vocabulary — deliberately separate from rag/
# query_understanding.py's ACTIONABLE_INTENTS (frozen). Some names
# overlap in meaning (e.g. "tracking") but this module never imports or
# extends that frozen enum — it is its own, narrowly-scoped classifier
# for exactly the six ERP-backed intents in this task's spec.
ERP_INTENTS = ("tracking", "order", "customer", "warranty", "invoice", "payment")

_INTENT_KEYWORD_PATTERNS: Dict[str, re.Pattern] = {
    "tracking": re.compile(r"ของถึงไหน|เช็คสถานะ|ติดตามพัสดุ|พัสดุถึงไหน|สินค้าถึงไหน|tracking", re.IGNORECASE),
    # Task 03B (2026-08-26): broadened from "รับประกัน|เคลมสินค้า|
    # ประกันสินค้า|warranty" to bare "ประกัน" (a superset — anything
    # containing "รับประกัน"/"ประกันสินค้า" already contains "ประกัน" too,
    # so this never narrows what used to match) so genuine warranty
    # phrasings without the "รับ" prefix ("สินค้าอยู่ในประกันไหม", "ยังอยู่
    # ในประกันหรือเปล่า") are recognized at all. Safe to broaden ONLY
    # because detect_erp_intent() below now runs _is_genuine_product_
    # warranty() as a second gate — a bare "ประกัน"/"warranty" match alone
    # is never enough on its own (see that function's docstring).
    "warranty": re.compile(r"ประกัน|เคลมสินค้า|warranty", re.IGNORECASE),
    "invoice": re.compile(r"ใบกำกับภาษี|ใบเสร็จ|invoice", re.IGNORECASE),
    "payment": re.compile(r"ชำระเงิน|จ่ายเงิน|ค้างชำระ|ยอดค้าง|payment", re.IGNORECASE),
    "order": re.compile(r"สถานะออเดอร์|เช็คออเดอร์|ออเดอร์ถึงไหน|order status", re.IGNORECASE),
    "customer": re.compile(r"ข้อมูลลูกค้า|ประวัติการสั่งซื้อ|บัญชีลูกค้า", re.IGNORECASE),
}
# Checked in this order — "tracking" is the most common/urgent intent
# and its own vocabulary ("ของถึงไหน") could otherwise be shadowed by a
# broader "order" pattern.
_INTENT_ORDER = ["tracking", "warranty", "invoice", "payment", "order", "customer"]

# Warranty/Guarantee Intent Collision fix (Task 03B, 2026-08-26) —
# confirmed live: the bare "รับประกัน" alternative in the warranty
# pattern above matches ANY message containing that word, with no
# requirement that a PRODUCT is what's actually being guaranteed — "รับ
# ประกันไหมว่าจะถึงภายใน 7 วัน" (a delivery-time guarantee question) matched
# it identically to "เช็คประกันสินค้าให้หน่อย" (a genuine product-warranty
# lookup), triggering the Serial Number follow-up question for a customer
# who never asked about their product at all. "เคลมสินค้า"/"ประกันสินค้า"
# already name a product explicitly and are unaffected by this fix (both
# also match _WARRANTY_PRODUCT_CONTEXT_RE below via "สินค้า"); only the
# bare "รับประกัน"/"warranty" alternative needed disambiguating.
_WARRANTY_PRODUCT_CONTEXT_RE = re.compile(
    # \bSN — leading boundary only (never a trailing one): a real serial
    # number is almost always glued straight to "SN" with no space
    # ("SN123456"), so a trailing \b would never match at all (digits and
    # letters share no word-boundary between them).
    r"สินค้า|เครื่อง|อุปกรณ์|serial\s*number|\bserial\b|\bSN|S/N|ระยะเวลาประกัน|หมดประกัน|ยังอยู่ในประกัน",
    re.IGNORECASE)
_DELIVERY_GUARANTEE_CONTEXT_RE = re.compile(
    r"ถึงภายใน|ถึงตามกำหนด|ถึงก่อน|เวลาขนส่ง|เวลาจัดส่ง|ขนส่ง|จัดส่ง|ส่งถึง|delivery|transit|shipping|SLA|"
    r"กำหนดส่ง|ปลายทาง|เวลา|ทางรถ|ทางเรือ|ทางอากาศ|\d+\s*วัน",
    re.IGNORECASE)
# A short, targeted negation guard — mirrors rag/query_resolution.py::
# strip_negated_spans' "blank out marker + following window" technique,
# but with markers/window sized for THIS module's own confirmed case
# ("ไม่ได้ถามประกันสินค้า ผมถามว่ารับประกันเวลาขนส่งไหม" — a negated mention
# of "ประกันสินค้า" must never itself count as positive product-warranty
# evidence). Reusing query_resolution.py's own markers/window directly
# would not help here — that list ("ไม่ใช่"/"ไม่เอา"/"ไม่ต้องการ"/"ยกเว้น")
# was built for location/transport entity negation and doesn't cover
# "ไม่ได้ถาม", the actual phrasing that needs stripping here.
_WARRANTY_NEGATION_MARKERS = ["ไม่ได้ถาม", "ไม่ได้หมายถึง", "ไม่ใช่", "ไม่ต้อง", "ไม่เอา"]
_WARRANTY_NEGATION_WINDOW_CHARS = 20

# Company-Policy-Subject exclusion (P0 Final Fix follow-up, 2026-08-28) —
# confirmed live: "Shipify ช่วยเคลมสินค้าไหม" / "Shipify รับประกันคุณภาพ
# สินค้าไหม" (does Shipify offer this service AT ALL — a general policy/
# capability question naming the COMPANY as subject) matched
# _WARRANTY_PRODUCT_CONTEXT_RE via "สินค้า" identically to a genuine "check
# MY OWN item's warranty" lookup, triggering the Serial Number follow-up
# for a customer who never asked about their own product. Checked BEFORE
# the product-context check so it takes precedence only for THIS narrow
# shape; every existing product-warranty-lookup example (none of which
# name "Shipify"/"บริษัท" as the question's subject) is unaffected.
_COMPANY_POLICY_SUBJECT_RE = re.compile(r"shipify|บริษัท", re.IGNORECASE)


def _strip_warranty_negation(text: str) -> str:
    result = text
    for marker in _WARRANTY_NEGATION_MARKERS:
        idx = 0
        while True:
            pos = result.find(marker, idx)
            if pos == -1:
                break
            end = min(len(result), pos + len(marker) + _WARRANTY_NEGATION_WINDOW_CHARS)
            result = result[:pos] + (" " * (end - pos)) + result[end:]
            idx = end
    return result


def _is_genuine_product_warranty(text: str) -> bool:
    """Disambiguates a bare "รับประกัน"/"warranty" mention: genuine
    PRODUCT warranty requires evidence that a PRODUCT/ITEM is what's
    being guaranteed (สินค้า/เครื่อง/อุปกรณ์/serial number/...) — never the
    word "รับประกัน"/"ประกัน" alone. A message carrying DELIVERY/SHIPPING
    guarantee vocabulary instead (ถึงภายใน/ขนส่ง/จัดส่ง/delivery/SLA/...) is
    a shipping question, not a product lookup. A message with NEITHER
    signal (a genuinely context-free "มีรับประกันไหม") is ambiguous —
    returns False (never confidently assume Product Warranty without
    real evidence; the message falls through to the normal RAG/Decision
    Engine path, which has its own clarification/answerability
    mechanisms for exactly this case). Negation-aware: a negated mention
    of "ประกันสินค้า" never counts as positive product evidence."""
    stripped = _strip_warranty_negation(text)
    if _COMPANY_POLICY_SUBJECT_RE.search(stripped):
        return False
    if _WARRANTY_PRODUCT_CONTEXT_RE.search(stripped):
        return True
    if _DELIVERY_GUARANTEE_CONTEXT_RE.search(stripped):
        return False
    return False


# Invoice Lookup Collision fix (P0 Final Fix, 2026-08-28) — mirrors the
# warranty disambiguation above exactly: confirmed live, "ออกใบกำกับภาษี
# ได้ไหม" (a general POLICY/capability question — does Shipify offer this
# service at all) matched the bare "invoice" keyword identically to a
# genuine "please retrieve MY invoice" lookup, triggering the invoice/
# order-number follow-up question for a customer who never asked to look
# anything up. Genuine invoice LOOKUP requires evidence of an actual
# retrieval request: self-reference, a polite request marker, or a real
# invoice/order number already given — never the bare word "ใบกำกับภาษี"/
# "ใบเสร็จ" alone.
_INVOICE_LOOKUP_EVIDENCE_RE = re.compile(
    r"ของผม|ของฉัน|ของดิฉัน|(ขอ|ช่วย|รบกวน).{0,40}(หน่อย|ด้วย)"
)


def _is_genuine_invoice_lookup_request(text: str) -> bool:
    """Disambiguates a bare "ใบกำกับภาษี"/"ใบเสร็จ"/"invoice" mention —
    same spirit as _is_genuine_product_warranty above: a message with
    NEITHER a self-reference/request marker NOR a real invoice/order
    number is ambiguous and falls through to the normal RAG/Decision
    Engine path instead of confidently assuming a private lookup."""
    if _INVOICE_LOOKUP_EVIDENCE_RE.search(text):
        return True
    if _SLOT_PATTERNS["invoice_number"].search(text) or _SLOT_PATTERNS["order_number"].search(text):
        return True
    return False


def detect_erp_intent(text: str) -> Optional[str]:
    """First-matching ERP intent, or None if the text doesn't ask about
    any ERP-backed topic at all — None means "this workflow layer has
    nothing to do with this turn," never "unknown intent."""
    if not text:
        return None
    for intent in _INTENT_ORDER:
        if _INTENT_KEYWORD_PATTERNS[intent].search(text):
            if intent == "warranty" and not _is_genuine_product_warranty(text):
                continue
            if intent == "invoice" and not _is_genuine_invoice_lookup_request(text):
                continue
            return intent
    return None


# ── Slot value extractors — one regex per slot type. Deliberately
# conservative (specific formats) so a random number never gets
# mis-captured as the wrong slot.
#
# Bug fix (production): "tracking_number" previously REQUIRED a 2-3
# letter prefix (e.g. "TH123456789"), so a pure-numeric carrier tracking
# number ("1005505051005", "123456789" — both real, valid identifiers
# for some carriers) was never recognized at all, even when the active
# workflow had already asked for exactly this slot. The letter prefix is
# now OPTIONAL — this is not a new hardcoded format, it is the same
# extraction rule simply no longer over-constraining what counts as a
# "possible identifier" candidate; validation of whether the resulting
# value is acceptable for a given workflow still happens in
# build_collection_state(), never here.
_SLOT_PATTERNS: Dict[str, re.Pattern] = {
    "tracking_number": re.compile(r"\b(?:[A-Za-z]{2,3})?\d{6,15}\b"),
    "order_number": re.compile(r"\b(?:ORD|ord)[-_]?\d{4,12}\b|#\d{4,12}\b"),
    "customer_code": re.compile(r"\b(?:CUS|cus|CUST|cust)[-_]?\d{3,12}\b"),
    "phone_number": re.compile(r"\b0\d{8,9}\b"),
    "serial_number": re.compile(r"\b(?:SN|sn|S/N)[-_:]?\s?[A-Za-z0-9]{4,20}\b"),
    "invoice_number": re.compile(r"\b(?:INV|inv)[-_]?\d{4,12}\b"),
}


def extract_slots_from_text(text: str) -> Dict[str, str]:
    """Every recognizable slot VALUE found in `text` — a chunk of raw
    user input may legitimately contain more than one slot (e.g. both a
    tracking number and an order number in the same message).

    Kept UNCHANGED for backward compatibility — used to accumulate slots
    from PAST turns (_accumulate_slots) and as the fallback when there is
    no active `next_expected_slot` (see build_collection_state). The
    CURRENT turn's binding decision, when a workflow is actively waiting
    on a specific slot, instead goes through the separate Candidate
    Extraction -> Slot Validation -> Contextual Binding pipeline below —
    see extract_candidates()/bind_candidate_to_slot()."""
    if not text:
        return {}
    found: Dict[str, str] = {}
    for slot, pattern in _SLOT_PATTERNS.items():
        m = pattern.search(text)
        if m:
            found[slot] = m.group(0)
    return found


# ── Contextual Slot Binding (P1 behavior completion) ─────────────────
#
# Three separate responsibilities, per the task's own Core Principle —
# none of them may decide "business meaning" alone:
#
#   1. Candidate Extraction (extract_candidates) — neutral, source-
#      agnostic possible-identifier tokens. Never assigns a slot name.
#      Deliberately generic enough to later accept candidates from OCR/
#      Vision/file-parser/voice-transcription sources too (Image/OCR
#      Preparation) — it only needs a List[str] of raw values, it does
#      not care where they came from.
#   2. Slot Validation (_SLOT_VALIDATORS) — "is this candidate an
#      ACCEPTABLE value for slot X", never "which slot does this
#      candidate belong to". Most slots share the same broad identifier
#      shape (business meaning does NOT come from the regex); only
#      phone_number has a genuinely distinctive shape worth its own
#      stricter rule.
#   3. Contextual Binding (bind_candidate_to_slot) — decides business
#      meaning using ONLY the active workflow's next_expected_slot,
#      never by picking whichever slot's pattern happens to match first.
_CANDIDATE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,22}")


def extract_candidates(text: str) -> List[str]:
    """Neutral candidate extraction — every plausible identifier-looking
    token in `text`, in order of appearance, de-duplicated. A token must
    contain at least one digit (a bare word like "เลขนี้ค่ะ" — Thai script,
    not matched by this ASCII-oriented pattern anyway — or a stray
    English word never becomes a false candidate). This function NEVER
    assigns a slot name; see bind_candidate_to_slot() for that."""
    if not text:
        return []
    seen = set()
    candidates: List[str] = []
    for m in _CANDIDATE_TOKEN_RE.finditer(text):
        token = m.group(0)
        if not any(ch.isdigit() for ch in token):
            continue
        if token not in seen:
            seen.add(token)
            candidates.append(token)
    return candidates


def _validate_generic_identifier(candidate: str) -> bool:
    """The shape shared by tracking/order/invoice/customer/serial
    identifiers in this platform — alphanumeric (plus -_/), 4-20 chars,
    at least one digit. Deliberately IDENTICAL across these slot types:
    per the Core Principle, a tracking-specific (or any other slot-
    specific) regex must never be the thing deciding business meaning —
    only `next_expected_slot` (contextual binding) does that."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_/]{3,19}", candidate)) and any(ch.isdigit() for ch in candidate)


def _validate_phone_number(candidate: str) -> bool:
    """Phone numbers DO have a genuinely distinctive, narrower shape
    (all-digit, leading 0, 9-10 digits total) — this is a real business
    rule, not an arbitrary slot-specific pattern, so it stays stricter
    than the shared generic identifier validator above."""
    return bool(re.fullmatch(r"0\d{8,9}", candidate))


# One validator per configured slot type — this is the ONLY place that
# knows what makes a value "acceptable" for a given slot; adding a new
# slot type (policy_number, claim_number, booking_number, patient_id, ...)
# for a future customer/industry means adding one line here (and one
# entry in INTENT_SCHEMAS), never touching extraction or binding logic.
_SLOT_VALIDATORS: Dict[str, Callable[[str], bool]] = {
    "tracking_number": _validate_generic_identifier,
    "order_number": _validate_generic_identifier,
    "invoice_number": _validate_generic_identifier,
    "customer_code": _validate_generic_identifier,
    "serial_number": _validate_generic_identifier,
    "phone_number": _validate_phone_number,
}


def bind_candidate_to_slot(candidates: List[str], slot: Optional[str]) -> Dict:
    """Contextual Slot Binding — the ONLY function that turns a neutral
    candidate list into a business-meaningful slot value, and it does so
    using ONLY `slot` (the active workflow's next_expected_slot), never
    by testing every slot's pattern and picking whichever matches.

    Returns:
        {"status": "bound" | "ambiguous" | "none_valid",
         "value": Optional[str],           # set only when status == "bound"
         "valid_candidates": [str, ...]}   # every candidate that passed
                                            # this slot's validator

    - Exactly one valid candidate -> "bound".
    - Zero valid candidates -> "none_valid" (ask again, existing retry rules apply).
    - More than one valid candidate -> "ambiguous" (never auto-picks the
      first one — the caller must ask the user to clarify).

    `candidates` is a plain List[str] so this accepts candidates from ANY
    future source (OCR result, Vision extraction, file parser, voice
    transcription) exactly the same way it accepts text-extracted ones —
    no OCR/Vision engine is implemented here, this is only ensuring the
    binder itself is already source-agnostic.
    """
    validator = _SLOT_VALIDATORS.get(slot) if slot else None
    if not validator or not candidates:
        return {"status": "none_valid", "value": None, "valid_candidates": []}
    valid = [c for c in candidates if validator(c)]
    if len(valid) == 1:
        return {"status": "bound", "value": valid[0], "valid_candidates": valid}
    if len(valid) > 1:
        return {"status": "ambiguous", "value": None, "valid_candidates": valid}
    return {"status": "none_valid", "value": None, "valid_candidates": []}


# ── Intent schemas — Admin Configuration (Part: ADMIN CONFIGURATION).
# Kept as a plain Python config dict, the same pattern already used by
# services/answer_planner.py's _PLAN_TEMPLATES and rag/
# query_understanding.py's REQUESTED_ATTRIBUTES_BY_INTENT — editable in
# code today; a future admin UI can load/persist this same shape from a
# DB table without changing the engine's own logic (see Known
# Limitations in the task's final report).
#
# `required_groups`: a list of slot GROUPS, each group a list of
# alternative slot names — ANY ONE slot in a group being collected
# satisfies that group (e.g. customer_code OR phone_number).
INTENT_SCHEMAS: Dict[str, Dict] = {
    "tracking": {
        "required_groups": [["tracking_number"]],
        "optional_slots": ["order_number"],
        "follow_up_question": "รบกวนแจ้งเลขพัสดุ หรือเลขออเดอร์ เพื่อให้ตรวจสอบสถานะสินค้าได้ค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถขอเลขพัสดุได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยตรวจสอบให้นะคะ",
    },
    "order": {
        "required_groups": [["order_number"]],
        "optional_slots": [],
        "follow_up_question": "รบกวนแจ้งเลขออเดอร์ค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถขอเลขออเดอร์ได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยตรวจสอบให้นะคะ",
    },
    "customer": {
        "required_groups": [["customer_code", "phone_number"]],
        "optional_slots": [],
        "follow_up_question": "รบกวนแจ้งรหัสลูกค้า หรือเบอร์โทรศัพท์ที่ใช้สมัครสมาชิกค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถยืนยันตัวตนลูกค้าได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยเหลือนะคะ",
    },
    "warranty": {
        "required_groups": [["serial_number"]],
        "optional_slots": [],
        "follow_up_question": "รบกวนแจ้ง Serial Number ของสินค้าค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถขอ Serial Number ได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยเหลือเรื่องการรับประกันนะคะ",
    },
    "invoice": {
        "required_groups": [["invoice_number", "order_number"]],
        "optional_slots": [],
        "follow_up_question": "รบกวนแจ้งเลขใบกำกับภาษี หรือเลขใบสั่งซื้อค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถขอเลขใบกำกับภาษีได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยเหลือนะคะ",
    },
    "payment": {
        "required_groups": [["customer_code"]],
        "optional_slots": [],
        "follow_up_question": "รบกวนแจ้งรหัสลูกค้า หรือเลขออเดอร์ค่ะ",
        "max_retry": 2,
        "escalation_message": "ขออภัยค่ะ ทางเราไม่สามารถขอรหัสลูกค้าได้ เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยเหลือเรื่องการชำระเงินนะคะ",
    },
}

# Explicit human-request / refusal phrases — deliberately small,
# narrowly-scoped vocabulary owned by THIS module (never imports rag/
# query_understanding.py's own _HUMAN_AGENT_RE — that file is frozen
# and this workflow layer must not depend on its internals changing).
#
# Broadened 2026-08-13 (Human Handoff sprint) to cover more explicit
# phrasings a customer might use to ask for a person instead of the bot —
# not just the narrower "คุยกับ.../ขอ...เจ้าหน้าที่" phrasing this pattern
# originally covered. IGNORECASE only affects the Latin-script
# alternatives (Thai script has no case).
#
# Deliberately NOT bare "พนักงาน" or bare "CS"/"ติดต่อกลับ" — both are
# genuinely ambiguous words this codebase already uses for OTHER,
# unrelated, protected meanings: "พนักงาน" alone also means "employee" in
# an HR/employee-data lookup (see tests/test_decision_engine.py's
# "ขอข้อมูลพนักงานหน่อย" fixture), and a message that explicitly asks the
# AI to relay something TO CS ("ช่วยแจ้ง CS ให้หน่อยว่า...ติดต่อกลับ") is a
# DIFFERENT, already-built, already-tested mechanism — the SendLineNotiCS
# confirmation gate (services/decision_engine.py::_requires_confirmation,
# 2026-08-09/10 sprints) — not a request to escalate to a human right
# now. Scoping "พนักงาน" to when it's paired with "ช่วย" (staff HELP), and
# "CS" to when it's immediately followed by "ติดต่อกลับ" (CS calls BACK,
# the exact phrasing in the spec's own example), keeps both new triggers
# specific to "customer wants a person" without stealing traffic from
# either of those other, unrelated features.
_HUMAN_REQUEST_RE = re.compile(
    r"คุยกับเจ้าหน้าที่|ขอเจ้าหน้าที่|ติดต่อคน|ขอสายเจ้าหน้าที่|ไม่คุยกับบอท"
    r"|พนักงาน\s*ช่วย|ขอคุยกับคน"
    # "ติดต่อเจ้าหน้าที่" (contact STAFF) — distinct from the SendLineNotiCS
    # notify-action trigger's own "...ติดต่อกลับ" (contact the CUSTOMER
    # back), which never mentions "เจ้าหน้าที่" — so this stays specific to
    # "customer wants a person" (Playground Production Parity UAT,
    # 2026-08-15, scenario J: "ขอติดต่อเจ้าหน้าที่").
    r"|ติดต่อเจ้าหน้าที่"
    r"|CS\s{0,3}ติดต่อกลับ|\bhuman agent\b|\bcustomer service\b",
    re.IGNORECASE,
)
_REFUSAL_RE = re.compile(r"ไม่มี|ไม่สะดวกให้|ไม่บอก|ไม่อยากให้|หาไม่เจอ|ไม่รู้เลขที่")

# Active Handoff Follow-up (Golden Application Defect Fixes, 2026-08-16) --
# a message that refers to an ALREADY-outstanding human-contact request
# ("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", "เมื่อไหร่จะมีคนติดต่อ") rather than
# asking for one fresh (_HUMAN_REQUEST_RE, above). Deliberately a SEPARATE
# regex, never merged into _HUMAN_REQUEST_RE: the caller (decision_engine.
# py::decide) only treats a match here as handoff continuation when the
# conversation's OWN persisted handoff_status is currently PENDING/NOTIFIED
# (passed in via context["handoff_status"]) -- this pattern alone, on a
# conversation with no active handoff, must fall through to ordinary
# routing (e.g. RAG) same as before. Both "active state" AND "follow-up
# intent" are required; neither alone is sufficient (see decide()'s own
# comment at the call site for why).
_HANDOFF_FOLLOWUP_RE = re.compile(
    r"(ยังไม่มี[^.!?]{0,15}(เจ้าหน้าที่|คน|ใคร)[^.!?]{0,10}(ติดต่อ|โทร)"
    r"|(เจ้าหน้าที่|คน)[^.!?]{0,10}ยังไม่[^.!?]{0,10}(ติดต่อ|โทร)"
    r"|เมื่อไหร่[^.!?]{0,15}(เจ้าหน้าที่|คน|ใคร)[^.!?]{0,10}ติดต่อ"
    r"|ยังรอ[^.!?]{0,10}เจ้าหน้าที่"
    r"|รอ[^.!?]{0,10}เจ้าหน้าที่[^.!?]{0,10}อยู่)",
    re.IGNORECASE,
)


def resolve_active_erp_intent(history: Optional[List[Dict]], current_message: str) -> Optional[str]:
    """detect_erp_intent() alone only recognizes a message that itself
    contains an ERP keyword phrase ("ของถึงไหน") — a bare follow-up reply
    like "TH123456789" (just the tracking number, no keyword at all)
    would otherwise be missed entirely and fall through to the normal
    RAG/LLM path instead of completing the slot-filling flow already in
    progress. Current wording's OWN intent always wins (same priority
    convention as every other module in this pipeline); only when the
    current message names no ERP intent of its own do we check whether
    the immediately preceding assistant turn was THIS engine's own
    follow-up question, and if so, continue that same flow."""
    current = detect_erp_intent(current_message)
    if current:
        return current
    if not history:
        return None
    last_assistant = next((t for t in reversed(history) if t.get("role") == "assistant"), None)
    if not last_assistant:
        return None
    last_text = (last_assistant.get("content") or "").strip()
    for intent, schema in INTENT_SCHEMAS.items():
        if last_text == schema["follow_up_question"]:
            return intent
    return None


def _accumulate_slots(history: Optional[List[Dict]]) -> Dict[str, str]:
    """Walks every prior USER turn (oldest first), keeping the LAST
    value seen per slot — same "last value wins" convention rag/
    query_resolution.py::accumulate_entities() already uses, so a
    customer correcting themselves ("TH111... ไม่ใช่ TH222...") always
    has their most recent answer win."""
    slots: Dict[str, str] = {}
    if not history:
        return slots
    for turn in history:
        if turn.get("role") != "user":
            continue
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        slots.update(extract_slots_from_text(content))
    return slots


def _count_prior_follow_up_prompts(history: Optional[List[Dict]], follow_up_question: str) -> int:
    """How many times THIS engine already asked its own follow-up
    question in prior assistant turns — used as the retry counter
    (Part: ESCALATION POLICY's "repeated attempts fail"). Counts exact
    matches only, so an unrelated assistant reply never inflates it."""
    if not history:
        return 0
    return sum(1 for t in history if t.get("role") == "assistant" and (t.get("content") or "").strip() == follow_up_question)


def _missing_groups(required_groups: List[List[str]], collected: Dict[str, str]) -> List[List[str]]:
    return [group for group in required_groups if not any(slot in collected for slot in group)]


def build_collection_state(intent: str, history: Optional[List[Dict]], current_message: str) -> Dict:
    """The full Slot Filling state for this turn — everything Developer
    Mode needs (Part: DEBUG MODE):
        {
          "intent": str,
          "required_slots": [[...], ...],      # groups, alternatives within a group
          "collected_slots": {slot: value, ...},
          "missing_slots": [[...], ...],       # groups still unsatisfied
          "is_complete": bool,
          "next_expected_slot": Optional[str],  # first slot name of the next missing group
          "follow_up_question": Optional[str],
          "retry_count": int,
          "max_retry": int,
          "escalation_required": bool,
          "escalation_reason": Optional[str],
        }

    `intent` must be one of ERP_INTENTS (see detect_erp_intent()) — the
    caller is responsible for deciding THAT this turn needs ERP at all;
    this function only handles the "what information do we still need"
    question once that decision has already been made.
    """
    schema = INTENT_SCHEMAS.get(intent)
    if not schema:
        return {"intent": intent, "required_slots": [], "collected_slots": {}, "missing_slots": [],
                "is_complete": True, "next_expected_slot": None, "follow_up_question": None,
                "retry_count": 0, "max_retry": 0, "escalation_required": False, "escalation_reason": None,
                "ambiguous_candidates": []}

    collected = _accumulate_slots(history)  # unchanged — history is still read via extract_slots_from_text

    # Contextual Slot Binding: what does THIS workflow still need, based
    # purely on what history already collected (before this turn's own
    # message is applied)? Only when something is still missing do we
    # run Candidate Extraction + Slot Validation for the CURRENT message
    # against that specific expected slot — never against every slot's
    # pattern indiscriminately (Core Principle: business meaning comes
    # from conversation context, not from which regex happens to match).
    missing_before = _missing_groups(schema["required_groups"], collected)
    next_expected_before = missing_before[0][0] if missing_before else None

    ambiguous_candidates: List[str] = []
    if next_expected_before:
        # An active workflow is waiting on a SPECIFIC slot — bind ONLY to
        # that slot via Candidate Extraction -> Slot Validation ->
        # Contextual Binding. Deliberately does NOT also run the legacy
        # blind multi-slot extractor here: doing so would let an
        # unrelated slot's pattern (e.g. tracking_number's own broad
        # numeric shape) silently attach itself to `collected_slots` even
        # though the active workflow never asked for it — exactly the
        # "incorrectly bind it as tracking_number" failure mode this
        # feature exists to prevent.
        candidates = extract_candidates(current_message)
        binding = bind_candidate_to_slot(candidates, next_expected_before)
        if binding["status"] == "bound":
            collected[next_expected_before] = binding["value"]
        elif binding["status"] == "ambiguous":
            ambiguous_candidates = binding["valid_candidates"]
        # "none_valid" -> fall through with nothing bound; existing
        # retry/escalation rules below apply exactly as before.
    else:
        # No active missing slot for this workflow — retain the original,
        # unchanged blind-extraction behavior (e.g. a first turn that
        # names the intent and the value together already).
        collected.update(extract_slots_from_text(current_message))

    missing = _missing_groups(schema["required_groups"], collected)
    is_complete = not missing
    next_expected = missing[0][0] if missing else None

    # Bug fix (production): a successfully extracted slot must never be
    # read as "still retrying" — once collection is complete, the retry
    # counter resets to 0 rather than reporting how many follow-up
    # questions were asked before the user finally succeeded.
    retry_count = 0 if is_complete else _count_prior_follow_up_prompts(history, schema["follow_up_question"])

    clarification_question = None
    if ambiguous_candidates:
        clarification_question = f"พบข้อมูล {len(ambiguous_candidates)} รายการค่ะ รบกวนระบุว่าต้องการใช้หมายเลขใด"

    escalation_required = False
    escalation_reason = None
    if _HUMAN_REQUEST_RE.search(current_message or ""):
        escalation_required = True
        escalation_reason = "user_requested_human"
    elif not is_complete and not ambiguous_candidates and _REFUSAL_RE.search(current_message or ""):
        escalation_required = True
        escalation_reason = "user_refused_to_provide_information"
    elif not is_complete and not ambiguous_candidates and retry_count >= schema["max_retry"]:
        escalation_required = True
        escalation_reason = "max_retry_exceeded"

    if is_complete:
        follow_up_question = None
    elif clarification_question:
        follow_up_question = clarification_question
    else:
        follow_up_question = schema["follow_up_question"]

    return {
        "intent": intent,
        "required_slots": schema["required_groups"],
        "collected_slots": collected,
        "missing_slots": missing,
        "is_complete": is_complete,
        "next_expected_slot": next_expected,
        "follow_up_question": follow_up_question,
        "ambiguous_candidates": ambiguous_candidates,
        "retry_count": retry_count,
        "max_retry": schema["max_retry"],
        "escalation_required": escalation_required,
        "escalation_reason": escalation_reason,
        "escalation_message": schema["escalation_message"] if escalation_required else None,
    }
