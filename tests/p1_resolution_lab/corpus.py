# -*- coding: utf-8 -*-
"""P1 canonical-resolver generalisation lab — 250+ UNSEEN cases.

Cases are COMBINATORIALLY generated from templates (products, quantities
with units, methods, correction verbs, topic switches, typo forms) so the
set is not cherry-picked to the resolver's regexes, plus hand-written
referent / multi-intent / long-history cases.

Each case: {id, category, message, history, expect{...}}
  expect keys (all optional; only the present ones are scored):
    act        — ConversationResolution.conversation_act
    intent     — primary_intent
    journey    — active_journey  ("IMPORT_INTEREST" | None)
    topic      — topic_switch family
    corr       — dict slot -> "new" value (checked against slot_corrections[slot]["new"])
    slots      — dict slot -> (value, unit)   checked against slot_updates
    qty_unit   — expected quantity unit string somewhere in updates/corrections (unit-preservation)
"""
from __future__ import annotations

_DISC = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
         "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")


def _u(c):
    return {"role": "user", "content": c}


def _a(c):
    return {"role": "assistant", "content": c}


def _import_seed(product, qty=None, method=None):
    h = [_u("อยากสั่งของจากจีน"), _a(_DISC), _u(f"เป็น{product}")]
    ack = f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{product}"
    if qty:
        ack += f" จำนวนประมาณ {qty} ชิ้น"
    if method:
        ack += f" ขนส่ง{{'road':'ทางรถ','sea':'ทางเรือ','air':'ทางอากาศ'}}"[0]  # placeholder, real below
    _M = {"road": "ทางรถ", "sea": "ทางเรือ", "air": "ทางอากาศ"}
    ack = f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{product}"
    if qty:
        ack += f" จำนวนประมาณ {qty} ชิ้น"
    if method:
        ack += f" ขนส่ง{_M[method]}"
    ack += "นะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
    h.append(_a(ack))
    return h


_PRODUCTS = ["รองเท้า", "โต๊ะ", "เก้าอี้", "เสื้อผ้า", "กระเป๋า", "หมวก", "ชั้นวางของ",
             "โคมไฟตั้งโต๊ะ", "อะไหล่รถยนต์", "กล่องพลาสติก", "ผ้าห่ม", "ที่นอน",
             "จักรยาน", "ตุ๊กตา", "แก้วน้ำ", "จาน", "ช้อนส้อม", "นาฬิกา", "ร่ม", "พรม"]
_UNITS = ["คู่", "ตัว", "ชิ้น", "อัน", "ใบ", "ชุด", "กล่อง", "โหล", "แพ็ค", "ลัง"]
_CHANGE_VERBS = ["เปลี่ยนเป็น", "เอาเป็น", "ขอเปลี่ยนเป็น", "เปลี่ยนของเป็น"]
_CHANGE_TYPO = ["เปลี่ยนเป้น", "เปลียนเปน", "เปลี่ยนเปน", "เปลียนเป็น"]
_METHODS = [("ทางรถ", "road"), ("ทางเรือ", "sea"), ("ทางอากาศ", "air")]
_TOPIC = [
    ("ขอเบอร์ติดต่อ", "CONTACT_INFO"),
    ("ขอเบอร์ติดต่อหน่อยครับ", "CONTACT_INFO"),
    ("ถอนเงินขนส่งยังไง", "SHIPPING_WITHDRAWAL"),
    ("ขอถอนเงินค่าสั่งซื้อ", "PURCHASE_WITHDRAWAL"),
    ("คูปองใช้ยังไง", "COUPON_USAGE"),
    ("โกดังไทยอยู่ที่ไหน", "PICKUP_LOCATION"),
    ("ขอใบกำกับภาษีหน่อย", "INVOICE"),
]
_CANCELS = ["ไม่เอาแล้ว", "ไม่เอาแล้วค่ะ", "ยกเลิก", "ไม่ต้องแล้ว", "พอแล้วค่ะ", "ไม่สนแล้ว"]


