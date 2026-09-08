# -*- coding: utf-8 -*-
"""PHASE-6D — structured-value adversarial suite.

Proves the normalization layer NEVER mutates a structured value, and
never silently "repairs" a malformed identifier into a different valid
one. Pure — runs against services.thai_text_normalizer directly.
"""
from __future__ import annotations

from typing import Dict, List

from services.thai_text_normalizer import normalize_message

# (label, sentence, the exact substrings that must survive byte-for-byte)
_KEEP: List[Dict] = [
    {"id": "custcode", "text": "ลูกค้าโค้ด SP1008 ครับ", "keep": ["SP1008"]},
    {"id": "ft-id", "text": "แบรนด์ FT1325 ค่ะะ", "keep": ["FT1325"]},
    {"id": "po", "text": "ออเดอร์ PO12345 ยังไม่มาา", "keep": ["PO12345"]},
    {"id": "pa", "text": "เลข PA889900 ตรวจให้หน่อยยย", "keep": ["PA889900"]},
    {"id": "shipment", "text": "พัสดุ FT318220260726001 ถึงไหนแล้ววว", "keep": ["FT318220260726001"]},
    {"id": "cn-track", "text": "แทรค CN123456789TH ยังไม่ขยับ", "keep": ["CN123456789TH"]},
    {"id": "url-taobao", "text": "แปลงลิ้งนี้ https://item.taobao.com/item.htm?id=99887766 ให้หน่อย",
     "keep": ["https://item.taobao.com/item.htm?id=99887766"]},
    {"id": "url-1688-mobile", "text": "ลิ้ง https://m.1688.com/offer/620304050.html ใช่มั้ย",
     "keep": ["https://m.1688.com/offer/620304050.html"]},
    {"id": "phone", "text": "เบอผม 0899999999 โทรกลับด้วน", "keep": ["0899999999"]},
    {"id": "phone-dash", "text": "ติดต่อ 02-026-6426 ได้ป่าวว", "keep": ["02-026-6426"]},
    {"id": "email", "text": "ส่งเมลไป support@shipify.co.th นะะ", "keep": ["support@shipify.co.th"]},
    {"id": "amount", "text": "ยอด 12,345.67 บาท ถูกมั้ย", "keep": ["12,345.67"]},
    {"id": "amount2", "text": "โอนไป 2,500 บาทท", "keep": ["2,500"]},
    {"id": "dims-mm", "text": "กล่อง 520mm x 220mm x 110mm หนักkกก 2 กิโล",
     "keep": ["520mm", "220mm", "110mm"]},
    {"id": "dims-plain", "text": "ขนาดด 50x40x30 ส่งทางเรือ", "keep": ["50x40x30"]},
    {"id": "dims-cm", "text": "พัสดุ 120 x 60 x 75 ซม.", "keep": ["120", "60", "75"]},
    {"id": "weight-g", "text": "หนัก 5000 กรัมม", "keep": ["5000"]},
    {"id": "percent", "text": "ลด 15% ใช่มั้ยยย", "keep": ["15%"]},
    {"id": "date", "text": "สั่งวันที่ 2026-09-08 ยังไม่ได้ของ", "keep": ["2026-09-08"]},
    {"id": "mixed", "text": "บิล FT1325 ยอด 2,500 บาท ลิ้ง https://x.1688.com/a.html",
     "keep": ["FT1325", "2,500", "https://x.1688.com/a.html"]},
]

# malformed / ambiguous identifiers — normalization must NOT turn them
# into a different, well-formed identifier. It may leave them as-is; the
# routing layer then asks for the exact value.
_MALFORMED: List[Dict] = [
    {"id": "ft-with-letter", "text": "เลข FT132S ครับ", "must_not_become": ["FT1325", "FT1328"]},
    {"id": "po-with-letter", "text": "PO1234S ตรวจให้ที", "must_not_become": ["PO12345", "PO12348"]},
    {"id": "zero-oh", "text": "โค้ด SPO1O8", "must_not_become": ["SP0108", "SP1008"]},
    {"id": "one-ell", "text": "บิล FTl325", "must_not_become": ["FT1325"]},
]


def run_adversarial() -> Dict:
    results: List[Dict] = []
    corrupt = 0
    for c in _KEEP:
        nm = normalize_message(c["text"])
        missing = [k for k in c["keep"] if k not in nm.normalized]
        ok = not missing
        if not ok:
            corrupt += 1
        results.append({"case_id": "adv-keep-" + c["id"], "raw_input": c["text"],
                        "normalized_input": nm.normalized, "protected_tokens": nm.protected_tokens,
                        "status": "PASS" if ok else "FAIL_STRUCTURED_VALUE_CHANGED",
                        "reason": "" if ok else f"missing after normalize: {missing}",
                        "structured_value_changed": not ok})
    for c in _MALFORMED:
        nm = normalize_message(c["text"])
        became = [b for b in c["must_not_become"] if b in nm.normalized]
        ok = not became
        if not ok:
            corrupt += 1
        results.append({"case_id": "adv-malformed-" + c["id"], "raw_input": c["text"],
                        "normalized_input": nm.normalized, "protected_tokens": nm.protected_tokens,
                        "status": "PASS" if ok else "FAIL_SILENT_IDENTIFIER_REPAIR",
                        "reason": "" if ok else f"malformed id silently repaired to {became}",
                        "structured_value_changed": not ok})
    return {"total": len(results), "structured_value_corruption": corrupt, "cases": results}
