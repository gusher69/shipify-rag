# -*- coding: utf-8 -*-
"""P2 structured-conversation-frame lab corpus (>= 200 cases).

Each case threads a sequence of turns through
resolve_conversation -> conversation_frame_store.build_frame and asserts
the FINAL structured frame (journey / status / slots with units) plus a
parity classification against the legacy text-derived frame.

Turn shape:  {"u": "<user text>", "a": "<assistant reply text>"}
The assistant reply feeds the NEXT turn's legacy derive_active_frame and
requested-slot inference, exactly like production history.
"""

_DISCOVERY = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
             "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")


def _ack_product(p, qty=None, meth=None):
    q = f" จำนวนประมาณ {qty} ชิ้น" if qty else ""
    m = f" ขนส่ง{'ทางเรือ' if meth=='sea' else 'ทางรถ' if meth=='road' else 'ทางอากาศ'}" if meth else ""
    ask = " รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ" if not qty else (" สนใจส่งทางรถหรือทางเรือคะ" if not meth else "")
    return f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{p}{q}{m}นะคะ 😊{ask}"


def _ack_qty(p, qty, meth=None):
    m = " ขนส่งทางเรือ ตามเดิมค่ะ" if meth == "sea" else (" ขนส่งทางรถ ตามเดิมค่ะ" if meth == "road" else "")
    ask = " สนใจส่งทางรถหรือทางเรือคะ" if not meth else ""
    return f"รับทราบค่ะ ปรับเป็นจำนวนประมาณ {qty} ชิ้น สำหรับ{p}นะคะ{m}{ask}"


def _ack_meth(p, meth, qty=None):
    mm = "ทางเรือ" if meth == "sea" else "ทางรถ" if meth == "road" else "ทางอากาศ"
    q = f" จำนวนประมาณ {qty} ชิ้น ตามเดิมค่ะ" if qty else ""
    return f"รับทราบค่ะ เปลี่ยนเป็นขนส่ง{mm} สำหรับ{p}นะคะ{q}"


def _ack_prod_corr(p, qty=None, meth=None):
    extra = ""
    if qty or meth:
        mm = " ขนส่งทางเรือ" if meth == "sea" else " ขนส่งทางรถ" if meth == "road" else ""
        qq = f" จำนวนประมาณ {qty} ชิ้น" if qty else ""
        extra = f"{qq}{mm} ตามเดิมนะคะ"
    ask = " รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ" if not qty else (" สนใจส่งทางรถหรือทางเรือคะ" if not meth else "")
    return f"ได้ค่ะ เปลี่ยนเป็น{p}ได้เลยค่ะ 😊{extra}{ask}"


_CANCEL = "ได้ค่ะ ยกเลิกรายการนี้ให้นะคะ 😊 ถ้าต้องการให้ช่วยเรื่องอื่น แจ้งได้เลยค่ะ"
_CONTACT = "ติดต่อได้ทาง LINE: @Shipify, โทร 02-026-6426 ค่ะ"
_RATE = "ทางเรือคิด 19 บาท/กิโลกรัม ค่ะ"

CASES = []


def _c(cid, category, turns, expect):
    CASES.append({"id": cid, "category": category, "turns": turns, "expect": expect})


# ── A. required real-world replays (task §10) ───────────────────────
_c("A", "multi_turn", [
    {"u": "20 คู่อยากสั่งของจากจีน", "a": _DISCOVERY},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"quantity": (20, "คู่")}, "missing": ["product"]})

_c("B", "multi_turn", [
    {"u": "20 คู่อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นชั้นวางของ", "a": _ack_product("ชั้นวางของ", 20)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"product": ("ชั้นวางของ", None), "quantity": (20, "คู่")}})

_c("C", "corrections", [
    {"u": "20 คู่อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นชั้นวางของ", "a": _ack_product("ชั้นวางของ", 20)},
    {"u": "เปลี่ยนเป็นรองเท้า", "a": _ack_prod_corr("รองเท้า", 20)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"product": ("รองเท้า", None), "quantity": (20, "คู่")}})

