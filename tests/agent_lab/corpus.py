# -*- coding: utf-8 -*-
"""Lab corpus — paraphrase families, entity matrices and multi-turn journeys.

The customer's complaint is about MEANING surviving a change of wording,
order or context:

    "เวลาเปลี่ยนคำ เปลี่ยนบริบท AI ตอบไม่ได้ ทั้งที่ความหมายเดียวกัน"

So the corpus is organised as FAMILIES: one meaning, many surface forms —
polite and informal, reordered, shortened, typo'd, and embedded in a
running conversation. Every member of a family must reach the same
canonical understanding. The families are built from the vocabulary the
platform's existing customer suites already use (task §22: the six-source
master pass is NOT redone; its requirements are regression data).

TEST DATA ONLY. Nothing here is imported by runtime code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# ── 1. paraphrase families ───────────────────────────────────────────
# (family_id, expectation, [surface forms])
# expectation keys are asserted on the graph's AgentDecision.
PARAPHRASE_FAMILIES: List[Tuple[str, Dict[str, Any], List[str]]] = [
    ("supplier_dispatch", {"auth_required": True, "planned_action": "ASK_IDENTIFIER"}, [
        "ร้านส่งของหรือยัง",
        "ต้นทางส่งมาหรือยังครับ",
        "ของที่สั่งร้านส่งออกมาหรือยัง",
        "ร้านจีนส่งของออกมาหรือยังครับ",
        "ช่วยเช็กหน่อยว่าร้านจีนส่งของออกมารึยัง",
        "ร้านส่งของออกมารึยังคะ",
        "ทางร้านจัดส่งของออกมาหรือยังคะ",
    ]),
    ("cancellation_policy", {"primary_intent": "CANCELLATION_POLICY",
                             "auth_required": False,
                             "planned_action": "ANSWER_POLICY"}, [
        "ยกเลิกบิลสั่งซื้อได้ไหม",
        "ยกเลิกออเดอร์ได้ไหมคะ",
        "ยกเลิกคำสั่งซื้อได้หรือเปล่าครับ",
        "ยกเลิกบิลได้ปะ",
        "ขอยกเลิกออเดอร์ได้มั้ย",
        "เงื่อนไขยกเลิกเป็นยังไง",
        "ยกเลิกบิลได้หรอ",
    ]),
    ("cancellation_operation", {"primary_intent": "CANCELLATION_OPERATION"}, [
        "ช่วยยกเลิกบิล POS_TEST_001",
        "ช่วยยกเลิกบิล POS_TEST_001 ให้หน่อย",
        "รบกวนยกเลิกออเดอร์ POS_TEST_002 หน่อยครับ",
        "ขอให้ยกเลิกบิลสั่งซื้อ POS_TEST_003",
        "จัดการยกเลิกบิล POS_TEST_004 ให้ที",
        "ยกเลิกคำสั่งซื้อ POS_TEST_005 หน่อยค่ะ",
    ]),
    ("purchase_withdrawal", {"primary_intent": "PURCHASE_WITHDRAWAL"}, [
        "อยากถอนเงิน",
        "ถอนเครดิตยังไง",
        "ขอถอนยอดในระบบ",
        "ถอนเงินได้ไหม",
        "จะถอนยังไงคะ",
        "ถอนเงินออกมายังไงครับ",
    ]),
    ("contact_info", {"primary_intent": "CONTACT_INFO", "auth_required": False,
                      "selected_tool": "CONTACT_INFO"}, [
        "ขอเบอร์ติดต่อ",
        "ขอเบอร์ติดต่อหน่อยครับ",
        "ติดต่อช่องทางไหนคะ",
        "ขออีเมล และเว็บไซต์",
        "ขอเบอร์โทรหน่อยค่ะ",
        "ติดต่อยังไงคะ",
    ]),
    ("public_delivery_capability", {"auth_required": False}, [
        "ส่งถึงบ้านไหม",
        "จัดส่งสินค้าถึงหน้าบ้านเลยไหม",
        "ส่งถึงคอนโดได้ไหมคะ",
        "จัดส่งต่างจังหวัดได้ไหมครับ",
        "ส่งถึงที่ทำงานได้มั้ย",
        "มีส่งถึงบ้านหรือเปล่า",
    ]),
    ("private_own_shipment", {"auth_required": True,
                              "planned_action": "ASK_IDENTIFIER"}, [
        "ของฉันส่งถึงบ้านหรือยัง",
        "ของผมถึงไหนแล้วครับ",
        "พัสดุของฉันถึงไทยหรือยัง",
        "บิลผมส่งถึงหน้าบ้านหรือยัง",
        "ของที่ผมสั่งไปถึงไหนแล้ว",
        "ออเดอร์ของฉันถึงไหนแล้วคะ",
    ]),
]

# ── 2. entity / multi-intent matrix ──────────────────────────────────
PRODUCTS = ["รองเท้า", "ชั้นวางของ", "กล่องพลาสติก", "เครื่องซีลถุง", "ของเล่น",
            "กระเป๋าเดินทาง", "โคมไฟตั้งโต๊ะ", "ผ้าม่าน", "เก้าอี้สำนักงาน", "ตู้เก็บของ"]
UNITS = ["ตัว", "ชิ้น", "คู่", "ชุด", "กล่อง", "ขวด", "โหล", "ลัง", "เครื่อง", "พาเลท"]
QUANTITIES = [2, 3, 5, 10, 12, 20, 30, 50, 100, 500]
QUESTIONS = ["ราคาเท่าไหร่", "คิดค่าส่งยังไง", "ถึงไทยกี่วัน", "ส่งทางรถได้ไหม",
             "ใช้ขนส่งอะไร", "มีค่าอะไรบ้าง", "เท่าไร", "เมื่อไหร่ถึง",
             "ส่งถึงหน้าบ้านไหม", "คิดยังไง"]


def entity_cases() -> List[Dict[str, Any]]:
    """Single-turn cases carrying several facts at once, in every order,
    with and without spaces, with and without a trailing question."""
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(PRODUCTS):
        for j, u in enumerate(UNITS):
            n = QUANTITIES[(i + j) % len(QUANTITIES)]
            q = QUESTIONS[(i + j) % len(QUESTIONS)]
            forms = [
                (f"อยากสั่ง{p}จากจีน {n} {u} {q}", None),
                (f"{n} {u} อยากสั่ง{p}จากจีน {q}", None),
                (f"อยากนำเข้า{p} {n}{u} ส่งเรือ {q}", "sea"),
                (f"{q} อยากสั่ง{p} {n} {u}", None),
                (f"อยากได้{p}จากจีน{n}{u}{q}", None),
                (f"สั่ง{p} {n} {u} ส่งทางรถ {q} ครับ", "road"),
            ]
            for text, method in forms:
                out.append({"text": text, "product": p, "quantity": n,
                            "unit": u, "method": method, "question": q})
    return out


# ── 3. multi-turn journeys (context continuity) ──────────────────────
# Each step declares what must be TRUE after it. `keeps` names slots the
# bot must still know; `never_asks` names slots it must not ask for again.
def journeys() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in PRODUCTS:
        for u, n in zip(UNITS, QUANTITIES):
            # a) quantity first, then product, then method
            out.append({"id": f"qty_first::{p}::{u}", "steps": [
                {"say": f"{n} {u}อยากสั่งของจากจีน",
                 "keeps": {"quantity": n}, "never_asks": ["quantity"]},
                {"say": f"{p}ครับ",
                 "keeps": {"quantity": n, "product": p}, "never_asks": ["quantity"]},
                {"say": "ส่งเรือครับ",
                 "keeps": {"quantity": n, "product": p, "shipping_method": "sea"},
                 "never_asks": ["quantity", "product"]},
            ]})
            # b) product first, then quantity, then a correction
            out.append({"id": f"prod_first::{p}::{u}", "steps": [
                {"say": f"อยากสั่ง{p}จากจีน",
                 "keeps": {"product": p}, "never_asks": ["product"]},
                {"say": f"{n} {u}",
                 "keeps": {"product": p, "quantity": n}, "never_asks": ["product"]},
                {"say": f"เอา {n + 5} {u} ดีกว่า",
                 "keeps": {"product": p, "quantity": n + 5}, "never_asks": ["product"]},
            ]})
            # c) an explicit topic switch mid-journey, then back
            out.append({"id": f"topic_switch::{p}::{u}", "steps": [
                {"say": f"{n} {u}อยากสั่ง{p}จากจีน",
                 "keeps": {"quantity": n, "product": p}, "never_asks": []},
                {"say": "ขอเบอร์ติดต่อ", "expect_intent": "CONTACT_INFO",
                 "keeps": {}, "never_asks": ["quantity", "product"]},
            ]})
            # d) an explicit policy question must beat the running journey
            out.append({"id": f"policy_beats_journey::{p}::{u}", "steps": [
                {"say": f"{n} {u}อยากสั่งของจากจีน", "keeps": {"quantity": n},
                 "never_asks": []},
                {"say": "กระต่าย", "keeps": {"quantity": n}, "never_asks": ["quantity"]},
                {"say": "กระต่ายนำเข้าได้หรอ", "not_actions": ["ASK_QUANTITY",
                                                               "ASK_SHIPPING_METHOD"],
                 "keeps": {}, "never_asks": []},
            ]})
            # e) a mid-journey PRODUCT correction — the quantity the
            #    customer gave is about the order, not the old product, so
            #    it must survive the swap.
            _alt = PRODUCTS[(PRODUCTS.index(p) + 3) % len(PRODUCTS)]
            out.append({"id": f"product_correction::{p}::{u}", "steps": [
                {"say": f"{n} {u}อยากสั่ง{p}จากจีน",
                 "keeps": {"quantity": n, "product": p}, "never_asks": []},
                {"say": f"เปลี่ยนเป็น{_alt}",
                 "keeps": {"quantity": n, "product": _alt},
                 "never_asks": ["quantity"]},
                {"say": "ทางเรือค่ะ",
                 "keeps": {"quantity": n, "product": _alt, "shipping_method": "sea"},
                 "never_asks": ["quantity", "product"]},
            ]})
    return out


# ── 4. safety subset (executed through the real engine) ──────────────
SAFETY_CASES: List[Dict[str, Any]] = (
    [{"text": t, "public": True} for t in
     ["ส่งถึงบ้านไหม", "มีบริการอะไรบ้าง", "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง",
      "CBM คืออะไร", "มีขั้นต่ำในการสั่งไหม", "ค่าขนส่งคิดยังไง",
      "ยกเลิกบิลสั่งซื้อได้ไหม", "ตีลังไม้ได้ไหม", "ขอเบอร์ติดต่อ",
      "มีขนส่งทางเครื่องบินไหม", "ถอนเงินใช้เวลากี่วัน", "ออกใบกำกับได้ไหม"]]
    + [{"text": t, "private": True} for t in
       ["ของฉันส่งถึงบ้านหรือยัง", "ร้านส่งของหรือยัง", "ของผมถึงไหนแล้วครับ",
        "บิลของฉันยกเลิกหรือยัง", "ยอดเงินของฉันเหลือเท่าไหร่",
        "คูปองของฉันเหลืออะไรบ้าง", "ใบกำกับของฉันออกหรือยัง"]]
    + [{"text": t, "operational": True} for t in
       ["ช่วยยกเลิกบิล POS_TEST_001 ให้หน่อย", "บิลขนส่ง FT ต้องการเปลี่ยนที่อยู่จัดส่ง",
        "ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ", "รวมบิลเหมารถค่ะ", "รีเเพ็คค่ะ"]]
    + [{"text": t} for t in
       ["อยากถอนเงิน", "20 คู่อยากสั่งของจากจีน",
        "อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ ราคาเท่าไหร่",
        "ครับ", "สรุปยังไงครับ", "ส่งไปดาวอังคารได้ไหม", "ของมาบุบมากเลย"]]
)


def total_turns() -> int:
    n = sum(len(forms) for _fid, _exp, forms in PARAPHRASE_FAMILIES)
    n += len(entity_cases())
    n += sum(len(j["steps"]) for j in journeys())
    n += len(SAFETY_CASES)
    return n
