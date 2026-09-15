# -*- coding: utf-8 -*-
"""Thai noisy-text corpus (task §8/§7/§10) — generated, not hand-patched.

Three corpora, all sanitized (no real customer identifiers; the example
codes below are the documented placeholder formats):

  typo_cases()        >= 300 noisy variants of a ground-truthed base set.
                      Every variant is produced by a NOISE OPERATOR that
                      models one real human mistake (a dropped or
                      swapped tone mark, a duplicated vowel, a doubled
                      final consonant, an informal particle, spacing,
                      elongation, zero-width debris, the keyboard layout
                      left in English) plus the owner's own hand-listed
                      mistakes. The truth is the CLEAN sentence's
                      ground-truth reading, never the noisy reading of
                      another case.
  paraphrase_groups() >= 300 wordings across >= 40 meaning groups; each
                      group has ONE canonical family (and key entities).
  journeys()          >= 100 multi-turn conversations built from turn
                      pools, with noise applied to a share of the turns,
                      and per-turn expectations for slot continuity,
                      precedence and authority.

Ground truth is a semantic reading — family, product, quantity + unit,
shipping method, authority requirement — not a reply string.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from services.language import vocabulary as V

# ── ground truth ─────────────────────────────────────────────────────
Family = Union[str, Set[str]]


@dataclass
class Truth:
    family: Family                       # one family, or an accepted set
    product: Optional[str] = None        # None = must be absent; "*" = any
    quantity: Optional[int] = None
    unit: Optional[str] = None
    method: Optional[str] = None
    auth: Optional[bool] = None          # None = not asserted
    check_product: bool = True
    check_quantity: bool = True
    check_method: bool = True
    history: Optional[str] = None        # key into HISTORIES
    note: str = ""


# the ASSISTANT turns are the platform's own committed wordings — reply
# text IS the conversation state on this platform, so a synthetic
# history must use the real phrasing.
_ASK_PRODUCT = "ได้ค่ะ รับทราบ จำนวนประมาณ 20 คู่นะคะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"
_ASK_PRODUCT_NOQTY = "ยินดีให้บริการนำเข้าสินค้าจากจีนค่ะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"
_ASK_METHOD = "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 คู่นะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"
_ASK_QTY = "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้านะคะ 😊 ต้องการจำนวนประมาณเท่าไหร่คะ"
_ASK_WEIGHT = "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 คู่ ขนส่งทางเรือนะคะ 😊 รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"

HISTORIES: Dict[str, List[Dict[str, str]]] = {
    "ASK_PRODUCT": [
        {"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
        {"role": "assistant", "content": _ASK_PRODUCT},
    ],
    "ASK_PRODUCT_NOQTY": [
        {"role": "user", "content": "อยากสั่งของจากจีน"},
        {"role": "assistant", "content": _ASK_PRODUCT_NOQTY},
    ],
    "ASK_METHOD": [
        {"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
        {"role": "assistant", "content": _ASK_PRODUCT},
        {"role": "user", "content": "รองเท้าครับ"},
        {"role": "assistant", "content": _ASK_METHOD},
    ],
    "ASK_QTY": [
        {"role": "user", "content": "อยากนำเข้ารองเท้าจากจีน"},
        {"role": "assistant", "content": _ASK_QTY},
    ],
    "JOURNEY": [
        {"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
        {"role": "assistant", "content": _ASK_PRODUCT},
        {"role": "user", "content": "รองเท้าครับ"},
        {"role": "assistant", "content": _ASK_METHOD},
        {"role": "user", "content": "ทางเรือครับ"},
        {"role": "assistant", "content": _ASK_WEIGHT},
    ],
}

IMPORT = "IMPORT_INTEREST"
POLICY = "PRODUCT_POLICY"
STATUS = "SHIPMENT_STATUS"
CONTACT = "CONTACT_INFO"

# ── base sentences with ground truth ─────────────────────────────────
BASE: List[Tuple[str, Truth]] = [
    # import-interest openers
    ("อยากสั่งของจากจีน", Truth(IMPORT)),
    ("อยากนำเข้าของจากจีน", Truth(IMPORT)),
    ("สนใจนำเข้าสินค้าจีน", Truth(IMPORT)),
    ("อยากฝากสั่งของจีน", Truth(IMPORT)),
    ("20 คู่อยากสั่งของจากจีน", Truth(IMPORT, quantity=20, unit="คู่")),
    ("อยากสั่งของจากจีน 15 ชิ้น", Truth(IMPORT, quantity=15, unit="ชิ้น")),
    ("อยากนำเข้ารองเท้าจากจีน 50 คู่", Truth(IMPORT, product="รองเท้า", quantity=50, unit="คู่")),
    ("สนใจนำเข้ากล่องพลาสติก 5 ลัง ส่งทางเรือ", Truth(IMPORT, product="กล่องพลาสติก", quantity=5, unit="ลัง", method="sea")),
    ("ต้องการนำเข้าของเล่นจากจีน", Truth(IMPORT, product="ของเล่น")),
    ("อยากได้ชั้นวางของ 10 ชิ้น", Truth(IMPORT, product="ชั้นวางของ", quantity=10, unit="ชิ้น")),
    ("สนใจสั่งเครื่องครัวจากจีน 30 ชุด ทางรถ", Truth(IMPORT, product="เครื่องครัว", quantity=30, unit="ชุด", method="road")),
    ("จะนำเข้าเสื้อผ้า 100 ตัว", Truth(IMPORT, product="เสื้อผ้า", quantity=100, unit="ตัว")),
    ("อยากสั่งหมวกจากจีน 200 ใบ ส่งทางรถ", Truth(IMPORT, product="หมวก", quantity=200, unit="ใบ", method="road")),
    ("สนใจนำเข้าโคมไฟ 12 ชิ้น", Truth(IMPORT, product="โคมไฟ", quantity=12, unit="ชิ้น")),
    # slot answers inside a journey
    ("รองเท้าครับ", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT")),
    ("เป็นชั้นวางของ", Truth(IMPORT, product="ชั้นวางของ", check_quantity=False, history="ASK_PRODUCT")),
    ("เอากระต่ายครับ", Truth(IMPORT, product="กระต่าย", check_quantity=False, history="ASK_PRODUCT")),
    ("สินค้าเป็นกระเป๋าค่ะ", Truth(IMPORT, product="กระเป๋า", check_quantity=False, history="ASK_PRODUCT")),
    ("ของเล่นครับ", Truth(IMPORT, product="ของเล่น", check_quantity=False, history="ASK_PRODUCT_NOQTY")),
    ("ทางเรือครับ", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("ส่งรถค่ะ", Truth(IMPORT, method="road", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("ส่งเรือได้ไหม", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("20 คู่", Truth(IMPORT, quantity=20, unit="คู่", check_product=False, history="ASK_QTY")),
    ("ประมาณ 50 ชิ้นค่ะ", Truth(IMPORT, quantity=50, unit="ชิ้น", check_product=False, history="ASK_QTY")),
    ("เอ้ย 10 คู่", Truth(IMPORT, quantity=10, unit="คู่", check_product=False, check_method=False, history="JOURNEY")),
    # product policy
    ("กระต่ายนำเข้าได้ไหม", Truth(POLICY, product="กระต่าย", auth=False)),
    ("ของเล่นนำเข้าได้ไหม", Truth(POLICY, product="ของเล่น", auth=False)),
    ("เครื่องสำอางนำเข้าได้หรือเปล่า", Truth(POLICY, product="เครื่องสำอาง", auth=False)),
    ("ปืนนำเข้าได้ไหม", Truth(POLICY, product="ปืน", auth=False)),
    ("แบตเตอรี่นำเข้าได้ไหมครับ", Truth(POLICY, product="แบตเตอรี่", auth=False)),
    ("อาหารเสริมนำเข้าได้ไหมคะ", Truth(POLICY, product="อาหารเสริม", auth=False)),
    ("ของแบบนี้นำเข้าได้ไหม", Truth(POLICY, product="รองเท้า", check_quantity=False, check_method=False, history="JOURNEY",
                                    note="demonstrative refers to the known product")),
    # shipment status — private (ownership evidence) and public
    ("ขอเช็คออเดอร์ของผมหน่อย", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("ของผมถึงไทยหรือยัง", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("พัสดุของฉันถึงไหนแล้ว", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("เช็คสถานะพัสดุของผมหน่อยครับ", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("ของถึงไทยหรือยัง", Truth({STATUS, "GENERAL_ASSISTANCE"})),
    ("ติดตามพัสดุ", Truth({STATUS, "GENERAL_ASSISTANCE"})),
    # contact
    ("ขอเบอร์ติดต่อ", Truth(CONTACT, auth=False)),
    ("ขอเบอร์โทรหน่อยครับ", Truth(CONTACT, auth=False)),
    ("ติดต่อเจ้าหน้าที่ยังไง", Truth({CONTACT, "HUMAN_CS", "GENERAL_ASSISTANCE"}, auth=False)),
    # withdrawal / cancellation / coupon / invoice
    ("ถอนเงิน", Truth("PURCHASE_WITHDRAWAL")),
    ("อยากถอนเงินจากวอลเล็ท", Truth({"PURCHASE_WITHDRAWAL", "WALLET_WITHDRAWAL"})),
    ("ยกเลิกออเดอร์ได้ไหม", Truth({"CANCELLATION_POLICY"}, auth=False)),
    ("ขอยกเลิกออเดอร์ POS123456", Truth({"CANCELLATION_OPERATION"}, check_product=False)),
    ("คูปองใช้ยังไง", Truth("COUPON_USAGE", auth=False)),
    ("มีคูปองส่วนลดไหม", Truth({"COUPON_USAGE", "MY_COUPONS"})),
    ("ขอใบกำกับภาษี", Truth("INVOICE")),
    ("ออกใบกำกับภาษีได้ไหม", Truth("INVOICE")),
    # price / duration
    ("ค่าส่งเท่าไหร่", Truth({"SHIPPING_ESTIMATE", "SHIPPING_RATE", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False)),
    ("ส่งทางเรือกิโลละเท่าไหร่", Truth({"SHIPPING_ESTIMATE", "SHIPPING_RATE", "GENERAL_ASSISTANCE", "UNKNOWN"}, method="sea", auth=False)),
    ("ส่งทางรถกี่วัน", Truth({"SHIPPING_ESTIMATE", "SHIPPING_DURATION", "GENERAL_ASSISTANCE", "UNKNOWN"}, method="road", auth=False)),
    # address change
    ("ขอเปลี่ยนที่อยู่จัดส่ง", Truth("ADDRESS_CHANGE")),
    # general
    ("สวัสดีครับ", Truth({"GREETING", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False)),
    ("ชิปปิ้งคืออะไร", Truth({"SERVICE_DISCOVERY", "GENERAL_ASSISTANCE", "UNKNOWN", "IMPORT_INTEREST"}, auth=False)),
]

# the owner's own list (task §8/§15), with the same ground-truth shape.
HAND_TYPO_CASES: List[Tuple[str, str, Truth]] = [
    # (noisy, clean, truth)
    ("20คุ่อยากสั่งขงจากจีน", "20 คู่อยากสั่งของจากจีน", Truth(IMPORT, quantity=20, unit="คู่")),
    ("20คู่อยากสั่งของจากจีน", "20 คู่อยากสั่งของจากจีน", Truth(IMPORT, quantity=20, unit="คู่")),
    ("20คู่ครับอยากสั่งของจากจีน", "20 คู่อยากสั่งของจากจีน", Truth(IMPORT, quantity=20, unit="คู่")),
    ("เป้นรองเท้าคับ", "เป็นรองเท้าครับ", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT")),
    ("เปนรองเท้า", "เป็นรองเท้า", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT")),
    ("เป้นนรองเท้าค้าบ", "เป็นรองเท้าครับ", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT")),
    ("รองเท้า คับ", "รองเท้าครับ", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT")),
    ("ส่งเรื่อได้ปะ", "ส่งเรือได้ไหม", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("ทางเรือคับ", "ทางเรือครับ", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("กระต่ายนำเข่าได้หรอ", "กระต่ายนำเข้าได้ไหม", Truth(POLICY, product="กระต่าย", auth=False)),
    ("กระต่ายนำเขาได้ไหม", "กระต่ายนำเข้าได้ไหม", Truth(POLICY, product="กระต่าย", auth=False)),
    ("ขอเช็คออเดอของผมหนอย", "ขอเช็คออเดอร์ของผมหน่อย", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("ถอนเงืน", "ถอนเงิน", Truth("PURCHASE_WITHDRAWAL")),
    ("ถอนเงืนหน่อย", "ถอนเงินหน่อย", Truth("PURCHASE_WITHDRAWAL")),
    ("ยกเลกออเดอได้ไหม", "ยกเลิกออเดอร์ได้ไหม", Truth({"CANCELLATION_POLICY"}, auth=False)),
    ("ใบกำกับภาษีี", "ใบกำกับภาษี", Truth("INVOICE")),
    ("ขอใบกำกับภาษีีหน่อย", "ขอใบกำกับภาษีหน่อย", Truth("INVOICE")),
    ("คุปองใช้ยังไง", "คูปองใช้ยังไง", Truth("COUPON_USAGE", auth=False)),
    ("เช็คออเดอของผม", "เช็คออเดอร์ของผม", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("ของถึงไทหรือยัง", "ของถึงไทยหรือยัง", Truth({STATUS, "GENERAL_ASSISTANCE"})),
    ("ของผมถึงไทหรือยัง", "ของผมถึงไทยหรือยัง", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True)),
    ("ส่งเรื่อ", "ส่งเรือ", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("สั่งของจน", "สั่งของจีน", Truth({IMPORT, "UNKNOWN", "GENERAL_ASSISTANCE"})),
    ("อยากสั่งของจนครับ", "อยากสั่งของจีนครับ", Truth(IMPORT)),
    ("อยากนำเข่าของจากจีน", "อยากนำเข้าของจากจีน", Truth(IMPORT)),
    ("อยากนำเขาของจากจีน", "อยากนำเข้าของจากจีน", Truth(IMPORT)),
    ("สนใจนำเข่าสินค้าจีน", "สนใจนำเข้าสินค้าจีน", Truth(IMPORT)),
    ("อยากสั่งขงจากจีน", "อยากสั่งของจากจีน", Truth(IMPORT)),
    ("ทางเรือคับ ราคาเท่าไหร่", "ทางเรือครับ ราคาเท่าไหร่", Truth({IMPORT, "SHIPPING_ESTIMATE", "GENERAL_ASSISTANCE", "UNKNOWN"}, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("ขอเบอร์ติดต่อหนอย", "ขอเบอร์ติดต่อหน่อย", Truth(CONTACT, auth=False)),
    ("ขอเบอติดต่อ", "ขอเบอร์ติดต่อ", Truth(CONTACT, auth=False)),
    ("งั้นขอเบอร์ติดต่อ", "ขอเบอร์ติดต่อ", Truth(CONTACT, auth=False)),
    ("อยากได้ 5 ชินครับ", "อยากได้ 5 ชิ้นครับ", Truth(IMPORT, quantity=5, unit="ชิ้น", check_product=False, history="ASK_QTY")),
    ("20คู่ครับ", "20 คู่ครับ", Truth(IMPORT, quantity=20, unit="คู่", check_product=False, history="ASK_QTY")),
    ("20 คู่", "20 คู่", Truth(IMPORT, quantity=20, unit="คู่", check_product=False, history="ASK_QTY")),
    ("เอ้ยย 10 คุ่", "เอ้ย 10 คู่", Truth(IMPORT, quantity=10, unit="คู่", check_product=False, check_method=False, history="JOURNEY")),
    ("ของแบบนี้นำเข่าได้หรอ", "ของแบบนี้นำเข้าได้ไหม", Truth(POLICY, product="รองเท้า", check_quantity=False, check_method=False, history="JOURNEY")),
    ("เป้นชันวางของ", "เป็นชั้นวางของ", Truth(IMPORT, product="*", check_quantity=False, history="ASK_PRODUCT",
                                            note="unknown-noun spelling is preserved, never rewritten")),
    ("ชั้นวางของครับบบ", "ชั้นวางของครับ", Truth(IMPORT, product="ชั้นวางของ", check_quantity=False, history="ASK_PRODUCT")),
    ("เอาชั้นวางของ", "ชั้นวางของ", Truth(IMPORT, product="ชั้นวางของ", check_quantity=False, history="ASK_PRODUCT")),
    ("สินค้าเป็นชั้นวางของ", "ชั้นวางของ", Truth(IMPORT, product="ชั้นวางของ", check_quantity=False, history="ASK_PRODUCT")),
    ("l;ylfu8iy[", "สวัสดีครับ", Truth({"GREETING", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False)),
    ("อยากสั่งของจากจีนครับบบบ", "อยากสั่งของจากจีนครับ", Truth(IMPORT)),
    ("อยาก   สั่งของ  จากจีน", "อยากสั่งของจากจีน", Truth(IMPORT)),
    ("อยาก​สั่งของ​จากจีน", "อยากสั่งของจากจีน", Truth(IMPORT)),
    ("ค่าส่งเท่าไหร่ค้าบ", "ค่าส่งเท่าไหร่ครับ", Truth({"SHIPPING_ESTIMATE", "SHIPPING_RATE", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False)),
    ("ส่งทางรดได้ไหม", "ส่งทางรถได้ไหม", Truth(IMPORT, method="road", check_product=False, check_quantity=False, history="ASK_METHOD")),
    ("มีคุปองส่วนลดมั้ย", "มีคูปองส่วนลดไหม", Truth({"COUPON_USAGE", "MY_COUPONS"})),
    ("ยกเลกได้ปะ", "ยกเลิกได้ไหม", Truth({"CANCELLATION_POLICY", "UNKNOWN", "GENERAL_ASSISTANCE"})),
]

# identifiers that must survive normalisation byte-for-byte (task §9).
IDENTIFIER_CASES: List[Tuple[str, str]] = [
    ("ขอเช็ค POS123456 หน่อย", "POS123456"),
    ("FT3182 ถึงไทหรือยัง", "FT3182"),
    ("แทรค TH1234567890TH ให้หน่อย", "TH1234567890TH"),
    ("เลขพัสดุ 1234567890123 ถึงไหนแล้ว", "1234567890123"),
    ("https://detail.1688.com/offer/612345678901.html ส่งเรื่อได้ปะ", "https://detail.1688.com/offer/612345678901.html"),
    ("ลิงก์ https://item.taobao.com/item.htm?id=612345678901 อันนี้", "https://item.taobao.com/item.htm?id=612345678901"),
    ("เบอร์ 081-234-5678 ติดต่อกลับหน่อย", "081-234-5678"),
    ("โทร 0812345678 นะคับ", "0812345678"),
    ("รหัสลูกค้า CS0001234 ครับ", "CS0001234"),
    ("email test.user@example.com ค้าบ", "test.user@example.com"),
    ("PO-2024-000123 ยกเลกได้ไหม", "PO-2024-000123"),
    ("ราคา 1,500 บาท ส่งเรื่อ", "1,500"),
]

# unknown product names that must never be rewritten (task §11).
UNKNOWN_PRODUCT_CASES: List[Tuple[str, str]] = [
    ("กระต่ายนำเข้าได้ไหม", "กระต่าย"),
    ("กระต่ายครับ", "กระต่าย"),
    ("ฟิกเกอร์นำเข้าได้ไหม", "ฟิกเกอร์"),
    ("เคสไอโฟนครับ", "เคสไอโฟน"),
    ("ลิงยางนำเข้าได้ไหม", "ลิงยาง"),
    ("เรือบังคับครับ", "เรือบังคับ"),
    ("รถเข็นเด็กค่ะ", "รถเข็นเด็ก"),
    ("ขวดน้ำครับ", "ขวดน้ำ"),
    ("โคมไฟระย้าครับ", "โคมไฟระย้า"),
    ("เป็นชุดนอนค่ะ", "ชุดนอน"),
    ("กล่องพลาสติกครับ", "กล่องพลาสติก"),
    ("เรื่องราวครับ", "เรื่องราว"),
]


# real, correctly spelled phrases that a spelling layer could plausibly
# "fix" into a different business meaning (send vs order, monkey vs link,
# crate vs link, complete vs the particle). OVER_CORRECTION = 0 means every
# one of them reaches the interpreter byte-identical (whitespace aside)
# after normalisation AND semantic recovery.
REAL_PHRASE_CASES: List[str] = [
    "ส่งของครบแล้ว", "ส่งของถึงไทยหรือยัง", "ได้รับของครบ", "ส่งของให้ครบ", "ของมาครบยัง",
    "กระต่ายนำเข้าได้ไหม", "ลิงนำเข้าได้ไหม", "เอาลิงยาง", "3 ลัง", "5 ลังครับ", "เรื่องนี้ยังไง",
    "เรือบังคับครับ", "รถเข็นเด็กค่ะ", "ชิปปิ้งคืออะไรคะ", "ขอเบอร์ติดต่อ", "งั้นขอเบอร์ติดต่อ",
    "ค่าส่งเท่าไหร่", "ของถึงไทยหรือยัง", "ส่งทางรถกี่วัน", "ไม่เอาแล้วครับ", "สั่งของจากจีน",
    "ยกเลิกออเดอร์ได้ไหม", "ที่ชาร์จ", "แก้วน้ำ 10 ใบ", "ขวดน้ำครับ", "โคมไฟระย้าครับ", "ของเล่นครับ",
    "สินค้ามือสองค่ะ", "อยากได้ 5 ชิ้นครับ", "รับของที่โกดังได้ไหม", "รดน้ำต้นไม้", "ทางรถไฟ",
    "เรือดำน้ำ", "แล้วราคาเท่าไหร่อะ", "เอ้ย 10 คู่", "ของแบบนี้นำเข้าได้หรอ", "ปืนนำเข้าได้ไหม",
]


# ── noise operators ──────────────────────────────────────────────────
_TONE = ["่", "้"]
_MARKS = set("ัิีึืุู็่้๊๋์")
_VOCAB = sorted((t for t in V.all_terms() if len(t) >= 3 and re.search(r"[฀-๿]", t)),
                key=len, reverse=True)
_PARTICLE_VARIANTS = {
    "ครับ": ["คับ", "ค้าบ", "คร้าบ", "ครับบบ", "ครัช"],
    "ค่ะ": ["คร่า", "ค่ะๆ", "ค้ะ"],
    "ไหม": ["มั้ย", "ปะ", "ป่ะ", "หรอ", "มัย"],
}


def _vocab_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    for term in _VOCAB:
        for m in re.finditer(re.escape(term), text):
            if not any(a <= m.start() < b for a, b in spans):
                spans.append((m.start(), m.end()))
    return sorted(spans)


def op_drop_mark(text: str, rnd: random.Random) -> Optional[str]:
    """Drop one combining mark from a vocabulary word (นำเข้า -> นำเขา,
    ยกเลิก -> ยกเลก, จีน -> จน)."""
    for a, b in rnd.sample(_vocab_spans(text), k=len(_vocab_spans(text))) or []:
        idx = [i for i in range(a, b) if text[i] in _MARKS and text[i] != "์"]
        if idx:
            i = rnd.choice(idx)
            return text[:i] + text[i + 1:]
    return None


def op_swap_tone(text: str, rnd: random.Random) -> Optional[str]:
    """Swap ่ and ้ inside a vocabulary word (นำเข้า -> นำเข่า)."""
    for a, b in _vocab_spans(text):
        idx = [i for i in range(a, b) if text[i] in _TONE]
        if idx:
            i = rnd.choice(idx)
            other = _TONE[1] if text[i] == _TONE[0] else _TONE[0]
            return text[:i] + other + text[i + 1:]
    return None


def op_dup_mark(text: str, rnd: random.Random) -> Optional[str]:
    """Duplicate one vowel/tone mark (ภาษี -> ภาษีี)."""
    idx = [i for i, ch in enumerate(text) if ch in _MARKS and ch != "์"]
    if not idx:
        return None
    i = rnd.choice(idx)
    return text[:i + 1] + text[i] + text[i + 1:]


def op_double_final(text: str, rnd: random.Random) -> Optional[str]:
    """Double the final consonant of a vocabulary word (เป็น -> เป็นน)."""
    spans = _vocab_spans(text)
    if not spans:
        return None
    a, b = rnd.choice(spans)
    last = text[b - 1]
    if last in _MARKS or not re.match(r"[ก-ฮ]", last):
        return None
    return text[:b] + last + text[b:]


def op_drop_thanthakhat(text: str, rnd: random.Random) -> Optional[str]:
    """Drop a silenced final consonant (ออเดอร์ -> ออเดอ)."""
    m = re.search(r"[ก-ฮ]์", text)
    if not m:
        return None
    return text[:m.start()] + text[m.end():]


def op_particle(text: str, rnd: random.Random) -> Optional[str]:
    for canon, variants in _PARTICLE_VARIANTS.items():
        if text.endswith(canon):
            return text[:-len(canon)] + rnd.choice(variants)
    return None


def op_add_particle(text: str, rnd: random.Random) -> Optional[str]:
    if re.search(r"(ครับ|ค่ะ|คะ|ไหม|มั้ย)$", text):
        return None
    return text + rnd.choice(["คับ", "ค้าบ", "ครับ", "ค่ะ", "คร่า"])


def op_remove_spaces(text: str, rnd: random.Random) -> Optional[str]:
    if " " not in text:
        return None
    return text.replace(" ", "")


def op_extra_spaces(text: str, rnd: random.Random) -> Optional[str]:
    m = re.search(r"(?<![A-Za-z0-9-])\d+", text)     # never inside a code
    if m:
        return text[:m.start()] + "  " + m.group(0) + "   " + text[m.end():]
    parts = text.split(" ")
    if len(parts) > 1:
        return "   ".join(parts)
    return None


def op_elongate(text: str, rnd: random.Random) -> Optional[str]:
    m = re.search(r"[ก-ฮ]$", text)
    if not m:
        return None
    return text + text[-1] * rnd.choice([2, 3])


def op_zero_width(text: str, rnd: random.Random) -> Optional[str]:
    idx = [i for i in range(1, len(text)) if re.match(r"[ก-ฮ]", text[i])]
    if not idx:
        return None
    i = rnd.choice(idx)
    return text[:i] + rnd.choice(["​", "‍", "﻿"]) + text[i:]


def op_nbsp(text: str, rnd: random.Random) -> Optional[str]:
    if " " not in text:
        return None
    return text.replace(" ", " ", 1)


def op_keyboard(text: str, rnd: random.Random) -> Optional[str]:
    """The whole message typed with the layout left in English."""
    if re.search(r"[0-9A-Za-z]", text) or len(text) > 14:
        return None
    try:
        from pythainlp.util import thai_to_eng, eng_to_thai
    except Exception:
        return None
    latin = thai_to_eng(text)
    if not latin or eng_to_thai(latin) != text or re.search(r"[฀-๿]", latin):
        return None
    return latin


NOISE_OPERATORS = [
    ("drop_mark", op_drop_mark), ("swap_tone", op_swap_tone), ("dup_mark", op_dup_mark),
    ("double_final", op_double_final), ("drop_thanthakhat", op_drop_thanthakhat),
    ("particle", op_particle), ("add_particle", op_add_particle),
    ("remove_spaces", op_remove_spaces), ("extra_spaces", op_extra_spaces),
    ("elongate", op_elongate), ("zero_width", op_zero_width), ("nbsp", op_nbsp),
    ("keyboard", op_keyboard),
]


def _combined(text: str, rnd: random.Random) -> Optional[str]:
    """Two mistakes in one message."""
    out = text
    applied = 0
    for name, op in rnd.sample(NOISE_OPERATORS[:7], k=4):
        got = op(out, rnd)
        if got and got != out:
            out = got
            applied += 1
        if applied == 2:
            break
    return out if applied == 2 else None


@dataclass
class TypoCase:
    noisy: str
    clean: str
    op: str
    truth: Truth
    history: Optional[str]


def typo_cases(seed: int = 6) -> List[TypoCase]:
    rnd = random.Random(seed)
    out: List[TypoCase] = []
    seen: Set[Tuple[str, Optional[str]]] = set()
    for noisy, clean, truth in HAND_TYPO_CASES:
        out.append(TypoCase(noisy, clean, "hand", truth, truth.history))
        seen.add((noisy, truth.history))
    for text, truth in BASE:
        for name, op in NOISE_OPERATORS + [("combined", _combined)]:
            got = op(text, rnd)
            if not got or got == text or (got, truth.history) in seen:
                continue
            seen.add((got, truth.history))
            out.append(TypoCase(got, text, name, truth, truth.history))
    return out


# ── paraphrase groups ────────────────────────────────────────────────
@dataclass
class ParaphraseGroup:
    name: str
    truth: Truth
    variants: List[str]


PARAPHRASE_GROUPS: List[ParaphraseGroup] = [
    ParaphraseGroup("import_opener", Truth(IMPORT), [
        "อยากสั่งของจากจีน", "อยากนำเข้าของจากจีน", "จะซื้อของจีนเข้ามาไทย", "สนใจนำเข้าสินค้าจีน",
        "สั่งจากจีนได้ไหม", "อยากฝากสั่งของจีน", "ต้องการนำเข้าสินค้าจากจีน", "สนใจสั่งของจากจีนครับ",
        "อยากนำสินค้าจากจีนเข้ามาขาย", "จะสั่งของจากจีนต้องทำยังไง", "ฝากสั่งของจีนได้ไหมคะ", "สนใจใช้บริการนำเข้าค่ะ"]),
    ParaphraseGroup("import_qty_opener", Truth(IMPORT, quantity=20, unit="คู่"), [
        "20 คู่อยากสั่งของจากจีน", "อยากสั่งของจากจีน 20 คู่", "สนใจนำเข้า 20 คู่", "จะสั่ง 20 คู่จากจีน",
        "20คู่ อยากนำเข้าจากจีน", "อยากได้ 20 คู่จากจีน", "ต้องการสั่งของจีนประมาณ 20 คู่", "ฝากสั่งของจีน 20 คู่ครับ"]),
    ParaphraseGroup("import_product_opener", Truth(IMPORT, product="รองเท้า"), [
        "อยากนำเข้ารองเท้าจากจีน", "สนใจสั่งรองเท้าจากจีน", "อยากสั่งรองเท้าจากจีนครับ", "ต้องการนำเข้ารองเท้า",
        "จะนำเข้ารองเท้าจากจีนค่ะ", "ฝากสั่งรองเท้าจากจีนได้ไหม", "อยากได้รองเท้าจากจีน", "สนใจนำเข้ารองเท้าค่ะ"]),
    ParaphraseGroup("import_product_qty_opener", Truth(IMPORT, product="เสื้อผ้า", quantity=100, unit="ตัว"), [
        "อยากนำเข้าเสื้อผ้า 100 ตัว", "สนใจสั่งเสื้อผ้าจากจีน 100 ตัว", "จะนำเข้าเสื้อผ้า 100 ตัวจากจีน",
        "100 ตัว อยากสั่งเสื้อผ้าจากจีน", "ต้องการนำเข้าเสื้อผ้าประมาณ 100 ตัว", "ฝากสั่งเสื้อผ้า 100 ตัวครับ",
        "อยากได้เสื้อผ้า 100 ตัวจากจีน", "สนใจนำเข้าเสื้อผ้าจำนวน 100 ตัวค่ะ"]),
    ParaphraseGroup("product_answer", Truth(IMPORT, product="รองเท้า", check_quantity=False, history="ASK_PRODUCT"), [
        "รองเท้าครับ", "เป็นรองเท้าค่ะ", "เอารองเท้า", "สินค้าเป็นรองเท้า", "รองเท้า", "อยากสั่งรองเท้า",
        "เป็นพวกรองเท้าครับ", "รองเท้าค่ะ"]),
    ParaphraseGroup("product_answer_compound", Truth(IMPORT, product="ชั้นวางของ", check_quantity=False, history="ASK_PRODUCT"), [
        "ชั้นวางของครับ", "เป็นชั้นวางของ", "เอาชั้นวางของ", "สินค้าเป็นชั้นวางของ", "ชั้นวางของ", "ชั้นวางของค่ะ",
        "เป็นชั้นวางของครับ", "อยากสั่งชั้นวางของ"]),
    ParaphraseGroup("method_answer_sea", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD"), [
        "ทางเรือครับ", "ส่งเรือ", "เรือ", "เอาทางเรือ", "ส่งทางเรือค่ะ", "ทางเรือ", "ส่งเรือได้ไหม", "ขอทางเรือครับ"]),
    ParaphraseGroup("method_answer_road", Truth(IMPORT, method="road", check_product=False, check_quantity=False, history="ASK_METHOD"), [
        "ทางรถครับ", "ส่งรถ", "รถ", "เอาทางรถ", "ส่งทางรถค่ะ", "ทางรถ", "ส่งรถได้ไหม", "ขอทางรถครับ"]),
    ParaphraseGroup("qty_answer", Truth(IMPORT, quantity=50, unit="ชิ้น", check_product=False, history="ASK_QTY"), [
        "50 ชิ้น", "ประมาณ 50 ชิ้นค่ะ", "50ชิ้นครับ", "เอา 50 ชิ้น", "50 ชิ้นครับ", "สัก 50 ชิ้น", "ประมาณ50ชิ้น", "50 ชิ้นค่ะ"]),
    ParaphraseGroup("qty_correction", Truth(IMPORT, quantity=10, unit="คู่", check_product=False, check_method=False, history="JOURNEY"), [
        "เอ้ย 10 คู่", "เปลี่ยนเป็น 10 คู่", "แก้เป็น 10 คู่", "เอา 10 คู่แทน", "ขอแก้เป็น 10 คู่ครับ", "เอ๊ย 10 คู่",
        "เปลี่ยนเป็น 10 คู่ค่ะ", "ไม่ใช่ 20 เอา 10 คู่"]),
    ParaphraseGroup("policy_rabbit", Truth(POLICY, product="กระต่าย", auth=False), [
        "กระต่ายนำเข้าได้ไหม", "กระต่ายนำเข้าได้หรอ", "กระต่ายสั่งจากจีนได้ไหม", "นำเข้ากระต่ายได้ไหม",
        "กระต่ายนำเข้าได้หรือเปล่า", "กระต่ายส่งเข้าไทยได้ไหม", "กระต่ายนำเข้าได้ป่ะ", "กระต่ายนำเข้าได้มั้ยครับ"]),
    ParaphraseGroup("policy_toys", Truth(POLICY, product="ของเล่น", auth=False), [
        "ของเล่นนำเข้าได้ไหม", "ของเล่นนำเข้าได้หรือเปล่า", "นำเข้าของเล่นได้ไหม", "ของเล่นสั่งจากจีนได้ไหม",
        "ของเล่นนำเข้าได้มั้ย", "ของเล่นส่งเข้าไทยได้ไหม", "ของเล่นนำเข้าได้หรอ", "ของเล่นนำเข้าได้ป่ะครับ"]),
    ParaphraseGroup("policy_known_product", Truth(POLICY, product="รองเท้า", check_quantity=False, check_method=False, history="JOURNEY"), [
        "ของแบบนี้นำเข้าได้ไหม", "ของแบบนี้นำเข้าได้หรอ", "อันนี้นำเข้าได้ไหม", "รองเท้านำเข้าได้ไหม",
        "แบบนี้นำเข้าได้หรือเปล่า", "ของแบบนี้นำเข้าได้มั้ย", "รองเท้านำเข้าได้หรอ", "สินค้านี้นำเข้าได้ไหม"]),
    ParaphraseGroup("private_status", Truth({STATUS, "GENERAL_ASSISTANCE"}, auth=True), [
        "ขอเช็คออเดอร์ของผมหน่อย", "ของผมถึงไทยหรือยัง", "พัสดุของฉันถึงไหนแล้ว", "เช็คสถานะพัสดุของผมหน่อยครับ",
        "ออเดอร์ของผมถึงไหนแล้ว", "ของผมถึงไหนแล้วครับ", "ขอเช็คสถานะของผมหน่อย", "สินค้าของผมถึงไทยยัง"]),
    ParaphraseGroup("contact", Truth(CONTACT, auth=False), [
        "ขอเบอร์ติดต่อ", "ขอเบอร์โทรหน่อยครับ", "ติดต่อได้ที่ไหน", "ขอเบอร์ติดต่อหน่อยค่ะ", "งั้นขอเบอร์ติดต่อ",
        "ขอช่องทางติดต่อ", "เบอร์ติดต่อบริษัท", "ขอเบอร์โทรติดต่อค่ะ"]),
    ParaphraseGroup("withdrawal", Truth({"PURCHASE_WITHDRAWAL", "WALLET_WITHDRAWAL"}), [
        "ถอนเงิน", "อยากถอนเงิน", "ขอถอนเงินหน่อย", "ถอนเงินยังไง", "ต้องการถอนเงินค่ะ", "ถอนเงินได้ไหม",
        "จะถอนเงินครับ", "ขอถอนเงินคืน"]),
    ParaphraseGroup("cancel_policy", Truth({"CANCELLATION_POLICY"}, auth=False), [
        "ยกเลิกออเดอร์ได้ไหม", "ยกเลิกออเดอร์ได้หรือเปล่า", "ยกเลิกออเดอร์ได้มั้ย", "ออเดอร์ยกเลิกได้ไหม",
        "ยกเลิกออเดอร์ได้หรอ", "ยกเลิกออเดอร์ได้ป่ะ", "สั่งแล้วยกเลิกออเดอร์ได้ไหม", "ยกเลิกออเดอร์ได้ไหมครับ"]),
    ParaphraseGroup("coupon", Truth({"COUPON_USAGE", "MY_COUPONS"}, auth=None), [
        "คูปองใช้ยังไง", "ใช้คูปองยังไง", "คูปองใช้อย่างไร", "วิธีใช้คูปอง", "คูปองส่วนลดใช้ยังไงคะ",
        "ใช้คูปองส่วนลดยังไง", "คูปองใช้ยังไงครับ", "มีคูปองส่วนลดไหม"]),
    ParaphraseGroup("invoice", Truth("INVOICE"), [
        "ขอใบกำกับภาษี", "ออกใบกำกับภาษีได้ไหม", "อยากได้ใบกำกับภาษี", "ขอใบกำกับภาษีหน่อยค่ะ",
        "ออกใบกำกับภาษีให้หน่อย", "ใบกำกับภาษีขอได้ไหม", "ต้องการใบกำกับภาษีครับ", "ขอ tax invoice"]),
    ParaphraseGroup("price", Truth({"SHIPPING_ESTIMATE", "SHIPPING_RATE", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False), [
        "ค่าส่งเท่าไหร่", "ค่าส่งเท่าไร", "ค่าขนส่งเท่าไหร่", "ค่าส่งคิดยังไง", "ราคาค่าส่งเท่าไหร่คะ",
        "ค่าส่งกิโลละเท่าไหร่", "ค่าส่งเท่าไหร่ครับ", "ค่าส่งประมาณเท่าไหร่"]),
    ParaphraseGroup("address_change", Truth("ADDRESS_CHANGE"), [
        "ขอเปลี่ยนที่อยู่จัดส่ง", "เปลี่ยนที่อยู่ส่งของได้ไหม", "อยากเปลี่ยนที่อยู่จัดส่ง", "ขอแก้ที่อยู่จัดส่งหน่อย",
        "เปลี่ยนที่อยู่จัดส่งค่ะ", "ขอเปลี่ยนที่อยู่ส่งสินค้า", "เปลี่ยนที่อยู่จัดส่งได้มั้ย", "แก้ไขที่อยู่จัดส่งครับ"]),
    ParaphraseGroup("policy_cosmetics", Truth(POLICY, product="เครื่องสำอาง", auth=False), [
        "เครื่องสำอางนำเข้าได้ไหม", "เครื่องสำอางนำเข้าได้หรือเปล่า", "นำเข้าเครื่องสำอางได้ไหม",
        "เครื่องสำอางสั่งจากจีนได้ไหม", "เครื่องสำอางนำเข้าได้มั้ยคะ", "เครื่องสำอางส่งเข้าไทยได้ไหม",
        "เครื่องสำอางนำเข้าได้หรอ", "เครื่องสำอางนำเข้าได้ป่ะ"]),
    ParaphraseGroup("policy_battery", Truth(POLICY, product="แบตเตอรี่", auth=False), [
        "แบตเตอรี่นำเข้าได้ไหม", "แบตเตอรี่นำเข้าได้หรือเปล่า", "นำเข้าแบตเตอรี่ได้ไหม", "แบตเตอรี่สั่งจากจีนได้ไหม",
        "แบตเตอรี่นำเข้าได้มั้ย", "แบตเตอรี่ส่งเข้าไทยได้ไหม", "แบตเตอรี่นำเข้าได้หรอครับ", "แบตเตอรี่นำเข้าได้ป่ะ"]),
    ParaphraseGroup("import_qty_ชิ้น", Truth(IMPORT, quantity=15, unit="ชิ้น"), [
        "อยากสั่งของจากจีน 15 ชิ้น", "15 ชิ้นอยากสั่งของจากจีน", "สนใจนำเข้า 15 ชิ้น", "จะสั่ง 15 ชิ้นจากจีน",
        "อยากได้ 15 ชิ้นจากจีน", "ต้องการสั่งของจีน 15 ชิ้น", "ฝากสั่งของจีน 15 ชิ้นครับ", "อยากนำเข้าของจากจีน 15 ชิ้น"]),
    ParaphraseGroup("import_box_sea", Truth(IMPORT, product="กล่องพลาสติก", quantity=5, unit="ลัง", method="sea"), [
        "สนใจนำเข้ากล่องพลาสติก 5 ลัง ส่งทางเรือ", "อยากสั่งกล่องพลาสติก 5 ลัง ทางเรือ", "จะนำเข้ากล่องพลาสติก 5 ลัง ส่งเรือ",
        "อยากนำเข้ากล่องพลาสติกจากจีน 5 ลัง ทางเรือครับ", "ต้องการนำเข้ากล่องพลาสติก 5 ลัง ส่งทางเรือค่ะ",
        "ฝากสั่งกล่องพลาสติก 5 ลัง ส่งเรือ", "สนใจสั่งกล่องพลาสติกจากจีน 5 ลัง ทางเรือ", "อยากได้กล่องพลาสติก 5 ลัง ส่งทางเรือ"]),
    ParaphraseGroup("greeting", Truth({"GREETING", "GENERAL_ASSISTANCE", "UNKNOWN"}, auth=False), [
        "สวัสดีครับ", "สวัสดีค่ะ", "หวัดดีครับ", "สวัสดี", "ดีครับ", "สวัสดีค่า", "หวัดดี", "สวัสดีครับผม"]),
    ParaphraseGroup("policy_gun", Truth(POLICY, product="ปืน", auth=False), [
        "ปืนนำเข้าได้ไหม", "ปืนนำเข้าได้หรือเปล่า", "นำเข้าปืนได้ไหม", "ปืนสั่งจากจีนได้ไหม", "ปืนนำเข้าได้มั้ย",
        "ปืนส่งเข้าไทยได้ไหม", "ปืนนำเข้าได้หรอ", "ปืนนำเข้าได้ป่ะ"]),
    ParaphraseGroup("duration_road", Truth({"SHIPPING_ESTIMATE", "SHIPPING_DURATION", "GENERAL_ASSISTANCE", "UNKNOWN"}, method="road", auth=False), [
        "ส่งทางรถกี่วัน", "ทางรถใช้เวลากี่วัน", "ส่งทางรถกี่วันถึง", "ทางรถกี่วันครับ", "ส่งรถกี่วันถึงไทย",
        "ทางรถถึงไทยกี่วัน", "ส่งทางรถใช้เวลานานไหม", "ทางรถกี่วันคะ"]),
    ParaphraseGroup("method_sea_feasibility", Truth(IMPORT, method="sea", check_product=False, check_quantity=False, history="ASK_METHOD"), [
        "ส่งเรือได้ไหม", "ส่งเรือได้ปะ", "ทางเรือได้ไหมครับ", "ส่งทางเรือได้มั้ย", "เรือได้ไหม", "ส่งเรือได้หรอ",
        "ทางเรือได้ป่ะ", "เอาเรือได้ไหม"]),
    ParaphraseGroup("import_hat_road", Truth(IMPORT, product="หมวก", quantity=200, unit="ใบ", method="road"), [
        "อยากสั่งหมวกจากจีน 200 ใบ ส่งทางรถ", "สนใจนำเข้าหมวก 200 ใบ ทางรถ", "จะนำเข้าหมวก 200 ใบ ส่งรถ",
        "อยากนำเข้าหมวกจากจีน 200 ใบ ทางรถครับ", "ต้องการนำเข้าหมวก 200 ใบ ส่งทางรถค่ะ", "ฝากสั่งหมวก 200 ใบ ส่งรถ",
        "สนใจสั่งหมวกจากจีน 200 ใบ ทางรถ", "อยากได้หมวก 200 ใบ ส่งทางรถ"]),
    ParaphraseGroup("import_lamp", Truth(IMPORT, product="โคมไฟ", quantity=12, unit="ชิ้น"), [
        "สนใจนำเข้าโคมไฟ 12 ชิ้น", "อยากสั่งโคมไฟจากจีน 12 ชิ้น", "จะนำเข้าโคมไฟ 12 ชิ้น", "อยากได้โคมไฟ 12 ชิ้น",
        "ต้องการนำเข้าโคมไฟ 12 ชิ้นครับ", "ฝากสั่งโคมไฟ 12 ชิ้น", "สนใจสั่งโคมไฟจากจีน 12 ชิ้นค่ะ", "อยากนำเข้าโคมไฟจากจีน 12 ชิ้น"]),
    ParaphraseGroup("product_answer_bag", Truth(IMPORT, product="กระเป๋า", check_quantity=False, history="ASK_PRODUCT"), [
        "กระเป๋าครับ", "เป็นกระเป๋าค่ะ", "เอากระเป๋า", "สินค้าเป็นกระเป๋า", "กระเป๋า", "อยากสั่งกระเป๋า",
        "เป็นพวกกระเป๋าครับ", "กระเป๋าค่ะ"]),
    ParaphraseGroup("product_answer_toys", Truth(IMPORT, product="ของเล่น", check_quantity=False, history="ASK_PRODUCT_NOQTY"), [
        "ของเล่นครับ", "เป็นของเล่นค่ะ", "เอาของเล่น", "สินค้าเป็นของเล่น", "ของเล่น", "อยากสั่งของเล่น",
        "เป็นพวกของเล่นครับ", "ของเล่นค่ะ"]),
    ParaphraseGroup("qty_answer_boxes", Truth(IMPORT, quantity=3, unit="ลัง", check_product=False, history="ASK_QTY"), [
        "3 ลัง", "ประมาณ 3 ลังค่ะ", "3ลังครับ", "เอา 3 ลัง", "3 ลังครับ", "สัก 3 ลัง", "ประมาณ3ลัง", "3 ลังค่ะ"]),
    ParaphraseGroup("public_status", Truth({STATUS, "GENERAL_ASSISTANCE"}), [
        "ของถึงไทยหรือยัง", "ของถึงไทยยัง", "สินค้าถึงไทยหรือยัง", "ของถึงไทยรึยัง", "ของถึงไทยแล้วหรือยัง",
        "ของมาถึงไทยหรือยัง", "สินค้าถึงไทยยังครับ", "ของถึงไทยหรือยังคะ"]),
    ParaphraseGroup("cancel_operation", Truth({"CANCELLATION_OPERATION"}, check_product=False), [
        "ขอยกเลิกออเดอร์ POS123456", "ยกเลิกออเดอร์ POS123456 ให้หน่อย", "ขอยกเลิก POS123456", "ยกเลิกออเดอร์ POS123456 ครับ",
        "ช่วยยกเลิกออเดอร์ POS123456 หน่อย", "ยกเลิก POS123456 ให้ด้วยค่ะ", "อยากยกเลิกออเดอร์ POS123456", "ขอยกเลิกออเดอร์ POS123456 ค่ะ"]),
    ParaphraseGroup("import_kitchen", Truth(IMPORT, product="เครื่องครัว", quantity=30, unit="ชุด", method="road"), [
        "สนใจสั่งเครื่องครัวจากจีน 30 ชุด ทางรถ", "อยากนำเข้าเครื่องครัว 30 ชุด ส่งทางรถ", "จะนำเข้าเครื่องครัว 30 ชุด ส่งรถ",
        "อยากสั่งเครื่องครัวจากจีน 30 ชุด ทางรถครับ", "ต้องการนำเข้าเครื่องครัว 30 ชุด ส่งทางรถค่ะ", "ฝากสั่งเครื่องครัว 30 ชุด ส่งรถ",
        "สนใจนำเข้าเครื่องครัวจากจีน 30 ชุด ทางรถ", "อยากได้เครื่องครัว 30 ชุด ส่งทางรถ"]),
    ParaphraseGroup("policy_supplement", Truth(POLICY, product="อาหารเสริม", auth=False), [
        "อาหารเสริมนำเข้าได้ไหม", "อาหารเสริมนำเข้าได้หรือเปล่า", "นำเข้าอาหารเสริมได้ไหม", "อาหารเสริมสั่งจากจีนได้ไหม",
        "อาหารเสริมนำเข้าได้มั้ย", "อาหารเสริมส่งเข้าไทยได้ไหม", "อาหารเสริมนำเข้าได้หรอ", "อาหารเสริมนำเข้าได้ป่ะคะ"]),
    ParaphraseGroup("method_answer_air", Truth(IMPORT, method="air", check_product=False, check_quantity=False, history="ASK_METHOD"), [
        "ทางเครื่องบินครับ", "ทางอากาศ", "เครื่องบิน", "เอาทางเครื่องบิน", "ส่งทางอากาศค่ะ", "ทางเครื่องบิน",
        "ส่งเครื่องบินได้ไหม", "ขอทางอากาศครับ"]),
    ParaphraseGroup("import_shirt_answer", Truth(IMPORT, product="เสื้อยืด", check_quantity=False, history="ASK_PRODUCT"), [
        "เสื้อยืดครับ", "เป็นเสื้อยืดค่ะ", "เอาเสื้อยืด", "สินค้าเป็นเสื้อยืด", "เสื้อยืด", "อยากสั่งเสื้อยืด",
        "เป็นพวกเสื้อยืดครับ", "เสื้อยืดค่ะ"]),
]


# ── multi-turn journeys ──────────────────────────────────────────────
@dataclass
class Turn:
    text: str
    expect: Dict[str, Any] = field(default_factory=dict)
    noisy: bool = False


@dataclass
class Journey:
    name: str
    turns: List[Turn]


_PRODUCTS = [("รองเท้า", "คู่"), ("กระเป๋า", "ใบ"), ("เสื้อยืด", "ตัว"), ("ชั้นวางของ", "ชิ้น"),
             ("กล่องพลาสติก", "ลัง"), ("โคมไฟ", "ชิ้น"), ("หมวก", "ใบ"), ("ของเล่น", "ชิ้น"),
             ("เครื่องครัว", "ชุด"), ("ขวดน้ำ", "ขวด")]
_OPENERS = ["{q} {u}อยากสั่งของจากจีน", "อยากสั่งของจากจีน {q} {u}", "สนใจนำเข้า {q} {u}",
            "อยากนำเข้าของจากจีน {q} {u}", "ฝากสั่งของจีน {q} {u}ครับ", "{q}{u}อยากสั่งของจากจีน"]
_PRODUCT_ANSWERS = ["{p}ครับ", "เป็น{p}ค่ะ", "เอา{p}", "สินค้าเป็น{p}", "{p}", "{p} ครับ"]
_METHOD_ANSWERS = {"sea": ["ทางเรือครับ", "ส่งเรือ", "ส่งเรือได้ไหม", "เอาทางเรือ", "ทางเรือค่ะ"],
                   "road": ["ทางรถครับ", "ส่งรถ", "ส่งรถได้ไหม", "เอาทางรถ", "ทางรถค่ะ"]}
_PRICE_QS = ["แล้วราคาเท่าไหร่", "ค่าส่งเท่าไหร่", "แล้วราคาเท่าไหร่อะ", "ค่าส่งกิโลละเท่าไหร่"]
_CORRECTIONS = ["เอ้ย {q2} {u}", "เปลี่ยนเป็น {q2} {u}", "แก้เป็น {q2} {u}ครับ"]
_POLICY_KNOWN = ["ของแบบนี้นำเข้าได้ไหม", "ของแบบนี้นำเข้าได้หรอ", "อันนี้นำเข้าได้ไหม"]
_POLICY_OTHER = [("กระต่าย", "กระต่ายนำเข้าได้ไหม"), ("เครื่องสำอาง", "เครื่องสำอางนำเข้าได้หรือเปล่า"),
                 ("แบตเตอรี่", "แบตเตอรี่นำเข้าได้ไหม"), ("อาหารเสริม", "อาหารเสริมนำเข้าได้ไหมคะ")]
_CONTACT = ["งั้นขอเบอร์ติดต่อ", "ขอเบอร์ติดต่อหน่อย", "ขอเบอร์โทรหน่อยครับ"]
_PRIVATE = ["ขอเช็คออเดอร์ของผมหน่อย", "ของผมถึงไทยหรือยัง", "พัสดุของฉันถึงไหนแล้ว"]

# noise operators safe to apply to a JOURNEY turn (they never touch the
# product noun itself, only vocabulary words / particles / spacing).
_JOURNEY_NOISE = [op_particle, op_add_particle, op_swap_tone, op_drop_mark, op_elongate,
                  op_extra_spaces, op_remove_spaces, op_zero_width, op_double_final]


def _noise(text: str, rnd: random.Random, protect: Sequence[str] = ()) -> Tuple[str, bool]:
    """Apply one journey-safe operator; never inside a protected noun."""
    for op in rnd.sample(_JOURNEY_NOISE, k=len(_JOURNEY_NOISE)):
        got = op(text, rnd)
        if got and got != text and all(p in got for p in protect):
            return got, True
    return text, False


def journeys(seed: int = 11, count: int = 100) -> List[Journey]:
    rnd = random.Random(seed)
    out: List[Journey] = []
    for i in range(count):
        p, u = _PRODUCTS[i % len(_PRODUCTS)]
        q = rnd.choice([5, 10, 20, 30, 50, 100, 200])
        q2 = rnd.choice([n for n in (3, 8, 10, 15, 25, 40, 60) if n != q])
        method = rnd.choice(["sea", "road"])
        shape = i % 5
        turns: List[Turn] = []

        opener = rnd.choice(_OPENERS).format(q=q, u=u)
        turns.append(Turn(opener, {"quantity": (q, u), "product": None, "action": "ASK_PRODUCT"}))
        turns.append(Turn(rnd.choice(_PRODUCT_ANSWERS).format(p=p),
                          {"quantity": (q, u), "product": p, "not_action": ["ASK_PRODUCT", "ASK_QUANTITY"]}))
        if shape in (0, 1, 2, 4):
            turns.append(Turn(rnd.choice(_METHOD_ANSWERS[method]),
                              {"quantity": (q, u), "product": p, "method": method,
                               "not_action": ["ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"]}))
        if shape in (0, 3):
            turns.append(Turn(rnd.choice(_PRICE_QS),
                              {"quantity": (q, u), "product": p, "not_action": ["ASK_PRODUCT", "ASK_QUANTITY"]}))
        if shape in (0, 1, 4):
            turns.append(Turn(rnd.choice(_CORRECTIONS).format(q2=q2, u=u),
                              {"quantity": (q2, u), "product": p, "not_action": ["ASK_PRODUCT", "ASK_QUANTITY"]}))
        if shape in (0, 2):
            turns.append(Turn(rnd.choice(_POLICY_KNOWN),
                              {"product": p, "intent": POLICY, "not_action": ["ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"],
                               "auth": False}))
        if shape in (1, 3):
            other, q_text = rnd.choice(_POLICY_OTHER)
            turns.append(Turn(q_text, {"intent": POLICY, "entity_product": other, "auth": False,
                                       "not_action": ["ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"]}))
        if shape in (0, 2, 4):
            turns.append(Turn(rnd.choice(_CONTACT), {"intent": CONTACT, "auth": False,
                                                     "not_action": ["ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"]}))
        if shape in (3, 4):
            turns.append(Turn(rnd.choice(_PRIVATE), {"auth": True, "action": "ASK_IDENTIFIER",
                                                     "not_action": ["ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"]}))
        # noise on roughly half the turns, never inside the product noun
        for t in turns:
            if rnd.random() < 0.55:
                t.text, t.noisy = _noise(t.text, rnd, protect=[p] if p in t.text else [])
        out.append(Journey(f"J{i + 1:03d}-{p}-{method}-s{shape}", turns))
    return out