_c("D", "corrections", [
    {"u": "20 คู่อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นชั้นวางของ", "a": _ack_product("ชั้นวางของ", 20)},
    {"u": "เปลี่ยนเป็นรองเท้า", "a": _ack_prod_corr("รองเท้า", 20)},
    {"u": "เอ้ย 10 คู่", "a": _ack_qty("รองเท้า", 10)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"product": ("รองเท้า", None), "quantity": (10, "คู่")}})

_c("E", "corrections", [
    {"u": "อยากสั่งรองเท้าจากจีน", "a": _ack_product("รองเท้า")},
    {"u": "10 คู่", "a": _ack_qty("รองเท้า", 10)},
    {"u": "ทางรถ", "a": _ack_meth("รองเท้า", "road", 10)},
    {"u": "ทางเรือดีกว่า", "a": _ack_meth("รองเท้า", "sea", 10)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"product": ("รองเท้า", None), "quantity": (10, "คู่"), "shipping_method": ("sea", None)}})

_c("F", "lifecycle", [
    {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นรองเท้า", "a": _ack_product("รองเท้า")},
    {"u": "ไม่เอาแล้ว", "a": _CANCEL},
], {"journey": "IMPORT_INTEREST", "status": "CANCELLED"})

_c("G", "topic_switch", [
    {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นรองเท้า", "a": _ack_product("รองเท้า")},
    {"u": "ขอเบอร์ติดต่อ", "a": _CONTACT},
], {"journey": "IMPORT_INTEREST", "status": "SUSPENDED"})

# ── B. programmatic multi-turn / corrections / topic / lifecycle ────
_PRODUCTS = ["โต๊ะ", "เก้าอี้", "เสื้อผ้า", "กระเป๋า", "โคมไฟ", "ชั้นวางของ",
             "เครื่องจักร", "อะไหล่รถยนต์", "ของเล่น", "เครื่องครัว", "พรม", "หมอน"]
_UNITS = [("คู่", 20), ("ตัว", 5), ("ชิ้น", 100), ("กล่อง", 3), ("ชุด", 6),
          ("โหล", 2), ("อัน", 40), ("ใบ", 12), ("เครื่อง", 2), ("แพ็ค", 8)]

for i, p in enumerate(_PRODUCTS):
    u, n = _UNITS[i % len(_UNITS)]
    # open -> product -> quantity+unit
    _c(f"MT{i:02d}", "multi_turn", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": f"เป็น{p}", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (n, u)}})

for i, p in enumerate(_PRODUCTS):
    p2 = _PRODUCTS[(i + 3) % len(_PRODUCTS)]
    u, n = _UNITS[(i + 2) % len(_UNITS)]
    # open -> product -> qty -> product correction (qty kept)
    _c(f"PC{i:02d}", "corrections", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": f"เป็น{p}", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
        {"u": f"เปลี่ยนเป็น{p2}", "a": _ack_prod_corr(p2, n)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p2, None), "quantity": (n, u)}})

for i, p in enumerate(_PRODUCTS):
    u, n = _UNITS[(i + 1) % len(_UNITS)]
    n2 = n + 7
    # qty correction keeps the ORIGINAL unit
    _c(f"QC{i:02d}", "corrections", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
        {"u": f"เอ้ย {n2} {u}", "a": _ack_qty(p, n2)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (n2, u)}})

for i, p in enumerate(_PRODUCTS):
    # method correction road -> sea, product+qty retained
    _c(f"SM{i:02d}", "corrections", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": "5 ชิ้น", "a": _ack_qty(p, 5)},
        {"u": "ทางรถ", "a": _ack_meth(p, "road", 5)},
        {"u": "เอาทางเรือแทน", "a": _ack_meth(p, "sea", 5)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (5, "ชิ้น"), "shipping_method": ("sea", None)}})