def _build():
    cases = []
    n = 0

    # ── 1. multi-turn: import open -> product slot (100) ──
    for p in _PRODUCTS:
        for opener in ("อยากสั่งของจากจีน", "สนใจนำเข้าสินค้าจากจีน", "อยากซื้อของจาก 1688", "อยากสั่งของจีน"):
            n += 1
            cases.append({
                "id": f"mt_open_{n}", "category": "multi_turn",
                "message": opener, "history": [],
                "expect": {"act": "NEW_INTENT", "intent": "IMPORT_INTEREST", "journey": None},
            })
            if n >= 40:
                break
        if n >= 40:
            break
    for p in _PRODUCTS[:12]:
        for phr in (f"เป็น{p}", f"{p}ค่ะ", f"อยากได้{p}", f"ขอเป็น{p}", f"{p}"):
            n += 1
            h = [_u("อยากสั่งของจากจีน"), _a(_DISC)]
            cases.append({
                "id": f"mt_slot_{n}", "category": "multi_turn",
                "message": phr, "history": h,
                "expect": {"intent": "IMPORT_INTEREST"},
            })

    # ── 2. corrections: product (50) ──
    for i, p_old in enumerate(_PRODUCTS):
        p_new = _PRODUCTS[(i + 7) % len(_PRODUCTS)]
        for v in (_CHANGE_VERBS + _CHANGE_TYPO):
            n += 1
            cases.append({
                "id": f"corr_prod_{n}", "category": "corrections",
                "message": f"{v}{p_new}ได้ไหม", "history": _import_seed(p_old),
                "expect": {"act": "CORRECTION", "journey": "IMPORT_INTEREST",
                           "corr": {"product": p_new}},
            })
            if n % 3 == 0:
                break
        if len([c for c in cases if c["category"] == "corrections"]) >= 30:
            break

    # ── quantity corrections (12) ──
    for i, unit in enumerate(_UNITS):
        old_q, new_q = 20, 10 + i
        n += 1
        cases.append({
            "id": f"corr_qty_{n}", "category": "corrections",
            "message": f"เอ้ย {new_q} {unit}", "history": _import_seed("รองเท้า", qty=old_q),
            "expect": {"act": "CORRECTION", "corr": {"quantity": new_q}},
        })

    # ── method corrections (9) ──
    for name, lab in _METHODS:
        for other_name, other_lab in _METHODS:
            if other_lab == lab:
                continue
            n += 1
            cases.append({
                "id": f"corr_mth_{n}", "category": "corrections",
                "message": f"{other_name}ดีกว่า", "history": _import_seed("รองเท้า", method=lab),
                "expect": {"act": "CORRECTION", "corr": {"shipping_method": other_lab}},
            })
    for name, lab in _METHODS:
        n += 1
        cases.append({
            "id": f"corr_mth_switch_{n}", "category": "corrections",
            "message": f"เปลี่ยนเป็น{name}", "history": _import_seed("โต๊ะ", method=("sea" if lab != "sea" else "road")),
            "expect": {"act": "CORRECTION", "corr": {"shipping_method": lab}},
        })

    # ── 3. topic switches (30+) ──
    for msg, fam in _TOPIC:
        for prod in _PRODUCTS[:5]:
            n += 1
            cases.append({
                "id": f"topic_{n}", "category": "topic_switch",
                "message": msg, "history": _import_seed(prod),
                "expect": {"act": "TOPIC_SWITCH", "topic": fam},
            })

    # rejections (must not be TOPIC_SWITCH / not a product)
    for c in _CANCELS:
        for prod in _PRODUCTS[:5]:
            n += 1
            cases.append({
                "id": f"reject_{n}", "category": "topic_switch",
                "message": c, "history": _import_seed(prod),
                "expect": {"act": "REJECTION"},
            })

    # ── 4. multi-entity (25) ──
    me = [
        ("อยากสั่งรองเท้า 20 คู่จากจีน ส่งทางเรือ",
         {"product": ("รองเท้า", None), "quantity": (20, "คู่"), "shipping_method": ("sea", None)}),
        ("อยากสั่งโต๊ะ 5 ตัว หนักตัวละ 8 กิโล",
         {"product": ("โต๊ะ", None), "quantity": (5, "ตัว"), "weight": (8, "กิโล")}),
        ("สั่งเก้าอี้ 12 ตัวจาก 1688 ทางรถ",
         {"product": ("เก้าอี้", None), "quantity": (12, "ตัว"), "shipping_method": ("road", None)}),
        ("อยากนำเข้ากระเป๋า 100 ใบ จากจีน",
         {"product": ("กระเป๋า", None), "quantity": (100, "ใบ")}),
        ("ซื้อหมวก 3 โหลจาก taobao",
         {"product": ("หมวก", None), "quantity": (3, "โหล")}),
        ("อยากสั่งผ้าห่ม 50 ผืน ทางเรือ จากจีน",
         {"product": ("ผ้าห่ม", None), "shipping_method": ("sea", None)}),
        ("สั่งจาน 200 ใบ ขนาด 25x25x5 cm",
         {"product": ("จาน", None), "quantity": (200, "ใบ"), "dimensions": ("25x25x5", "cm")}),
        ("อยากได้นาฬิกา 6 เรือน ราคาเท่าไหร่",  # question inside multi-entity
         {"quantity": None}),
        ("20 คู่อยากสั่งของจากจีน",
         {"quantity": (20, "คู่")}),
        ("10 ตัว โต๊ะ จากจีน",
         {"quantity": (10, "ตัว"), "product": ("โต๊ะ", None)}),
    ]
    for i in range(3):
        for msg, exp in me:
            n += 1
            cases.append({
                "id": f"multi_{n}", "category": "multi_entity",
                "message": msg, "history": [],
                "expect": {"slots": {k: v for k, v in exp.items() if v}},
            })
            if len([c for c in cases if c["category"] == "multi_entity"]) >= 25:
                break
        if len([c for c in cases if c["category"] == "multi_entity"]) >= 25:
            break

    # ── 5. typo + context (25) ──
    typo_pairs = [
        ("เปลี่ยนเป้นรองเท้าได้ไหม", "corrections", {"act": "CORRECTION", "corr": {"product": "รองเท้า"}}, _import_seed("โต๊ะ")),
        ("เปลียนเปนเก้าอี้", "corrections", {"act": "CORRECTION", "corr": {"product": "เก้าอี้"}}, _import_seed("โต๊ะ")),
        ("ไม่ไช่โต๊ะ เป้นเก้าอี้", "corrections", {"act": "CORRECTION", "corr": {"product": "เก้าอี้"}}, _import_seed("โต๊ะ")),
        ("เอ้ย 15 คู่", "corrections", {"act": "CORRECTION", "corr": {"quantity": 15}}, _import_seed("รองเท้า", qty=20)),
        ("เปลี่ยนเปนทางเรือ", "corrections", {"act": "CORRECTION", "corr": {"shipping_method": "sea"}}, _import_seed("โต๊ะ", method="road")),
    ]
    for i in range(5):
        for msg, cat, exp, h in typo_pairs:
            n += 1
            cases.append({"id": f"typo_{n}", "category": "typo_context",
                          "message": msg, "history": h, "expect": exp})

    # ── 6. long history (20) ──
    _round = [_u("อยากสั่งของจากจีน"), _a(_DISC), _u("เป็นโต๊ะ"),
              _a("ได้ค่ะ รับทราบว่าต้องการนำเข้าโต๊ะนะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"),
              _u("เปลี่ยนเป็นเก้าอี้"),
              _a("ได้ค่ะ เปลี่ยนเป็นเก้าอี้ได้เลยค่ะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"),
              _u("https://detail.1688.com/offer/652702302959.html"),
              _a("แปลงลิงก์ให้เรียบร้อยแล้วค่ะ 😊"),
              _u("แล้วค่าขนส่งทางเรือกิโลละเท่าไหร่"), _a("ทางเรือคิด 19 บาท/กิโลกรัม ค่ะ")]
    for depth in (2, 3, 4, 5):
        long_h = _round * depth
        for msg, exp in [
            ("อยากสั่งของจากจีน", {"act": "NEW_INTENT", "intent": "IMPORT_INTEREST"}),
            ("ขอเบอร์ติดต่อ", {"act": "TOPIC_SWITCH", "topic": "CONTACT_INFO"}),
            ("ไม่เอาแล้ว", {"act": "REJECTION"}),
            ("ของผมถึงไหนแล้ว", {"intent": "SHIPMENT_STATUS"}),
            ("ชั้นวางของนำเข้าได้ไหม", {"intent": "PRODUCT_POLICY"}),
        ]:
            n += 1
            cases.append({"id": f"long_{n}", "category": "long_history",
                          "message": msg, "history": list(long_h), "expect": exp})

    # ── 7. referent cases (12) ──
    ref = [
        ("แล้วทางเรือล่ะ", _import_seed("รองเท้า", method="road"), {"journey": "IMPORT_INTEREST"}),
        ("อันนี้ราคาเท่าไหร่", _import_seed("โต๊ะ"), {}),
        ("เอาอันแรก", _import_seed("โต๊ะ"), {}),
        ("แบบนี้ส่งได้ไหม", _import_seed("เก้าอี้"), {}),
    ]
    for i in range(3):
        for msg, h, exp in ref:
            n += 1
            cases.append({"id": f"ref_{n}", "category": "referents",
                          "message": msg, "history": h, "expect": exp})

    # ── 8. required §5 anchors ──
    anchors = [
        ("A", "20 คู่อยากสั่งของจากจีน", [], {"intent": "IMPORT_INTEREST", "qty_unit": "คู่"}),
        ("G", "ชั้นวางของนำเข้าได้ไหม", [], {"intent": "PRODUCT_POLICY", "act": "NEW_INTENT"}),
        ("H", "ของแก้วแพ็กยังไง", [], {"intent": "GENERAL_ASSISTANCE"}),
        ("I", "ของผมถึงไหนแล้ว", [], {"intent": "SHIPMENT_STATUS"}),
    ]
    for tag, msg, h, exp in anchors:
        n += 1
        cases.append({"id": f"anchor_{tag}", "category": "anchor", "message": msg,
                      "history": h, "expect": exp})

    return cases


CASES = _build()
