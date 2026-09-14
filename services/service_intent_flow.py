# -*- coding: utf-8 -*-
"""PHASE-6B — pre-RAG conversational / service-intent flow.

Customer acceptance source: "แก้ไขเคส Shipify Part 2.docx". The reported
root cause is that routing was effectively::

    User message -> search KB -> found ? answer : "ไม่มีข้อมูลในระบบ"

with no conversational / service-intent step in front of it, so a help
request, a service-discovery turn, an intent to send money to a China
shop, a request for a platform's website URL, a public contact request,
and the "my supplier will ship to your warehouse" journey all dead-ended
on KB_NOT_FOUND.

This module owns ONE capability — the deterministic reply for those
pre-RAG families — the same shape as
``services/shipping_estimate_flow.py`` / ``services/withdrawal_flow.py``:
intent RECOGNITION stays in ``services/conversation_semantics.py``
(``interpret`` -> ``intent_family`` / ``conversation_act``); this module
only turns a recognised family into the right reply and never calls an
LLM, never authorises private data, and never invents a business
commitment.

Acceptance Criteria (customer, verbatim): *"KB_NOT_FOUND ไม่ควรเท่ากับ
Conversation Cannot Continue"* and *"ถ้า User ปฏิเสธคำตอบ … ระบบต้องห้าม
ส่ง answer เดิมซ้ำโดยไม่ re-evaluate intent"*.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# families this module produces a pre-RAG reply for. IMPORT_INTEREST is
# NOT here — it keeps its existing FIX-2.3 RAG discovery path
# (``_product_answer_service_continuation``); only the genuinely new
# families are owned here.
SERVICE_INTENT_FAMILIES = frozenset({
    "HELP_INTENT", "SERVICE_DISCOVERY", "MONEY_TRANSFER_INTEREST",
    "WEBSITE_LINK_REQUEST", "CONTACT_INFO", "WAREHOUSE_INBOUND_JOURNEY",
    # PHASE 6 POST-DEPLOY (defect class B) — a cancellation POLICY
    # question ("ยกเลิกบิลสั่งซื้อได้ไหม") is a one-turn public policy
    # answer: it must never demand an identifier, and it must never be
    # answered with the money-withdrawal procedure. The OPERATION
    # sibling keeps its existing operational-collection path and is NOT
    # listed here.
    "CANCELLATION_POLICY",
})

# ── deterministic replies ────────────────────────────────────────────

# Page 5 of the acceptance doc gives this reply verbatim as the expected
# behaviour for "ต้องการความช่วยเหลือ" / "ช่วยหน่อย" / …
HELP_REPLY = (
    "ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า "
    "โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือต้องการสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ"
)

# T02 — describe the services AND ask a useful next step (the doc marks
# "describe only" as ⚠️ PARTIAL). Service list is Shipify's own trusted
# description; the closing line is the Next-Best-Action question.
SERVICE_DISCOVERY_REPLY = (
    "Shipify เป็นผู้ช่วยสั่งซื้อและนำเข้าสินค้าจากจีนแบบครบวงจรค่ะ "
    "มีบริการฝากสั่งซื้อสินค้าจากจีน ฝากนำเข้า ฝากโอนเงินให้ร้านค้าจีน "
    "ช่วยประสานงานกับร้าน/โรงงานจีน และจัดส่งถึงปลายทางในไทย\n\n"
    "สนใจบริการไหนเป็นพิเศษคะ หรือถ้ามีสินค้าที่อยากนำเข้าอยู่แล้ว "
    "แจ้งรายละเอียดสินค้าและปริมาณมาได้เลยค่ะ"
)

# T05 — "งั้นโอนเงินให้ร้านที่จีน": recognise the money-transfer service
# and move into its discovery, NOT a withdrawal how-to (wrong direction).
MONEY_TRANSFER_REPLY = (
    "Shipify มีบริการฝากโอนเงินให้ร้านค้าจีนค่ะ 😊 "
    "รบกวนแจ้งรายละเอียดที่ต้องการโอน เช่น ยอดเงิน (ระบุเป็นหยวนหรือบาท) "
    "และข้อมูลร้านค้า/ลิงก์สินค้าปลายทาง เพื่อให้เจ้าหน้าที่ดำเนินการต่อให้ค่ะ"
)

# Public homepage URLs of the platforms — publicly-known navigation URLs,
# not a Shipify business fact. Also offers the (separate) product-link
# conversion so the customer is not stuck if that is what they meant.
WEBSITE_LINK_REPLY = (
    "ลิงก์เว็บไซต์หลักของแต่ละแพลตฟอร์มค่ะ\n"
    "• Taobao: https://www.taobao.com\n"
    "• Tmall: https://www.tmall.com\n"
    "• 1688: https://www.1688.com\n\n"
    "ถ้าต้องการให้ช่วยแปลงลิงก์สินค้าเป็นหน้าภาษาไทยของ Shipify "
    "ส่งลิงก์สินค้ามาได้เลยค่ะ"
)

# honest fallback for the warehouse-inbound journey — KB has no chunk for
# any of these questions today (whose-parcel identification / pre-arrival
# notification requirement / arrival contact-back). Never a nearby FAQ,
# never a notification action.
WAREHOUSE_INBOUND_FALLBACK = (
    "เรื่องนี้ตอนนี้ยังไม่มีข้อมูลที่ยืนยันได้ค่ะ เดี๋ยวส่งต่อให้เจ้าหน้าที่ช่วยตรวจสอบ"
    "และติดต่อกลับนะคะ หรือทักมาที่ LINE: @Shipify / โทร 02-026-6426 ได้เลยค่ะ"
)

# last-resort contact reply — only used when the KB contact chunk cannot
# be fetched at all. The values come straight from that chunk
# ("ติดต่อ Shipify ช่องทางไหนได้บ้าง"), never independently invented.
CONTACT_FALLBACK = (
    "ติดต่อ Shipify ได้ทาง LINE: @Shipify, ฝ่ายบริการลูกค้า 02-026-6426, "
    "ฝ่ายพัฒนาธุรกิจ 080-289-3956, อีเมล info@shipify.co.th ค่ะ"
)

# a re-evaluation prompt used when the customer REJECTED the previous
# answer and the current message alone does not resolve to a concrete
# family — ask a genuine clarifying question, never resend the old reply.
CLARIFY_REPLY = (
    "ขอโทษด้วยค่ะ รบกวนช่วยอธิบายเพิ่มอีกนิดได้ไหมคะว่าต้องการให้ช่วยเรื่องไหน "
    "จะได้ตอบให้ตรงกับที่ต้องการค่ะ"
)


_KB_QA_PREFIX_RE = re.compile(r"^\s*(?:Question|คำถาม)\s*:.*?(?:Answer|คำตอบ)\s*:\s*", re.IGNORECASE | re.DOTALL)
_KB_ALT_SUFFIX_RE = re.compile(r"\s*(?:Alternative phrasings|Keywords?|Tags?)\s*:.*$", re.IGNORECASE | re.DOTALL)


def _clean_kb_answer(content: str) -> str:
    txt = _KB_QA_PREFIX_RE.sub("", content or "")
    txt = _KB_ALT_SUFFIX_RE.sub("", txt)
    return txt.strip()


def fetch_contact_kb_answer(sb) -> Optional[str]:
    """The active PUBLIC contact chunk, by a direct content lookup (never
    a vector-similarity search — the customer flagged that this exact
    phrasing scores borderline and dead-ends). Returns the cleaned Answer
    text or None."""
    if sb is None:
        return None
    for needle in ("ติดต่อได้ทาง LINE", "ติดต่อ Shipify ช่องทางไหน", "ฝ่ายบริการลูกค้า"):
        try:
            r = (sb.table("knowledge_chunks").select("content")
                 .ilike("content", f"%{needle}%").eq("is_active", True)
                 .limit(1).execute())
        except Exception:
            return None
        if r.data:
            ans = _clean_kb_answer(r.data[0].get("content") or "")
            if ans:
                return ans
    return None


def contact_info_reply(sb) -> str:
    return fetch_contact_kb_answer(sb) or CONTACT_FALLBACK


def warehouse_inbound_reply(_wh_kind: Optional[str] = None) -> str:
    # every sub-kind currently resolves to the same honest fallback — the
    # KB gap is identical for all three. Kept parameterised so a future
    # KB chunk can be wired per kind without another routing change.
    return WAREHOUSE_INBOUND_FALLBACK


def import_interest_reply(product: Optional[str], *, quantity: Optional[int] = None,
                          method: Optional[str] = None, unit: Optional[str] = None) -> str:
    """Reuse the frame acknowledgement so ``derive_active_frame`` can read
    the product back on the next turn (same 'reply wording IS the state'
    pattern the import frame already uses).

    PHASE-6-SLOT-CONSUMPTION: `quantity`/`method`, when the SAME opening
    turn already stated them, must be carried into the ack's Frame too —
    building a Frame from `product` alone silently discarded a
    confidently-known quantity/method, so the very next line asked for a
    slot the customer had just supplied ("20 คู่อยากสั่งของจากจีน" ->
    re-asks quantity). The caller is the single source of these values
    (services/conversation_semantics.py's own IMPORT_INTEREST entity
    extraction); this function never re-derives them itself."""
    from services.conversation_semantics import Frame, frame_ack_reply
    # PHASE-6-SLOT-CONSUMPTION — a quantity/method-only opener ("20 คู่
    # อยากสั่งของจากจีน", no specific product noun) still has REAL known
    # state; it must acknowledge that and ask for product, never fall
    # back to the fully-generic two-path reply below (which would
    # silently discard the quantity the customer already gave).
    if product or quantity or method:
        # PHASE 6 POST-DEPLOY (defect class C) — the unit the customer
        # actually supplied travels with the quantity into the ack, so
        # "20 คู่" is never echoed back as "20 ชิ้น".
        return frame_ack_reply(Frame(product=product, quantity=quantity,
                                     method=method, unit=unit), changed="none")
    # OWNER-REAL-LINE-FIX-01 — a broad "อยากสั่งของจากจีน" must NOT assume
    # the customer already has a product link. Offer BOTH paths in one
    # coherent reply: send a link if they have one, otherwise say what
    # they want and discovery continues. No auth, no Human CS claim.
    return (
        "ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
        "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ"
    )


def reply_for_family(family: str, *, sb=None, entities: Optional[Dict] = None,
                     product: Optional[str] = None) -> Optional[str]:
    """The pre-RAG reply for a recognised service-intent family, or None
    when this module does not own it."""
    ent = entities or {}
    if family == "HELP_INTENT":
        return HELP_REPLY
    if family == "SERVICE_DISCOVERY":
        return SERVICE_DISCOVERY_REPLY
    if family == "MONEY_TRANSFER_INTEREST":
        return MONEY_TRANSFER_REPLY
    if family == "WEBSITE_LINK_REQUEST":
        return WEBSITE_LINK_REPLY
    if family == "CONTACT_INFO":
        return contact_info_reply(sb)
    if family == "WAREHOUSE_INBOUND_JOURNEY":
        return warehouse_inbound_reply(ent.get("wh_kind"))
    if family == "CANCELLATION_POLICY":
        # rendered from the SAME approved statement the operational
        # cancellation ack uses, so policy and operation can never state
        # different business truth (services/operational_change_flow.py).
        from services.operational_change_flow import CANCELLATION_POLICY_ANSWER
        return CANCELLATION_POLICY_ANSWER
    if family == "IMPORT_INTEREST":
        return import_interest_reply(product or ent.get("product"),
                                      quantity=ent.get("quantity"), method=ent.get("method"),
                                      unit=ent.get("quantity_unit"))
    return None


# ── repeated-answer / loop guard ────────────────────────────────────
_NORM_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _NORM_RE.sub(" ", (s or "").strip()).rstrip("ค่ะครับคะนะ ").strip()


def is_repeat_of_last_assistant(history: Optional[List[Dict]], candidate_text: str) -> bool:
    """True when ``candidate_text`` is materially the same as the most
    recent assistant turn — so the Decision Engine can refuse to resend
    it after the customer pushed back."""
    cand = _norm(candidate_text)
    if not cand:
        return False
    for turn in reversed(list(history or [])):
        if turn.get("role") != "assistant":
            continue
        prev = _norm(turn.get("content") or "")
        if not prev:
            return False
        if prev == cand:
            return True
        shorter, longer = sorted((prev, cand), key=len)
        return bool(shorter) and shorter in longer and len(shorter) >= 0.8 * len(longer)
    return False


_ASSISTANT_LINK_PROMPT_RE = re.compile(
    r"ส่งลิงก์สินค้า|ลิงก์ที่ต้องการแปลง|แปลงลิงก์.*ส่งลิงก์|ส่งลิงก์.*มาได้เลย",
    re.IGNORECASE)


def last_assistant_was_link_conversion_prompt(history: Optional[List[Dict]]) -> bool:
    for turn in reversed(list(history or [])):
        if turn.get("role") == "assistant":
            return bool(_ASSISTANT_LINK_PROMPT_RE.search(turn.get("content") or ""))
        if turn.get("role") == "user":
            continue
    return False


# T01 — a bare affirmation ("ใช่ครับ", "ใช่ค่ะ", "อือ", "ครับ") right
# after the assistant OFFERED help ("มีอะไรให้ช่วยไหมคะ", "ต้องการให้ช่วย
# เรื่องไหนคะ") is the customer accepting the offer — HELP_INTENT, never a
# KB lookup.
_BARE_AFFIRM_RE = re.compile(
    r"^\s*(?:ใช่|ครับ|ค่ะ|คับ|จ้า|จ้ะ|อือ|เออ|โอเค|ok|yes|ใช่เลย|ถูกต้อง|ใช่ครับ|ใช่ค่ะ)"
    r"[\s\.!~ครับคับค่ะคะจ้าจ้ะเลยนะ]*$", re.IGNORECASE)
_ASSISTANT_HELP_OFFER_RE = re.compile(
    r"มีอะไรให้(?:ช่วย|ผมช่วย|เราช่วย)|ต้องการให้(?:ช่วย|เรา|ผม)\S{0,10}(?:เรื่อง)?ไหน"
    r"|ให้ช่วยเรื่องไหน|มีอะไรให้ช่วยไหม|ต้องการความช่วยเหลือ\S{0,6}(?:เรื่อง)?ไหน"
    r"|สอบถามได้เลย|แจ้งได้เลย|มีเรื่องไหนให้ช่วย",
    re.IGNORECASE)


def is_help_affirmation(message: str, history: Optional[List[Dict]]) -> bool:
    if not _BARE_AFFIRM_RE.match((message or "").strip()):
        return False
    for turn in reversed(list(history or [])):
        if turn.get("role") == "assistant":
            return bool(_ASSISTANT_HELP_OFFER_RE.search(turn.get("content") or ""))
        if turn.get("role") == "user":
            continue
    return False