for i, p in enumerate(_PRODUCTS):
    # topic switch mid-journey -> SUSPENDED, slots untouched
    _c(f"TS{i:02d}", "topic_switch", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": f"เป็น{p}", "a": _ack_product(p)},
        {"u": "10 ชิ้น", "a": _ack_qty(p, 10)},
        {"u": ["ขอเบอร์ติดต่อ", "ถอนเงินขนส่งยังไง", "ที่อยู่โกดังอยู่ไหน"][i % 3],
         "a": _CONTACT},
    ], {"journey": "IMPORT_INTEREST", "status": "SUSPENDED",
        "slots": {"product": (p, None), "quantity": (10, "ชิ้น")}})

for i, p in enumerate(_PRODUCTS):
    # rejection -> CANCELLED
    _c(f"RJ{i:02d}", "lifecycle", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": "5 ชิ้น", "a": _ack_qty(p, 5)},
        {"u": ["ไม่เอาแล้ว", "ยกเลิก", "พอแล้ว", "ไม่ต้องแล้ว"][i % 4], "a": _CANCEL},
    ], {"journey": "IMPORT_INTEREST", "status": "CANCELLED"})

# ── C. unit preservation (never normalise to ชิ้น) ─────────────────
for i, (u, n) in enumerate(_UNITS):
    _c(f"UN{i:02d}", "unit_preservation", [
        {"u": f"อยากสั่งรองเท้าจากจีน", "a": _ack_product("รองเท้า")},
        {"u": f"{n} {u}", "a": _ack_qty("รองเท้า", n)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"quantity": (n, u)}, "unit": (u,)})

# weight / dimensions canonicalisation, raw kept
_c("UN-W1", "unit_preservation", [
    {"u": "อยากสั่งโต๊ะ 3 ตัว หนักตัวละ 5000 กรัม จากจีน", "a": _ack_product("โต๊ะ", 3)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"product": ("โต๊ะ", None), "quantity": (3, "ตัว")}, "weight_canonical_kg": 5.0})
# canonical conversion (raw mm -> canonical cm) is verified directly in
# tests/test_p2_conversation_frame.py::test_dimension_unit_canonical —
# a dims-in-import-sentence may legitimately route to the calculator.
_c("UN-D1", "unit_preservation", [
    {"u": "อยากสั่งกล่องรองเท้าจากจีน", "a": _ack_product("กล่องรองเท้า")},
    {"u": "5 คู่", "a": _ack_qty("กล่องรองเท้า", 5)},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
    "slots": {"quantity": (5, "คู่")}, "unit": ("คู่",)})

