# -*- coding: utf-8 -*-
"""PHASE-6C — Request Grounding Classifier (Smart General Assistance).

Given ONE customer message + the central Interpretation, decide what kind
of GROUNDING the answer needs, so the pipeline can answer a genuine
general-knowledge question naturally without ever fabricating a Shipify
business fact:

    BUSINESS_TRUTH_REQUIRED  price / policy / rate / ETA / warranty / a
                             Shipify-specific process — MUST come from
                             trusted RAG / config / API; never invented.
    PRIVATE_OR_ERP_REQUIRED  the customer's OWN record / account /
                             transaction — auth + ERP / workflow only.
    GENERAL_ASSISTANCE       general logistics / packing / import
                             know-how the LLM may answer from general
                             knowledge (framed as general info).
    MIXED                    both a general portion AND a Shipify
                             business portion in the same turn — answer
                             the general part, ground the business part
                             separately.
    UNCLEAR                  not enough signal — ask ONE natural
                             clarification.

Pure and deterministic — a small set of orthogonal markers, COMPOSED
(the same shape as services/conversation_semantics.py::_compose). No LLM,
no policy verdict, no personal data. It never *lowers* grounding: a
message that carries any Shipify-business or private marker can never be
classified GENERAL_ASSISTANCE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

CLASSES = ("BUSINESS_TRUTH_REQUIRED", "PRIVATE_OR_ERP_REQUIRED",
           "GENERAL_ASSISTANCE", "MIXED", "UNCLEAR")

# ── markers ─────────────────────────────────────────────────────────
# A Shipify-SPECIFIC business fact: the company's own price / rate / fee
# / policy / ETA / warranty / promotion / process. "how much does
# Shipify charge", "Shipify's policy", "your rate", "ค่าห่อกี่บาท".
_BIZ_FACT_RE = re.compile(
    r"shipify|บริษัท(?!อื่น)|ของทางเรา|ทางร้าน|"
    r"(?:ค่า(?:ส่ง|ขนส่ง|บริการ|ห่อ|แพ็ก|แพค|ตีลัง|ตีลังไม้|นำเข้า|ธรรมเนียม|dropoff|จัดส่ง)|เรท|ค่าธรรมเนียม)"
    r"\S{0,12}(?:เท่าไหร่|เท่าไร|กี่บาท|ราคา|คิด(?:ยังไง|เท่าไร|เท่าไหร่)?|แพงไหม|เพิ่มไหม)"
    r"|(?:นโยบาย|เงื่อนไข|การรับประกัน|ประกัน|เคลม|การชำระเงิน|ระยะเวลา(?:ขนส่ง|ส่งของ)|ETA|โปรโมชั่น|โปรโมชัน|ส่วนลด|คูปอง)\S{0,10}(?:ของ|shipify|บริษัท|เป็นยังไง|ยังไง|มีไหม|คืออะไร)"
    r"|(?:กี่วัน|ใช้เวลากี่วัน|นานไหม)\S{0,6}(?:ถึง|ส่ง|ขนส่ง)"
    r"|รับประกันไหม|การันตีไหม|เคลมได้ไหม",
    re.IGNORECASE)

# A PRIVATE / account-scoped request — the customer's OWN record.
_PRIVATE_RE = re.compile(
    r"ของผม|ของฉัน|ของดิฉัน|ของหนู|ของเรา|บิลผม|บิลฉัน|ออเดอร์ผม|ออเดอร์ฉัน|พัสดุผม|พัสดุฉัน|"
    r"บัญชีผม|บัญชีฉัน|ยอดเงินผม|ยอดเงินฉัน|วอลเล็ทผม|เลขบิลผม|ผมสั่ง|ฉันสั่ง|ที่ผมสั่ง|ที่ฉันสั่ง|"
    r"เช็ก?สถานะ(?:บิล|ออเดอร์|พัสดุ)|ติดตาม(?:บิล|พัสดุ|ออเดอร์)|"
    r"(?<![ก-๙])FT\d{3,}|(?<![ก-๙])SP\d{3,}|(?<![ก-๙])PO\d{5,}",
    re.IGNORECASE)

# A GENERAL logistics / packing / import / e-commerce know-how question —
# advice the LLM can safely give from general knowledge.
_GENERAL_KNOWHOW_RE = re.compile(
    r"(?:แพ็ก|แพค|ห่อ|บับเบิล|กันกระแทก|กันแตก|โฟม|พันฟิล์ม|กันชื้น|ซิลิก้า)\S{0,12}(?:ยังไง|อย่างไร|แบบไหน|ดี|แนะนำ|ควร|ปลอดภัย|ไม่แตก)"
    r"|(?:ต้อง|ควร)ใช้(?:ลังไม้|ตีลัง|โฟม|บับเบิล)ไหม"
    r"|(?:ควร|ต้อง|วิธี)\S{0,4}(?:แพ็ก|แพค|ห่อ|เตรียม|เลือก)\S{0,20}(?:ยังไง|อย่างไร|แบบไหน|ดี)"
    r"|ของ(?:แตกง่าย|เปราะ|บอบบาง|แก้ว|เซรามิก|กระเบื้อง|อิเล็กทรอนิกส์)\S{0,16}(?:แพ็ก|ห่อ|ส่ง|ยังไง|ควร|ไหม|มั้ย|หรือเปล่า|แบบไหน)"
    r"|(?:แตกง่าย|เปราะบาง|บอบบาง)(?:ไหม|มั้ย|หรือเปล่า|รึเปล่า)"
    r"|(?:CBM|ปริมาตร|น้ำหนักเชิงปริมาตร|volumetric)\s*(?:คือ|คำนวณ|หมายถึง)?\s*(?:อะไร|ยังไง)?"
    r"|(?:นำเข้า|import|ชิปปิ้ง|shipping)\S{0,16}(?:ทั่วไป|โดยทั่วไป|เบื้องต้น|มือใหม่|เริ่มต้น|เริ่มยังไง|คืออะไร|หมายถึงอะไร)"
    r"|มือใหม่\S{0,16}(?:นำเข้า|เริ่ม)"
    r"|(?:ภาษีนำเข้า|customs|ศุลกากร|HS\s*code|incoterm)\S{0,10}(?:คือ|ทำงานยังไง|คำนวณ|หมายถึง|คิดจากอะไร)"
    r"|(?:ทางเรือ|ทางรถ|ทางอากาศ)\S{0,8}(?:ต่างกัน|ต่างยังไง|ต่างตรงไหน|เลือกยังไง|ข้อดี|ข้อเสีย)"
    r"|(?:สินค้าอันตราย|ของเหลว|แบตเตอรี่)\S{0,16}(?:ส่งได้ไหมโดยทั่วไป|ปกติส่งได้ไหม|ทั่วไป)",
    re.IGNORECASE)

# A calculation intent — has its own flow, never "general assistance".
_CALC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:มม\.?|mm|ซม\.?|cm|ม\.?|m|นิ้ว|inch)?\s*[x×*]\s*\d+(?:\.\d+)?\s*(?:มม\.?|mm|ซม\.?|cm)?\s*[x×*]\s*\d+"
    r"|คำนวณ\S{0,6}(?:ค่าส่ง|ค่าขนส่ง)|ประเมิน\S{0,6}(?:ค่าส่ง|ค่าขนส่ง|ราคา)",
    re.IGNORECASE)

_QUESTION_RE = re.compile(r"ไหม|มั้ย|หรือเปล่า|รึเปล่า|ยังไง|อย่างไร|เท่าไหร่|เท่าไร|กี่|อะไร|ทำไม|ไหน|\?")
_MIXED_CONJ_RE = re.compile(r"แล้ว|และ|กับ|ส่วน|อีกอย่าง|นอกจากนี้|ด้วยว่า|พร้อมทั้ง|อีกเรื่อง")


@dataclass
class GroundingClass:
    cls: str = "UNCLEAR"
    reason: str = ""
    general_part: bool = False
    business_part: bool = False
    private_part: bool = False

    def as_dict(self):
        return {"class": self.cls, "reason": self.reason,
                "general_part": self.general_part, "business_part": self.business_part,
                "private_part": self.private_part}


# families that already resolve to a GROUNDED route (trusted RAG / ERP /
# calculator / workflow) — the classifier reports their grounding class
# and never overrides. The pre-RAG CONVERSATIONAL service families
# (HELP / SERVICE_DISCOVERY / IMPORT_INTEREST / MONEY_TRANSFER /
# CONTACT_INFO / WEBSITE_LINK_REQUEST / WAREHOUSE_INBOUND_JOURNEY) are
# NOT here: they route pre-RAG on their own and carry no
# business-truth / private grounding requirement, so a message that is
# also general how-to falls through to marker analysis.
_OWNED_BUSINESS = frozenset({
    "PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE",
    "CHARTER_TRUCK", "SHIPPING_ESTIMATE", "LINK_CONVERSION",
    "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL",
})
# PRODUCT_POLICY is deliberately NOT short-circuited. It straddles two
# request kinds: a genuine Shipify prohibited-goods / warranty / handling
# POLICY question (company truth) AND a general packing / fragile-goods
# how-to that services/conversation_semantics.py::_compose merely bucketed
# here by its product verb ("ของแตกง่ายควรแพ็กยังไงดี"). Marker analysis
# below resolves it: an explicit general-knowhow shape with no company
# marker -> GENERAL_ASSISTANCE; anything else -> BUSINESS_TRUTH_REQUIRED.
_PRODUCT_POLICY_FAM = "PRODUCT_POLICY"
_OWNED_PRIVATE = frozenset({"SHIPMENT_STATUS", "INVOICE", "MY_COUPONS", "ADDRESS_CHANGE"})


def classify_request_grounding(message: str, interpretation=None,
                               history=None) -> GroundingClass:
    """Deterministic 5-way grounding classification. Only meaningful when
    the central interpreter did NOT already claim the turn for one of its
    own families (those return a pass-through result the caller ignores)."""
    t = (message or "").strip()
    fam = getattr(interpretation, "intent_family", "UNKNOWN") if interpretation else "UNKNOWN"

    if fam in _OWNED_PRIVATE:
        return GroundingClass(cls="PRIVATE_OR_ERP_REQUIRED", reason=f"owned_family:{fam}",
                              private_part=True)
    if fam in _OWNED_BUSINESS:
        return GroundingClass(cls="BUSINESS_TRUTH_REQUIRED", reason=f"owned_family:{fam}",
                              business_part=True)

    if not t or len(t) < 3:
        return GroundingClass(cls="UNCLEAR", reason="empty_or_tiny")

    biz = bool(_BIZ_FACT_RE.search(t))
    priv = bool(_PRIVATE_RE.search(t)) or bool(getattr(interpretation, "is_private", False))
    gen = bool(_GENERAL_KNOWHOW_RE.search(t))
    calc = bool(_CALC_RE.search(t))
    is_q = bool(_QUESTION_RE.search(t)) or fam in ("GENERAL", "UNKNOWN")

    if calc and not (biz or priv):
        # a calculator payload — not this classifier's concern
        return GroundingClass(cls="BUSINESS_TRUTH_REQUIRED", reason="calculator_payload",
                              business_part=True)

    if priv:
        return GroundingClass(cls="PRIVATE_OR_ERP_REQUIRED", reason="private_record_marker",
                              private_part=True, business_part=biz)

    if biz and gen:
        return GroundingClass(cls="MIXED", reason="general_knowhow + shipify_business_fact",
                              general_part=True, business_part=True)
    if biz:
        return GroundingClass(cls="BUSINESS_TRUTH_REQUIRED", reason="shipify_business_fact",
                              business_part=True)
    if gen:
        return GroundingClass(cls="GENERAL_ASSISTANCE", reason="general_logistics_knowhow",
                              general_part=True)

    if fam == _PRODUCT_POLICY_FAM:
        # an unmarked product-policy turn — no decisive general-knowhow
        # shape, no explicit company-fact marker (e.g. "ส่งน้ำหอมได้ไหม").
        # Keep it company-grounded; never a general-chat guess.
        return GroundingClass(cls="BUSINESS_TRUTH_REQUIRED", reason="product_policy_default",
                              business_part=True)

    # a plain question that named no company fact, no private record, and
    # no clear general-knowhow shape — and the interpreter couldn't place
    # it. Ask one clarification rather than guessing a route.
    if is_q and fam in ("GENERAL", "UNKNOWN"):
        return GroundingClass(cls="UNCLEAR", reason="no_decisive_marker")
    return GroundingClass(cls="UNCLEAR", reason="fallthrough")
