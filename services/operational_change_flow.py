# -*- coding: utf-8 -*-
"""CUSTOMER-ACTION-1 — operational change / verify requests with NO
approved executable Business Action.

Cases S02 / S03 / S11 / S13 / S15 / G18 (from the customer UAT master).
Each is a request to CHANGE or VERIFY something on the customer's own
record for which there is NO ERP write action — the customer-approved
behaviour is uniform:

    understood intent
      -> acknowledge + ask for the ONE required identifier / input
      -> hand to Human CS (a staff member performs the change)

Never a "no information" dead-end, never a fake "ดำเนินการเรียบร้อยค่ะ",
never an unrelated ERP read. Deterministic + history-derived — the SAME
"assistant reply IS the state" pattern services/charter_truck_flow.py
uses. No LLM, no pending table. Semantic-First (services/
conversation_semantics.py) supplies the natural-language intent; this
module only decides the deterministic workflow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from services.slot_filling_engine import _validate_generic_identifier as _valid_id

# ── request-kind recognisers — a small COMPOSITIONAL verb + object set,
#    never a phrase dictionary. A kind fires when its verb marker AND its
#    object marker both appear in the message (order-independent — a bill
#    number may sit between them), except the two kinds (duplicate_bill /
#    topup_not_credited) that are self-describing on their own. Each kind
#    carries its own ack (part-1 of the CS-approved answer) + input label.
_CHANGE_VERB_RE = re.compile(r"เปลี่ยน|แก้ไข|แก้|ปรับ|ขอเปลี่ยน|ขอแก้|ต้องการเปลี่ยน|ต้องการแก้|อยากเปลี่ยน|อยากแก้|ลืมเลือก|ต้องการ\s*vat|อยากได้\s*vat", re.IGNORECASE)

# CUSTOMER-CSW4-VAT-1 — add_vat's own verb marker. CUS-S04's real
# wording and its natural paraphrases use "add"-shaped verbs the
# generic _CHANGE_VERB_RE never carried ("เพิ่ม VAT", "เอา VAT",
# "ใส่ VAT", "ขอ VAT", "ติ๊ก VAT"), plus "ลืม(เลือก)". Kept SEPARATE
# from _CHANGE_VERB_RE so widening it here cannot loosen the other
# kinds that share that constant — add_vat's object still strictly
# requires the literal "VAT" / "ภาษีมูลค่าเพิ่ม" token, which no
# invoice-issuance / invoice-download FAQ phrasing contains, so the
# wider verb list stays safely walled off from CUS-S07 / CUS-G05.
_VAT_VERB_RE = re.compile(
    r"เปลี่ยน|แก้ไข|แก้|ปรับ|ลืมเลือก|ลืม|ติ๊ก|เลือก|เพิ่ม|ใส่|เอา|ขอ|อยาก|ต้องการ",
    re.IGNORECASE)

# CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — change_carrier_or_selfpickup's
# own verb/context marker. Broader than _CHANGE_VERB_RE ("ขอ", "เอาเป็น",
# "บิลนี้") to catch CUS-S11's real openers, but kept SEPARATE so that
# widening cannot loosen modify_bill_qty / change_shipping_method which
# share _CHANGE_VERB_RE. Still REQUIRED alongside the carrier object, so
# a bare self-pickup FAQ ("รับสินค้าเองได้ไหมคะ") stays RAG.
_CARRIER_VERB_RE = re.compile(
    r"เปลี่ยน|แก้ไข|แก้|ปรับ|ขอ|เอาเป็น|บิลนี้|ต้องการ|อยาก", re.IGNORECASE)

_KINDS = [
    # kind, verb_re (or None), object_re, ack, input_label
    # CUSTOMER-CSW2-QUANTITY-CHANGE-1 — "จำนวนสินค้า" alone (no "บิล"
    # word) still counts as the object: a real-line-style paraphrase
    # ("แก้จำนวนสินค้าได้ไหม") never names the bill explicitly, only the
    # change verb + "quantity of the product". Safe to widen — no other
    # kind's object pattern mentions "จำนวน", so this can't collide with
    # a sibling kind, and the CHANGE VERB is still required.
    ("modify_bill_qty", _CHANGE_VERB_RE,
     re.compile(r"จำนวน(?:สินค้า)?(?:ในบิล|บิล|ที่สั่ง)|จำนวนในบิล|จำนวนสินค้า"),
     "แอดมินขอเลขบิลสั่งซื้อของรายการนี้หน่อยนะคะ", "เลขบิลสั่งซื้อ"),
    # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 — "เป็น(?:ทาง)?รถ|เรือ"
    # (the "ทาง" is now OPTIONAL): a real-line-style paraphrase
    # ("เปลี่ยนจากเรือเป็นรถได้ไหม") never says "ทางรถ"/"ทางเรือ", only
    # the bare mode word after "เป็น" (become). Thai has no word
    # delimiters, so a generic word-boundary lookaround for a bare
    # "รถ"/"เรือ" anywhere in the sentence risks false negatives (fails
    # unless it sits next to non-Thai text) and false positives inside
    # a compound word ("รถไฟ") alike; anchoring on the SAME "เป็น"
    # marker _extract_shipping_target_method() already relies on is
    # both reliable and reuses one mechanism instead of two.
    ("change_shipping_method", _CHANGE_VERB_RE,
     re.compile(r"(?:จัดส่ง|ส่ง|ขนส่ง)[^\n]{0,6}(?:ทางรถ|ทางเรือ)|เป็น(?:ทาง)?(?:รถ|เรือ)|วิธี(?:ส่ง|จัดส่ง|ขนส่ง)"
                r"|ทาง(?:รถ|เรือ)[^\n]{0,6}ได้ไหม"),
     "สามารถเปลี่ยนได้ค่ะ แอดมินรบกวนขอเลขบิลหน่อยนะคะ", "เลขบิล"),
    # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — its own verb regex
    # (_CARRIER_VERB_RE), NOT the shared _CHANGE_VERB_RE: CUS-S11's
    # real-line openers ("บิลนี้ขอรับเอง", "ขอให้ส่งเอกชน", "บิลนี้ขอ
    # ส่งเอกชนค่ะ", "ขอไปรับเอง") carry only a plain "ขอ" / "บิลนี้",
    # which _CHANGE_VERB_RE ("เปลี่ยน/แก้/ปรับ...") never covers, so
    # they used to fall through to RAG. A verb/context marker is still
    # REQUIRED though: a bare "รับสินค้าเองได้ไหมคะ" is a self-pickup
    # FAQ ("is self-pickup possible?"), not a request to change THIS
    # bill — only the change/ask context ("เปลี่ยน", "ขอ", "บิลนี้")
    # makes it operational. The object words themselves ("รับเอง" /
    # "ส่งเอกชน" / "เอกชน" / "flash" / "แฟลช") are unique to a
    # carrier/self-pickup choice — no sibling kind mentions them.
    ("change_carrier_or_selfpickup", _CARRIER_VERB_RE,
     re.compile(r"รับเอง|มารับเอง|มารับสินค้า|รับสินค้าเอง|ไปรับเอง|รับของเอง"
                r"|ส่งเอกชน|เป็นเอกชน|ขนส่งเอกชน|ขนส่งเป็นเอกชน|ส่ง\s*flash|เป็น\s*flash"
                r"|\bflash\b|แฟลช|self[\s-]*pick", re.IGNORECASE),
     "แอดมินรบกวนขอเลขบิลขนส่งหน่อยนะคะ", "เลขบิลขนส่ง"),
    # CUSTOMER-CSW4-VAT-1 — object is now (?<![A-Za-z])vat(?![A-Za-z])
    # instead of \bvat\b: Python's \b sees a Thai letter as a word
    # char, so "รVAT" ("...ต้องการVATด้วยค่ะ", an EXACT CUS-S04 source
    # variant) has NO boundary before "V" and \bvat\b silently missed
    # it. The bare "ใบกำกับภาษี" alternative is dropped — a tax-invoice
    # DOCUMENT request is CUS-S07 / CUS-G05 (RAG / invoice flow), never
    # this "add the VAT flag/charge to my bill" operation; keeping it
    # here would let the widened _VAT_VERB_RE ("ขอ", "ต้องการ") steal
    # "ขอใบกำกับภาษีครับ" / "ต้องการใบกำกับภาษี".
    ("add_vat", _VAT_VERB_RE,
     re.compile(r"(?<![A-Za-z])vat(?![A-Za-z])|ภาษีมูลค่าเพิ่ม", re.IGNORECASE),
     "คุณลูกค้าแจ้งเลขบิลสั่งซื้อที่ต้องการ VAT มาให้แอดมินได้เลยนะคะ", "เลขบิลสั่งซื้อ"),
    ("duplicate_bill", None,
     re.compile(r"บิลซ้ำ|บิลซ้ำกัน|มีบิลซ้ำ|บิลออกมาซ้ำ|บิลตีซ้ำ|บิลเบิ้ล"),
     "แอดมินเช็คบิลซ้ำและลบบิลให้นะคะ รบกวนขอเลขแทรคจีนหน่อยนะคะ", "เลขแทรคจีน"),
    ("verify_warehouse_address",
     re.compile(r"ถูกไหม|ถูกมั้ย|ถูกต้อง|ถูกรึเปล่า|ถูกหรือเปล่า|เช็ค|ตรวจสอบ|\bcheck\b", re.IGNORECASE),
     re.compile(r"(?:ที่อยู่)?โกดังจีน"),
     "แอดมินช่วยตรวจสอบความถูกต้องให้ค่ะ รบกวนแจ้งที่อยู่โกดังจีนที่กรอกไว้ในระบบและรหัสลูกค้ามาให้ตรวจสอบด้วยนะคะ",
     "ที่อยู่โกดังจีนที่กรอกไว้ และรหัสลูกค้า"),
    ("topup_not_credited", None,
     re.compile(r"ยอดเงินไม่เข้า|ยอดไม่เข้า|เงิน(?:ที่เติม)?[^\n]{0,10}(?:ยัง)?ไม่เข้า"
                r"|เติมเงินแล้ว[^\n]{0,16}(?:ไม่เข้า|ยังไม่เข้า|รอตรวจสอบ|ยอดยังไม่ขึ้น)"),
     "สวัสดีค่ะ แอดมินรบกวนขอสลิปการโอนเงินหน่อยนะคะ", "สลิปการโอนเงิน"),
    # CUSTOMER-RED-8 — CUS-G11 (Ai.xlsx sheet '1.thameuangton' row 11):
    # missing/incomplete item claim. Self-describing (no separate verb
    # needed, same shape as duplicate_bill/topup_not_credited above).
    ("missing_item_claim", None,
     re.compile(r"ได้รับสินค้าไม่ครบ|สินค้าไม่ครบ|ของไม่ครบ|เคลมสินค้า|ขอเคลม|สินค้าเสียหาย|ของเสียหาย|พัสดุเสียหาย"),
     "สวัสดีค่ะ คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และรูปหน้าแทรคจีนที่ติดข้างกล่อง กับวิดิโอตอนแกะสินค้า "
     "รวมทั้งรูปสินค้าทั้งหมดที่ได้รับมาให้แอดมินได้เลยนะคะ",
     "เลขบิลสั่งซื้อ, รูปหน้าแทรคจีนที่ติดข้างกล่อง, วิดิโอตอนแกะสินค้า, รูปสินค้าทั้งหมด"),
    # CUSTOMER-RED-8 — CUS-S06 (Ai.xlsx sheet '2.tongchecknairabop' row 6
    # / CSW6): custom production / screen-print / order-to-spec.
    ("custom_production", None,
     re.compile(r"สั่งผลิตตามสเปค|สั่งสกรีนโลโก้|สกรีนโลโก้|สั่งผลิต(?:สินค้า)?ตามสเปค|ผลิตตามสเปค|สั่งทำโลโก้"),
     "คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และแจ้งสเปคสินค้า จำนวน สีกับโลโก้มาให้แอดได้เลยค่ะ แอดจะประสานงานกับทางร้านให้นะคะ",
     "เลขบิลสั่งซื้อ, สเปคสินค้า, จำนวน, สี, โลโก้"),
    # CUSTOMER-RED-8 / CUSTOMER-CHARTER-COMBINE-REAL-1 — CUS-S16
    # (Ai.xlsx sheet '2.tongchecknairabop' row 16 / CSW16): combine
    # multiple bills into one charter-truck shipment. Distinct from a
    # FRESH single-shipment charter request (services/charter_truck_
    # flow.py, opened only after its own TC19 FAQ turn) — this is its
    # own ack + collect + Human-CS coordination shape, reusing the SAME
    # established pattern as every other kind here rather than a
    # bespoke multi-bill collector.
    #
    # REAL LINE (2026-09-05 ~17:13 ICT): the ORIGINAL ack ("รับทราบค่ะ
    # แอดมินรวมบิลที่เข้าไทยเหมารถให้นะคะ") sounds like the combine is
    # ALREADY under way and never actually asks for anything — so a
    # customer who then supplied bill numbers just got the exact SAME
    # sentence echoed back (operational_ask_prompt always returns
    # state.ack, whether this is the first ask or a still-missing
    # retry), reading as if nothing had been collected. Fixed: the ack
    # now explicitly asks for the bill numbers, matching stage A of the
    # 3-stage journey (A. request -> ask bills, B. bills collected ->
    # forward to Human CS, C. real confirmed result only -> completion
    # text) — never implying the combine itself has started.
    ("combine_bills_charter", None,
     re.compile(r"รวมบิล.{0,6}เหมารถ|เหมารถ.{0,6}รวมบิล|รวมบิลขนส่งเหมารถ"),
     "ได้ค่ะ รบกวนแจ้งเลขบิลขนส่งที่ต้องการรวมเหมารถมาได้เลยค่ะ หากมีหลายบิลสามารถส่งมาพร้อมกันได้เลยนะคะ",
     "เลขบิลขนส่งที่ต้องการรวม"),
]

# a delivery-ADDRESS change is a different case (CUS-S09) with its own
# approved Business Action (requestshippingaddresschange) — this flow
# must never steal it.
_ADDRESS_CHANGE_RE = re.compile(r"ที่อยู่จัดส่ง|ที่อยู่ผู้รับ|ที่อยู่ในการจัดส่ง|เปลี่ยนที่อยู่(?!โกดัง)")

_ACK_MARKER_RE = re.compile(
    r"แอดมินขอเลขบิลสั่งซื้อของรายการนี้|แอดมินรบกวนขอเลขบิล|แอดมินเช็คบิลซ้ำและลบบิลให้"
    r"|แอดมินช่วยตรวจสอบความถูกต้องให้|แอดมินรบกวนขอสลิปการโอนเงิน"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อที่ต้องการ\s*VAT"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และรูปหน้าแทรคจีน"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และแจ้งสเปคสินค้า"
    r"|รบกวนแจ้งเลขบิลขนส่งที่ต้องการรวมเหมารถ"
    r"|รบกวนแจ้งด้วยนะคะว่าต้องการเปลี่ยนเป็นทางรถหรือทางเรือคะ"
    r"|รบกวนแจ้งด้วยนะคะว่าต้องการเปลี่ยนเป็นรับเองหรือส่งเอกชนคะ")

_FRAME_LOOKBACK = 10

_BILL_TOKEN_RE = re.compile(r"\b([A-Za-z]{2,4}\d{4,})\b")

# PHASE-1-PURCHASE-BILL-DOMAIN-VALIDATION-1 — kinds whose required bill
# is a PURCHASE ORDER (each one's own customer source template reads
# "เลขบิลสั่งซื้อ PO,PA,POS,PE"). A shipment bill (FT/FE/SA/SP...) is a
# structurally valid identifier but the WRONG domain for these and must
# never satisfy the slot. Kinds that genuinely want a SHIPMENT bill
# (change_carrier_or_selfpickup / combine_bills_charter) and duplicate_
# bill (a China tracking number) are deliberately NOT listed and are
# completely unaffected — FT/FE/SA/SP stays valid for them.
_PURCHASE_BILL_KINDS = frozenset({
    "modify_bill_qty", "change_shipping_method", "add_vat",
    "missing_item_claim", "custom_production",
})
_PURCHASE_BILL_PREFIX_RE = re.compile(r"^(?:PO|PA|POS|PE)\d", re.IGNORECASE)
_SHIPMENT_BILL_PREFIX_RE = re.compile(r"^(?:FT|FE|SA|SP)\d", re.IGNORECASE)


def is_shipment_bill(token: str) -> bool:
    """True for an FT/FE/SA/SP-prefixed bill token (a shipment bill)."""
    return bool(_SHIPMENT_BILL_PREFIX_RE.match((token or "").strip()))


def is_purchase_bill(token: str) -> bool:
    """True for a PO/PA/POS/PE-prefixed bill token (a purchase order)."""
    return bool(_PURCHASE_BILL_PREFIX_RE.match((token or "").strip()))


_WRONG_BILL_DOMAIN_PREFIX = (
    "เลขที่แจ้งมาเป็นบิลขนส่งค่ะ รายการนี้ต้องใช้เลขบิลสั่งซื้อ (PO/PA/POS/PE) นะคะ")

# CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — the mirror: kinds whose
# required bill is a SHIPMENT bill (CUS-S11's api_input_hint:
# "shipment_bill_no ... FT,FE,SA,SP"). A purchase bill (PO/PA/POS/PE)
# is a structurally valid identifier but the wrong domain and must not
# satisfy the slot. combine_bills_charter keeps its own multi-bill
# branch and is out of scope here.
_SHIPMENT_BILL_KINDS = frozenset({"change_carrier_or_selfpickup"})
_WRONG_SHIPMENT_BILL_PREFIX = (
    "เลขที่แจ้งมาเป็นเลขบิลสั่งซื้อค่ะ รายการนี้ต้องใช้เลขบิลขนส่ง (FT/FE/SA/SP) นะคะ")
_PHONE_RE = re.compile(r"(?<!\d)(0\d[\d\- ]{7,10}\d)(?!\d)")

# CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 — CUS-S03's own api_input_hint
# is "bill_no + shipping_type (road/sea)": change_shipping_method needs
# BOTH, unlike every other kind here which needs only one input. When
# the message explicitly says "เปลี่ยน...เป็น X" (change ... TO X), X is
# the decisive TARGET method even if the FROM method is also named in
# the same sentence ("เปลี่ยนจากทางรถเป็นทางเรือ" -> sea, not road).
# Otherwise, a message naming exactly one of the two words is
# unambiguous on its own ("เรือค่ะ" -> sea); naming both with no "เป็น"
# marker (the exact customer wording itself, "...ทางรถ,ทางเรือได้ไหมคะ")
# or naming neither is genuinely ambiguous/missing, and must be asked
# for explicitly rather than guessed.
_TARGET_METHOD_AFTER_RE = re.compile(r"เป็น(?:ทาง)?(รถ|เรือ)")
_ROAD_WORD_RE = re.compile(r"ทางรถ|(?<![ก-๙])รถ(?![ก-๙])")
_SEA_WORD_RE = re.compile(r"ทางเรือ|(?<![ก-๙])เรือ(?![ก-๙])")
_SHIPPING_METHOD_TH = {"road": "ทางรถ", "sea": "ทางเรือ"}
# the two DYNAMIC follow-up prompts operational_ask_prompt() emits once
# ONE of change_shipping_method's two required inputs is already known
# (never its own static _KINDS ack, which asks for the bill only) --
# named here so derive_operational_state()'s open-collection recovery
# can recognize them as the SAME kind too.
_SHIPPING_BILL_ONLY_ASK_PROMPT = "แอดมินรบกวนขอเลขบิลหน่อยนะคะ"
_SHIPPING_METHOD_ASK_PROMPT = "รบกวนแจ้งด้วยนะคะว่าต้องการเปลี่ยนเป็นทางรถหรือทางเรือคะ"


def _extract_shipping_target_method(text: str) -> Optional[str]:
    t = text or ""
    m = _TARGET_METHOD_AFTER_RE.search(t)
    if m:
        return "sea" if m.group(1) == "เรือ" else "road"
    has_road, has_sea = bool(_ROAD_WORD_RE.search(t)), bool(_SEA_WORD_RE.search(t))
    if has_road and not has_sea:
        return "road"
    if has_sea and not has_road:
        return "sea"
    return None


# CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — CUS-S11's api_input_hint is
# "shipment_bill_no + new_carrier/type": change_carrier_or_selfpickup
# needs BOTH a shipment bill AND a target — SELF PICKUP ("รับเอง") or
# PRIVATE/EXTERNAL delivery ("ส่งเอกชน" / Flash). Same two-input shape
# as change_shipping_method (CUS-S03). "เปลี่ยน...เป็น X" names the
# decisive TARGET even when the FROM side is also mentioned
# ("เปลี่ยนจากรับเองเป็นส่งเอกชน" -> private_delivery). A message naming
# exactly one side with no "เป็น" marker is unambiguous ("ขอส่งเอกชน" ->
# private_delivery); naming both without a "เป็น" marker, or neither, is
# missing and must be asked for.
_CARRIER_TARGET_AFTER_RE = re.compile(
    r"เป็น\s*(?:ทาง|ส่ง|มา)?\s*(รับเอง|มารับเอง|มารับ|เอกชน|flash|แฟลช)", re.IGNORECASE)
_CARRIER_SELF_RE = re.compile(r"รับเอง|มารับเอง|มารับสินค้า|รับสินค้าเอง|ไปรับเอง|self[\s-]*pick", re.IGNORECASE)
_CARRIER_PRIVATE_RE = re.compile(
    r"ส่งเอกชน|เป็นเอกชน|ขนส่งเอกชน|เอกชน|ส่ง\s*flash|เป็น\s*flash|\bflash\b|แฟลช", re.IGNORECASE)
# a NEGATED private mention ("ไม่ต้องส่งเอกชนแล้ว ขอไปรับเอง" — an EXACT
# CUS-S11 self-pickup opener) states what the customer NO LONGER wants,
# not the target; it must not count as a private-delivery signal or the
# sentence reads as naming both sides and resolves to nothing.
_CARRIER_PRIVATE_NEG_RE = re.compile(
    r"ไม่(?:ต้อง|เอา|ใช้|อยาก)?\s*(?:จะ)?\s*(?:ส่ง)?\s*(?:เอกชน|flash|แฟลช)", re.IGNORECASE)
_CARRIER_TARGET_TH = {"self_pickup": "รับเอง", "private_delivery": "ส่งเอกชน"}
_CARRIER_TARGET_ASK_PROMPT = "รบกวนแจ้งด้วยนะคะว่าต้องการเปลี่ยนเป็นรับเองหรือส่งเอกชนคะ"


def _extract_carrier_target(text: str) -> Optional[str]:
    t = text or ""
    m = _CARRIER_TARGET_AFTER_RE.search(t)
    if m:
        g = m.group(1).lower()
        return "self_pickup" if ("รับเอง" in g or "มารับ" in g) else "private_delivery"
    has_self = bool(_CARRIER_SELF_RE.search(t))
    has_priv = bool(_CARRIER_PRIVATE_RE.search(t)) and not _CARRIER_PRIVATE_NEG_RE.search(t)
    if has_self and not has_priv:
        return "self_pickup"
    if has_priv and not has_self:
        return "private_delivery"
    return None


def classify_operational_request(message: str, interpretation: Optional[object] = None) -> Optional[Dict]:
    """Return {kind, ack, input_label} for an operational change/verify
    request with no executable action, else None. A kind with a verb
    marker needs BOTH its verb and its object to appear (order-
    independent); the self-describing kinds need only their object.
    Excludes the delivery-address change (CUS-S09 — has its own
    Business Action)."""
    t = message or ""
    if _ADDRESS_CHANGE_RE.search(t):
        return None
    for kind, verb_re, obj_re, ack, label in _KINDS:
        if not obj_re.search(t):
            continue
        if verb_re is not None and not verb_re.search(t):
            continue
        return {"kind": kind, "ack": ack, "input_label": label}
    return None


@dataclass
class OperationalState:
    kind: str = ""
    ack: str = ""
    input_label: str = ""
    bill: Optional[str] = None          # bill / tracking token, when supplied
    # CUSTOMER-CHARTER-COMBINE-1.1 — combine_bills_charter's own
    # required input is a LIST of shipment bills (CUS-S16's
    # api_input_hint: "shipment_bill_no_list"), unlike every other kind
    # here which needs only ONE. Kept as its own field rather than
    # overloading `bill` so every existing single-bill kind is
    # completely unaffected.
    bills: List[str] = field(default_factory=list)
    free_text: Optional[str] = None     # address / other free-form detail
    phone: Optional[str] = None
    # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 — change_shipping_method's
    # own second required input ("road" | "sea"); every other kind
    # leaves this None and is completely unaffected.
    shipping_method: Optional[str] = None
    # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — change_carrier_or_self
    # pickup's own second required input ("self_pickup" | "private_
    # delivery"); every other kind leaves this None and is unaffected.
    carrier_target: Optional[str] = None
    # PHASE-1-PURCHASE-BILL-DOMAIN-VALIDATION-1 — set when a SHIPMENT bill
    # (FT/FE/SA/SP...) was supplied for a _PURCHASE_BILL_KINDS kind;
    # drives operational_ask_prompt to explain + re-ask, and keeps
    # has_input() False so nothing is handed off. Cleared the moment a
    # valid purchase bill arrives.
    wrong_domain_bill: Optional[str] = None

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v}

    def has_input(self) -> bool:
        if self.kind == "change_shipping_method":
            # CUS-S03's own api_input_hint requires BOTH the bill and
            # the target method — the ONE kind here needing an AND,
            # not an OR, of its collected fields.
            return bool(self.bill and self.shipping_method)
        if self.kind == "change_carrier_or_selfpickup":
            # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — CUS-S11 likewise
            # needs BOTH the shipment bill AND the target (self pickup /
            # private delivery).
            return bool(self.bill and self.carrier_target)
        return bool(self.bill or self.bills or self.free_text or self.phone)


_CN_TRACKING_RE = re.compile(r"(?<!\d)(\d{9,16})(?!\d)")


def extract_operational_fields(message: str, into: OperationalState) -> OperationalState:
    t = message or ""
    if into.kind == "combine_bills_charter":
        # multiple bills may arrive in one message ("FT001 FT002") or
        # accumulate across turns ("FT001" then later "FT002 ด้วยค่ะ") —
        # every genuinely NEW bill token is appended, never replacing an
        # earlier one, so nothing already supplied is lost.
        for tok in _BILL_TOKEN_RE.findall(t):
            if _valid_id(tok) and not tok.isdigit():
                tok_u = tok.upper()
                if tok_u not in into.bills:
                    into.bills.append(tok_u)
    elif not into.bill:
        m = _BILL_TOKEN_RE.search(t)
        if m and _valid_id(m.group(1)) and not m.group(1).isdigit():
            _tok = m.group(1).upper()
            if into.kind in _PURCHASE_BILL_KINDS and is_shipment_bill(_tok):
                # PHASE-1-PURCHASE-BILL-DOMAIN-VALIDATION-1 — right shape,
                # wrong domain: never bind it, never hand off; flag it so
                # operational_ask_prompt explains and re-asks.
                into.wrong_domain_bill = _tok
            elif into.kind in _SHIPMENT_BILL_KINDS and is_purchase_bill(_tok):
                # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — the mirror
                # direction: a purchase bill (PO/PA/POS/PE) is the wrong
                # domain for a shipment-bill kind; flag, explain, re-ask.
                into.wrong_domain_bill = _tok
            else:
                into.bill = _tok
                into.wrong_domain_bill = None
        elif into.kind == "duplicate_bill":
            # a CN tracking is typically an all-digit run
            mt = _CN_TRACKING_RE.search(t)
            if mt:
                into.bill = mt.group(1)
    if not into.phone:
        m = _PHONE_RE.search(t)
        if m:
            into.phone = re.sub(r"[\s\-]", "", m.group(1))
    if into.kind == "change_shipping_method" and not into.shipping_method:
        method = _extract_shipping_target_method(t)
        if method:
            into.shipping_method = method
    if into.kind == "change_carrier_or_selfpickup":
        # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — OVERWRITE (not
        # setdefault) whenever THIS message decisively names a target, so
        # an in-episode correction ("ขอรับเอง" -> "เปลี่ยนใจ ขอส่งเอกชน")
        # lands on the latest one. In the accumulate-since-ack replay the
        # turns are processed oldest-first, so the most recent decisive
        # mention wins; a bare bill turn names no target and leaves an
        # earlier one intact.
        tgt = _extract_carrier_target(t)
        if tgt:
            into.carrier_target = tgt
    # for the verify-address / slip cases a free-text detail counts as the
    # required input once the customer replies with something substantive.
    if into.kind in ("verify_warehouse_address", "topup_not_credited") and not into.free_text:
        stripped = t.strip()
        if len(stripped) >= 6 and not classify_operational_request(stripped):
            into.free_text = stripped[:400]
    return into


def derive_operational_state(history: Optional[List[Dict]], current_message: str,
                             interpretation: Optional[object] = None) -> Optional[OperationalState]:
    """Reconstruct an active operational-change collection, or open a new
    one from `current_message`. Returns None when nothing operational is
    in play — including right after the previous episode was handed
    off, UNLESS `current_message` itself is a fresh, decisive
    operational request (never blocked by an unrelated, already-closed
    prior episode)."""
    turns = list(history or [])[-_FRAME_LOOKBACK:]

    # 1/2. whether an episode is still OPEN is decided SOLELY by the
    # MOST RECENT assistant turn matching an ACK marker — never by
    # scanning the whole lookback window for a done-marker, and never
    # by treating a done-marker as its own early-exit either.
    #
    # CUSTOMER-CSW2-REAL-1 fixed the first form of this bug: a
    # done-marker from an EARLIER, unrelated, already-closed episode
    # sitting anywhere within the last _FRAME_LOOKBACK turns silently
    # blocked EVERY later operational request in the window.
    #
    # CUSTOMER-CSW2-REAL-2 (this fix) — narrowing the done-marker check
    # to only the MOST RECENT turn was not enough: `return None`
    # there STILL short-circuited step 3 (classify a FRESH request from
    # the current message) even when the done-marker was the episode
    # THIS VERY NEW request is unrelated to (REAL LINE: "แก้จำนวนสินค้า
    # ได้ไหม" asked immediately after modify_bill_qty's OWN handoff
    # closed — a textbook case of "current explicit intent must not be
    # blocked by the immediately-preceding, already-finished episode").
    # A done-marker is NEVER also an ack (the two patterns are disjoint
    # by construction), so simply not matching _ACK_MARKER_RE already
    # leaves `open_kind` at None and lets step 3 run normally — no
    # special-cased early return is needed at all; a done-marker just
    # means "nothing open", exactly like any other unrelated reply.
    open_kind = None
    for t in reversed(turns):
        if t.get("role") != "assistant":
            continue
        c = t.get("content") or ""
        if _ACK_MARKER_RE.search(c):
            # recover which kind from the ack wording. Match on the FULL
            # ack text, never a truncated prefix: several kinds share an
            # identical opening clause ("คุณลูกค้าแจ้งเลขบิลสั่งซื้อ...") —
            # add_vat / missing_item_claim / custom_production all start
            # this way. A truncated N-char prefix check (`ack[:24] in c`)
            # matches whichever kind is EARLIEST in this list purely
            # because its short prefix happens to appear inside a LATER
            # kind's longer, different ack (real production bug: a
            # missing_item_claim ack was recovered as add_vat, then every
            # unrelated follow-up intent — Purchase Withdrawal, Shipping
            # Withdrawal, Custom Production, Charter Combine — was
            # answered with the wrong, stale "VAT" ack). The FULL ack
            # strings are themselves mutually non-overlapping, so this
            # substring check is unambiguous.
            for kind, _vrx, _orx, ack, label in _KINDS:
                if ack in c:
                    open_kind = (kind, ack, label)
                    break
            else:
                # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 -- once bill
                # OR method is already known, operational_ask_prompt()
                # emits a DYNAMIC follow-up ('...ขอเลขบิลหน่อยนะคะ' /
                # '...ทางรถหรือทางเรือคะ') asking ONLY for whatever is
                # still missing -- neither IS change_shipping_method's
                # own static _KINDS ack verbatim, so the generic loop
                # above never finds it (real production bug: the very
                # NEXT turn supplying the still-missing piece was read
                # as a fresh, unrelated message and fell through to an
                # ERP read of the bill number instead of continuing
                # this collection). Recognized here as the SAME kind,
                # using its one true static ack/label from _KINDS so
                # state.ack still reads correctly if either field is
                # later cleared/re-asked.
                if c.strip() in (_SHIPPING_METHOD_ASK_PROMPT, _SHIPPING_BILL_ONLY_ASK_PROMPT):
                    _csm = next((k for k in _KINDS if k[0] == "change_shipping_method"), None)
                    if _csm:
                        open_kind = (_csm[0], _csm[3], _csm[4])
                # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — same, for
                # change_carrier_or_selfpickup's dynamic target-ask prompt
                # (its static bill-ask ack IS in _KINDS and is recovered
                # by the generic loop above).
                elif c.strip() == _CARRIER_TARGET_ASK_PROMPT:
                    _ccp = next((k for k in _KINDS if k[0] == "change_carrier_or_selfpickup"), None)
                    if _ccp:
                        open_kind = (_ccp[0], _ccp[3], _ccp[4])
        break

    if open_kind:
        kind, _ack_txt, label = open_kind
        st = OperationalState(kind=kind, ack=_ack_txt, input_label=label)
        # accumulate every user turn since the ack — PLUS the one user
        # turn that TRIGGERED the very first ack, which is otherwise
        # silently lost on every later turn: step 3 (a fresh request)
        # extracts fields from that message directly on the turn it
        # arrives, but once the conversation moves on and this branch
        # reconstructs the episode from `history` instead, that same
        # message is never revisited (accumulation only starts AFTER
        # the ack, never at-or-before it). Invisible for every kind
        # needing only ONE input (its trigger message finishing early
        # supplies that on turn 1 itself, before this branch is ever
        # reached again) but a real loss for change_shipping_method
        # (CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1): its trigger message
        # can name the target method while the bill is still missing,
        # and that method must survive into the bill-supplying turn.
        # extract_operational_fields never overwrites an already-set
        # field (bill/shipping_method) and only appends genuinely NEW
        # bills, so re-processing this one turn is always safe.
        # CUSTOMER-CSW3-REAL-1 -- a HANDOFF reply anywhere inside this
        # same window closes whatever episode came before it. Without
        # this, an OLD, already-completed episode of the SAME kind
        # sitting earlier in the lookback window gets its fields
        # accumulated together with a NEW episode that reopened
        # afterward, and since extract_operational_fields never
        # overwrites an already-set field, the OLD (stale) value wins
        # and the NEW episode's own, correct value is silently
        # discarded (real production bug: a completed
        # change_shipping_method->sea episode, followed by a fresh
        # "เปลี่ยนจากเรือเป็นรถได้ไหม" + bill request, kept replying
        # "...เป็นทางเรือ" -- the stale target from the FIRST episode --
        # instead of the new "ทางรถ"). Reset the accumulator (and the
        # ack-seen flag) at each handoff boundary so only turns from
        # the LAST (current) episode are ever collected.
        seen_ack = False
        for i, t in enumerate(turns):
            if t.get("role") == "assistant" and _HANDOFF_REPLY_RE.search(t.get("content") or ""):
                st = OperationalState(kind=kind, ack=_ack_txt, input_label=label)
                seen_ack = False
                continue
            if t.get("role") == "assistant" and _ACK_MARKER_RE.search(t.get("content") or ""):
                if not seen_ack and i > 0 and turns[i - 1].get("role") == "user":
                    extract_operational_fields(turns[i - 1].get("content") or "", st)
                seen_ack = True
                continue
            if seen_ack and t.get("role") == "user":
                extract_operational_fields(t.get("content") or "", st)
        return st

    # 3. a fresh request in the current message.
    cls = classify_operational_request(current_message, interpretation)
    if not cls:
        return None
    st = OperationalState(kind=cls["kind"], ack=cls["ack"], input_label=cls["input_label"])
    extract_operational_fields(current_message, st)
    return st


_KIND_TH = {
    "modify_bill_qty": "ขอแก้จำนวนสินค้าในบิล",
    "change_shipping_method": "ขอเปลี่ยนวิธีจัดส่ง (ทางรถ/ทางเรือ)",
    "change_carrier_or_selfpickup": "ขอเปลี่ยนเป็นรับเอง / เปลี่ยนขนส่งเอกชน",
    "add_vat": "ขอเพิ่ม VAT / ใบกำกับภาษี ในบิลสั่งซื้อ",
    "duplicate_bill": "แจ้งบิลซ้ำ ขอให้ตรวจสอบและลบบิลซ้ำ",
    "verify_warehouse_address": "ขอให้ตรวจสอบความถูกต้องของที่อยู่โกดังจีนที่กรอกไว้",
    "topup_not_credited": "แจ้งเติมเงินแล้วยอดยังไม่เข้า ขอให้ตรวจสอบ",
    "missing_item_claim": "แจ้งได้รับสินค้าไม่ครบ/เสียหาย ขอเคลม",
    "custom_production": "ขอสั่งผลิตสินค้าตามสเปค / สกรีนโลโก้",
    "combine_bills_charter": "ขอรวมบิลขนส่งที่เข้าไทยเป็นเหมารถ",
}


def operational_ask_prompt(state: OperationalState) -> str:
    # PHASE-1-PURCHASE-BILL-DOMAIN-VALIDATION-1 -- a shipment bill was
    # given where a purchase bill is required: PREPEND the explanation to
    # this kind's own ack (never a standalone prompt -- the ack text is
    # what derive_operational_state's _ACK_MARKER_RE recovery keys on, so
    # a standalone prompt would silently break the still-open
    # collection). Checked before every other prompt shape.
    if state.wrong_domain_bill and not state.bill:
        # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — the explanation points
        # the customer at the domain THIS kind needs: purchase bill for
        # _PURCHASE_BILL_KINDS, shipment bill for _SHIPMENT_BILL_KINDS.
        _prefix = (
            _WRONG_SHIPMENT_BILL_PREFIX
            if state.kind in _SHIPMENT_BILL_KINDS
            else _WRONG_BILL_DOMAIN_PREFIX
        )
        return f"{_prefix}\n{state.ack}"
    # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 -- once the bill is
    # already known but the target method is still missing (or vice
    # versa), re-showing state.ack in full would re-ask for the bill
    # even though the customer already gave it. Ask ONLY for whatever
    # is still missing; when NEITHER is known yet, state.ack (the
    # source's own first-stage wording, asking for the bill) is
    # unchanged.
    if state.kind == "change_shipping_method":
        if state.bill and not state.shipping_method:
            return _SHIPPING_METHOD_ASK_PROMPT
        if state.shipping_method and not state.bill:
            return _SHIPPING_BILL_ONLY_ASK_PROMPT
    if state.kind == "change_carrier_or_selfpickup":
        # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — ask ONLY for whatever
        # is still missing. state.ack ("แอดมินรบกวนขอเลขบิลขนส่งหน่อยนะคะ")
        # already asks for the bill, so it covers both "neither known"
        # and "target known, bill missing".
        if state.bill and not state.carrier_target:
            return _CARRIER_TARGET_ASK_PROMPT
    return state.ack


def operational_handoff_summary(state: OperationalState) -> str:
    parts = [_KIND_TH.get(state.kind, state.kind)]
    if state.bills:
        parts.append("เลขบิลขนส่งที่ต้องการรวม: " + ", ".join(state.bills))
    if state.bill:
        parts.append(f"เลขบิล/แทรค: {state.bill}")
    if state.shipping_method:
        parts.append(f"วิธีขนส่งที่ต้องการเปลี่ยนเป็น: {_SHIPPING_METHOD_TH[state.shipping_method]}")
    if state.carrier_target:
        parts.append(f"ขนส่งที่ต้องการเปลี่ยนเป็น: {_CARRIER_TARGET_TH[state.carrier_target]}")
    if state.phone:
        parts.append(f"เบอร์: {state.phone}")
    if state.free_text:
        parts.append(f"รายละเอียด: {state.free_text[:200]}")
    return "คำขอดำเนินการกับบัญชีลูกค้า — " + " | ".join(parts)


# the neutral reply once the input is collected — carries NO staff
# promise; line_bot/webhook.py appends the coordination line
# (" เดี๋ยวเจ้าหน้าที่จะติดต่อดำเนินการให้นะคะ") only when a real Human CS
# notification for THIS episode actually goes out (or is already NOTIFIED).
OPERATIONAL_HANDOFF_REPLY = "รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ"

# CUSTOMER-CHARTER-COMBINE-1.1 / -REAL-1 -- CUS-S16's own source (Ai.xlsx
# sheet '2.tongchecknairabop' row 16 / CSW16) marks this a Human-CS-only
# operation (H19="Human CS": staff physically combine the bills and
# adjust their charter/เหมารถ status in the external warehouse system --
# there is no API this platform can call to do it, and no way for this
# platform to learn the result automatically either). The customer-
# approved THIRD-stage answer ("รวมบิลเหมารถเรียบร้อยค่ะ, เป็นบิล ...")
# is only valid once that real, staff-confirmed result exists -- it is
# NEVER used here, since collecting the bill list is not that
# confirmation. This is stage B of the 3-stage journey (A. ask bills,
# B. bills collected -> forward to Human CS/logistics -- THIS reply,
# C. real confirmed result only -> completion text): it honestly says
# the BILL LIST (not the combine itself) was received and forwarded.
# Every other kind keeps the generic OPERATIONAL_HANDOFF_REPLY, unchanged.
_KIND_HANDOFF_REPLY = {
    "combine_bills_charter": (
        "รับข้อมูลบิลที่ต้องการรวมแล้วค่ะ เดี๋ยวแอดมิน/ทีมขนส่งตรวจสอบและดำเนินการรวมบิลเหมารถให้นะคะ"),
    # CUSTOMER-CSW4-VAT-1 — CUS-S04 is a "Write API — เพิ่ม VAT flag"
    # in the source, but no such action is wired (same as CSW3), and
    # the source's own stage-2 text is informational guidance with a
    # human-filled amount ("จำนวน .....") and a bank account, never an
    # automated completion. So this stays a Human-CS handoff with an
    # honest "request received, admin will check & proceed" reply that
    # NAMES VAT — never "เพิ่ม VAT เรียบร้อยแล้ว".
    "add_vat": (
        "รับเรื่องขอเพิ่ม VAT ในบิลแล้วค่ะ เดี๋ยวแอดมินตรวจสอบและดำเนินการให้นะคะ"),
}


def operational_handoff_reply(state: OperationalState) -> str:
    # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 -- CUS-S03's own source
    # (Ai.xlsx row 3 / CSW3) has no wired write API for changing the
    # shipping method (confirmed via a live registry audit -- only
    # searchdatashipment/searchdatashipmentlist/requestshippingaddress
    # change exist, none of them a method-change write), so this stays
    # Human-CS-only, exactly like combine_bills_charter: an honest
    # "request received" reply naming the TARGET method, never the
    # source's own literal completion wording ("...ให้เรียบร้อยค่ะ"),
    # which stays reserved for a real staff-confirmed result this
    # platform has no way to detect automatically.
    if state.kind == "change_shipping_method" and state.shipping_method:
        return (
            f"รับเรื่องขอเปลี่ยนวิธีขนส่งเป็น{_SHIPPING_METHOD_TH[state.shipping_method]}แล้วค่ะ "
            "เดี๋ยวแอดมินตรวจสอบและดำเนินการเปลี่ยนให้นะคะ")
    # CUSTOMER-CSW11-CHANGE-DELIVERY-METHOD-1 — CUS-S11 is a "Write API —
    # เปลี่ยนขนส่ง" in the source, but no such write action is wired
    # (live registry audit: only searchdatashipment / searchdata
    # shipmentlist / requestshippingaddresschange exist). So it stays
    # Human-CS-only, exactly like change_shipping_method: an honest
    # "request received, admin/logistics will verify & process" reply
    # that NAMES the target — never the source's literal completion
    # wording ("...ให้เรียบร้อยค่ะ"), reserved for a real staff-confirmed
    # result this platform cannot detect automatically.
    if state.kind == "change_carrier_or_selfpickup" and state.carrier_target:
        return (
            f"รับเรื่องขอเปลี่ยนขนส่งเป็น{_CARRIER_TARGET_TH[state.carrier_target]}แล้วค่ะ "
            "เดี๋ยวแอดมิน/ทีมขนส่งตรวจสอบและดำเนินการเปลี่ยนให้นะคะ")
    return _KIND_HANDOFF_REPLY.get(state.kind, OPERATIONAL_HANDOFF_REPLY)


# CUSTOMER-CSW3-REAL-1 -- recognizes ANY of the handoff replies above
# (the generic one, every _KIND_HANDOFF_REPLY override, and change_
# shipping_method's own method-naming one, matched on its stable prefix
# since the method name itself varies) as an EPISODE-CLOSING reply, so
# derive_operational_state's reconstruction can tell "an already-closed
# episode's own turns" apart from "a NEW episode that reopened the SAME
# kind after it". Never used as an early-exit gate (that caused
# CUSTOMER-CSW2-REAL-2's regression) -- only as a reset point inside the
# accumulation loop below.
_HANDOFF_REPLY_RE = re.compile(
    re.escape(OPERATIONAL_HANDOFF_REPLY) + "|"
    + "|".join(re.escape(v) for v in _KIND_HANDOFF_REPLY.values())
    + "|รับเรื่องขอเปลี่ยนวิธีขนส่งเป็น"
    + "|รับเรื่องขอเปลี่ยนขนส่งเป็น")