# ── D. long history — old completed / link / withdrawal journeys then
#      a fresh explicit intent must start clean ────────────────────────
_STALE_ROUND = [
    {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
    {"u": "เป็นโต๊ะ", "a": _ack_product("โต๊ะ")},
    {"u": "เปลี่ยนเป็นเก้าอี้", "a": _ack_prod_corr("เก้าอี้")},
    {"u": "https://detail.1688.com/offer/652702302959.html", "a": "แปลงลิงก์ให้เรียบร้อยแล้วค่ะ 😊"},
    {"u": "แล้วค่าขนส่งทางเรือกิโลละเท่าไหร่", "a": _RATE},
]
for mult, label in ((0, "empty"), (2, "10turn"), (6, "30turn"), (10, "50turn"), (20, "100turn")):
    _c(f"LH-{label}", "long_history", (_STALE_ROUND * mult) + [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": "เป็นรองเท้า", "a": _ack_product("รองเท้า")},
        {"u": "20 คู่", "a": _ack_qty("รองเท้า", 20)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": ("รองเท้า", None), "quantity": (20, "คู่")},
        "no_stale_takeover": True})

for mult, label in ((6, "30turn"), (14, "70turn")):
    _c(f"LHC-{label}", "long_history", (_STALE_ROUND * mult) + [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": "เป็นกระเป๋า", "a": _ack_product("กระเป๋า")},
        {"u": "เปลี่ยนเป็นหมวก", "a": _ack_prod_corr("หมวก")},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": ("หมวก", None)}})

# ── E. lifecycle transitions ───────────────────────────────────────
_c("LC-suspend-resume", "lifecycle", [
    {"u": "อยากสั่งรองเท้าจากจีน", "a": _ack_product("รองเท้า")},
    {"u": "10 คู่", "a": _ack_qty("รองเท้า", 10)},
    {"u": "ขอเบอร์ติดต่อ", "a": _CONTACT},
    {"u": "กลับมาเรื่องรองเท้า อยากได้ 20 คู่", "a": _ack_qty("รองเท้า", 20)},
], {"journey": "IMPORT_INTEREST"})  # status not pinned — resume is P3

_c("LC-cancel-then-new", "lifecycle", [
    {"u": "อยากสั่งโต๊ะจากจีน", "a": _ack_product("โต๊ะ")},
    {"u": "ไม่เอาแล้ว", "a": _CANCEL},
    {"u": "อยากสั่งเก้าอี้จากจีน", "a": _ack_product("เก้าอี้")},
], {"journey": "IMPORT_INTEREST", "status": "ACTIVE", "slots": {"product": ("เก้าอี้", None)}})

_c("LC-topic-then-back", "lifecycle", [
    {"u": "อยากสั่งเสื้อผ้าจากจีน", "a": _ack_product("เสื้อผ้า")},
    {"u": "5 ชิ้น", "a": _ack_qty("เสื้อผ้า", 5)},
    {"u": "ถอนเงินขนส่งยังไง", "a": "ถอนได้ที่เมนู..."},
], {"journey": "IMPORT_INTEREST", "status": "SUSPENDED",
    "slots": {"product": ("เสื้อผ้า", None), "quantity": (5, "ชิ้น")}})

# ── F. non-journey turns must NOT create an import frame ────────────
for i, m in enumerate(["ชั้นวางของนำเข้าได้ไหม", "ของผมถึงไหนแล้ว", "ขอใบกำกับภาษี",
                        "คูปองใช้ยังไง", "โกดังอยู่ที่ไหน", "ของแก้วแพ็กยังไง"]):
    _c(f"NJ{i:02d}", "topic_switch", [{"u": m, "a": "..."}],
       {"journey": None})

# ── G. corpus expansion to >= 200 cases ───────────────────────────
_EXTRA_PRODUCTS = ["ตุ๊กตา", "นาฬิกา", "รองเท้าผ้าใบ", "กระเป๋าเดินทาง", "จักรยาน",
                   "เตาอบ", "พัดลม", "ผ้าม่าน", "ที่นอน", "ตู้เสื้อผ้า", "เก้าอี้สำนักงาน",
                   "โคมไฟตั้งโต๊ะ", "กล่องเก็บของ", "ชั้นหนังสือ", "โซฟา"]

# more multi-turn open->product->qty (all count units)
for i, p in enumerate(_EXTRA_PRODUCTS):
    u, n = _UNITS[i % len(_UNITS)]
    _c(f"MTX{i:02d}", "multi_turn", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": f"เป็น{p}", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (n, u)}, "unit": (u,)})

# more product corrections
for i, p in enumerate(_EXTRA_PRODUCTS):
    p2 = _EXTRA_PRODUCTS[(i + 5) % len(_EXTRA_PRODUCTS)]
    u, n = _UNITS[(i + 4) % len(_UNITS)]
    _c(f"PCX{i:02d}", "corrections", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
        {"u": f"เอาเป็น{p2}แทน", "a": _ack_prod_corr(p2, n)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p2, None), "quantity": (n, u)}, "unit": (u,)})

# more quantity corrections (unit kept across two corrections)
for i, p in enumerate(_EXTRA_PRODUCTS):
    u, n = _UNITS[(i + 3) % len(_UNITS)]
    _c(f"QCX{i:02d}", "corrections", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": f"{n} {u}", "a": _ack_qty(p, n)},
        {"u": f"เอ้ย {n+1} {u}", "a": _ack_qty(p, n + 1)},
        {"u": f"ไม่ใช่ {n+1} เป็น {n+5}", "a": _ack_qty(p, n + 5)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (n + 5, u)}, "unit": (u,)})

# more topic switches (varied switch targets)
_SWITCHERS = ["ขอเบอร์ติดต่อหน่อย", "ถอนเงินขนส่งยังไง", "โกดังไทยอยู่ไหน",
              "ขอลิงก์เว็บ Taobao", "คูปองใช้ยังไง", "ของผมถึงไหนแล้ว"]
for i, p in enumerate(_EXTRA_PRODUCTS):
    _c(f"TSX{i:02d}", "topic_switch", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": "8 ชิ้น", "a": _ack_qty(p, 8)},
        {"u": _SWITCHERS[i % len(_SWITCHERS)], "a": _CONTACT},
    ], {"journey": "IMPORT_INTEREST", "status": "SUSPENDED",
        "slots": {"product": (p, None), "quantity": (8, "ชิ้น")}})

# more lifecycle (cancel / new / cancel)
for i, p in enumerate(_EXTRA_PRODUCTS[:10]):
    p2 = _EXTRA_PRODUCTS[(i + 7) % len(_EXTRA_PRODUCTS)]
    _c(f"LCX{i:02d}", "lifecycle", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": "ไม่เอาแล้ว", "a": _CANCEL},
        {"u": f"อยากสั่ง{p2}จากจีน", "a": _ack_product(p2)},
        {"u": "5 คู่", "a": _ack_qty(p2, 5)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p2, None), "quantity": (5, "คู่")}, "unit": ("คู่",)})

# more long-history depths with a correction as the final turn
for mult, label in ((4, "20turn"), (8, "40turn"), (12, "60turn"), (16, "80turn")):
    _c(f"LHX-{label}", "long_history", (_STALE_ROUND * mult) + [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": "เป็นจักรยาน", "a": _ack_product("จักรยาน")},
        {"u": "3 คัน", "a": _ack_qty("จักรยาน", 3)},
        {"u": "เปลี่ยนเป็นสกู๊ตเตอร์", "a": _ack_prod_corr("สกู๊ตเตอร์", 3)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": ("สกู๊ตเตอร์", None)}, "no_stale_takeover": False})

# unit preservation across the full spread of count units
for i, (u, n) in enumerate(_UNITS):
    _c(f"UNX{i:02d}", "unit_preservation", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": "เป็นตุ๊กตา", "a": _ack_product("ตุ๊กตา")},
        {"u": f"{n} {u}", "a": _ack_qty("ตุ๊กตา", n)},
        {"u": f"เอ้ย {n*2} {u}", "a": _ack_qty("ตุ๊กตา", n * 2)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"quantity": (n * 2, u)}, "unit": (u,)})

# typo + context corrections (PHASE-6D: no fuzzy-fix at intent time, so
# the correction regexes must be typo-tolerant on their own)
_TYPO_CORR = [
    ("เปลี่ยนเป้นรองเท้า", "รองเท้า"),
    ("เปลียนเปนกระเป๋า", "กระเป๋า"),
    ("เอาเปนหมวกแทน", "หมวก"),
    ("ไม่ไช่โต๊ะ เปนเก้าอี้", "เก้าอี้"),
    ("เปลี่ยน เป็น ผ้าห่ม", "ผ้าห่ม"),
    ("เอ้ย เปนเสื้อ", "เสื้อ"),
]
for i, (msg, newp) in enumerate(_TYPO_CORR):
    _c(f"TY{i:02d}", "corrections", [
        {"u": "อยากสั่งของจากจีน", "a": _DISCOVERY},
        {"u": "เป็นชั้นวางของ", "a": _ack_product("ชั้นวางของ")},
        {"u": msg, "a": _ack_prod_corr(newp)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (newp, None)}})

# short-follow-up quantity answers (bare number, no unit -> unit None but
# value kept; then a corrective with unit sets the unit)
for i, p in enumerate(_EXTRA_PRODUCTS[:8]):
    _c(f"BQ{i:02d}", "multi_turn", [
        {"u": f"อยากสั่ง{p}จากจีน", "a": _ack_product(p)},
        {"u": "50", "a": _ack_qty(p, 50)},
        {"u": "เอ้ย 30 กล่อง", "a": _ack_qty(p, 30)},
    ], {"journey": "IMPORT_INTEREST", "status": "ACTIVE",
        "slots": {"product": (p, None), "quantity": (30, "กล่อง")}, "unit": ("กล่อง",)})
