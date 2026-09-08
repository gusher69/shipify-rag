# -*- coding: utf-8 -*-
"""PHASE-6D typo corpus — clean base cases + generated minor-typo variants.

Base sentences are deliberately plain, natural Thai. `build_corpus()`
returns:
  - the clean base cases (CLEAN INPUT regression set)
  - >= 150 single-turn typo cases (>= 4 variants per base)
  - >= 100 multi-turn typo cases (typo injected on selected turns of a
    journey; earlier turns clean so context is real)

No base sentence is used verbatim as a production rule anywhere.
"""
from __future__ import annotations

from typing import Dict, List

import zlib
from tests.phase6d_typo.typo_gen import variants


def _seed(x: str) -> int:
    return zlib.crc32(x.encode("utf-8")) & 0xFFFF

# family -> the intent families that count as "same behaviour" for a typo.
# A typo must not move the turn OUT of this set.
_SINGLE: List[Dict] = [
    # calculator payload / verb
    {"id": "calc-verb-1", "text": "ช่วยคำนวณค่าส่งให้หน่อยครับ", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "calc-verb-2", "text": "อยากคำนวณค่าขนส่งกล่องนี้", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "calc-verb-3", "text": "ขอประเมินค่าส่งหน่อยค่ะ", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "calc-dims-1", "text": "กล่องขนาด 40 x 30 x 20 หนัก 3 กิโล ส่งทางเรือ", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "calc-dims-2", "text": "พัสดุ 50x40x30 น้ำหนัก 5 กิโลกรัม", "family": ["SHIPPING_ESTIMATE"]},
    # shipping-cost discovery (evaluative)
    {"id": "cost-disc-1", "text": "ค่าส่งแพงไหมถ้าสั่งชั้นวางของ", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "cost-disc-2", "text": "ส่งของหนักๆ ค่าขนส่งแรงมั้ย", "family": ["SHIPPING_ESTIMATE"]},
    {"id": "cost-disc-3", "text": "ค่าส่งเฟอร์นิเจอร์เยอะไหมคะ", "family": ["SHIPPING_ESTIMATE"]},
    # withdrawal
    {"id": "wd-ship-1", "text": "ถอนเงินขนส่งยังไงคะ", "family": ["SHIPPING_WITHDRAWAL"]},
    {"id": "wd-ship-2", "text": "อยากถอนเครดิตค่าขนส่ง", "family": ["SHIPPING_WITHDRAWAL"]},
    {"id": "wd-buy-1", "text": "ถอนเงินค่าสั่งซื้อทำยังไง", "family": ["PURCHASE_WITHDRAWAL"]},
    # contact
    {"id": "contact-1", "text": "ขอเบอร์ติดต่อหน่อยค่ะ", "family": ["CONTACT_INFO"]},
    {"id": "contact-2", "text": "ขอไอดีไลน์ติดต่อหน่อย", "family": ["CONTACT_INFO"]},
    {"id": "contact-3", "text": "อยากติดต่อแอดมินทำยังไง", "family": ["CONTACT_INFO"]},
    # link conversion
    {"id": "link-1", "text": "ช่วยแปลงลิงก์ให้หน่อย", "family": ["LINK_CONVERSION"]},
    {"id": "link-2", "text": "แปลงลิงก์สินค้า Taobao ได้ไหม", "family": ["LINK_CONVERSION"]},
    {"id": "link-3", "text": "เอาลิงก์นี้ไปแปลงเป็นภาษาไทย", "family": ["LINK_CONVERSION"]},
    # import interest
    {"id": "imp-1", "text": "อยากนำเข้าเครื่องจักรจากจีน", "family": ["IMPORT_INTEREST"]},
    {"id": "imp-2", "text": "สนใจนำเข้าเสื้อผ้ามาขาย", "family": ["IMPORT_INTEREST", "SERVICE_DISCOVERY"]},
    {"id": "imp-3", "text": "อยากสั่งของจากจีนมาขายต่อ", "family": ["IMPORT_INTEREST", "SERVICE_DISCOVERY", "GENERAL", "UNKNOWN"]},
    # service discovery / help
    {"id": "disc-1", "text": "ที่นี่มีบริการอะไรบ้าง", "family": ["SERVICE_DISCOVERY"]},
    {"id": "disc-2", "text": "ช่วยอะไรได้บ้างคะ", "family": ["SERVICE_DISCOVERY", "HELP_INTENT"]},
    {"id": "help-1", "text": "ขอคำแนะนำหน่อยค่ะ", "family": ["HELP_INTENT", "SERVICE_DISCOVERY"]},
    # warehouse inbound
    {"id": "wh-1", "text": "ของถึงโกดังจีนแล้วต้องแจ้งใคร", "family": ["WAREHOUSE_INBOUND_JOURNEY"]},
    {"id": "wh-2", "text": "ที่อยู่โกดังจีนขอหน่อย",
     "family": ["PICKUP_LOCATION", "WAREHOUSE_INBOUND_JOURNEY", "CONTACT_INFO", "GENERAL", "UNKNOWN"]},
    # invoice
    {"id": "inv-1", "text": "ขอใบกำกับภาษีได้ไหมคะ", "family": ["INVOICE"]},
    {"id": "inv-2", "text": "โหลดใบกำกับภาษียังไง", "family": ["INVOICE"]},
    # tracking (private)
    {"id": "trk-1", "text": "เช็คสถานะพัสดุของผมหน่อย", "family": ["SHIPMENT_STATUS"]},
    {"id": "trk-2", "text": "ของที่สั่งไปถึงไหนแล้ว", "family": ["SHIPMENT_STATUS"]},
    # coupon
    {"id": "cpn-1", "text": "ใช้คูปองส่วนลดยังไง", "family": ["COUPON_USAGE"]},
    {"id": "cpn-2", "text": "คูปองของฉันมีอันไหนใช้ได้บ้าง", "family": ["MY_COUPONS", "COUPON_USAGE"]},
    # cancellation
    {"id": "cxl-1", "text": "ขอยกเลิกคำสั่งซื้อได้ไหม", "family": ["PRODUCT_POLICY", "GENERAL", "UNKNOWN"]},
    # website links
    {"id": "web-1", "text": "ขอลิงก์เว็บ Taobao กับ 1688", "family": ["WEBSITE_LINK_REQUEST"]},
    {"id": "web-2", "text": "เข้าเว็บ 1688 ยังไง", "family": ["WEBSITE_LINK_REQUEST"]},
    # general assistance (pure how-to)
    {"id": "ga-1", "text": "ของแตกง่ายควรแพ็กยังไงดี", "family": ["PRODUCT_POLICY", "GENERAL", "UNKNOWN"]},
    {"id": "ga-2", "text": "จานเซรามิกห่อแบบไหนถึงจะปลอดภัย", "family": ["PRODUCT_POLICY", "GENERAL", "UNKNOWN"]},
    # mixed general + business
    {"id": "mix-1", "text": "ของแก้วแพ็กยังไง แล้วบริษัทคิดค่าห่อเพิ่มไหม", "family": ["PRODUCT_POLICY", "GENERAL", "UNKNOWN"]},
    # public/private boundary
    {"id": "pp-1", "text": "ค่าส่งทางเรือกิโลละเท่าไหร่", "family": ["SHIPPING_ESTIMATE", "PRODUCT_POLICY", "GENERAL", "UNKNOWN"]},
]

# multi-turn journeys — earlier turns CLEAN, typo injected on the marked
# turn(s). `expect` is checked on the LAST turn.
_JOURNEYS: List[Dict] = [
    {"id": "j-wd-brand", "turns": [
        {"user": "ถอนเงินขนส่งยังไง", "typo": False},
        {"user": "SP1008", "typo": False},
        {"user": "เป็นแบรนด์ FT ค่ะ", "typo": True}],
     "expect": {"intent_family": ["SHIPPING_WITHDRAWAL", "GENERAL", "UNKNOWN"],
                "no_structured_change": ["SP1008"]}},
    {"id": "j-calc-weight", "turns": [
        {"user": "ช่วยคำนวณค่าส่งหน่อย", "typo": True},
        {"user": "กล่อง 40x30x20 ทางเรือ", "typo": False},
        {"user": "หนัก 5 กิโล", "typo": True}],
     "expect": {"intent_family": ["SHIPPING_ESTIMATE", "GENERAL", "UNKNOWN"],
                "no_structured_change": ["40x30x20", "5"]}},
    {"id": "j-link-followup", "turns": [
        {"user": "ช่วยแปลงลิงก์", "typo": True},
        {"user": "https://item.taobao.com/item.htm?id=99887766", "typo": False}],
     "expect": {"intent_family": ["LINK_CONVERSION"],
                "no_structured_change": ["https://item.taobao.com/item.htm?id=99887766"]}},
    {"id": "j-contact-then-web", "turns": [
        {"user": "ขอเบอร์ติดต่อ", "typo": True},
        {"user": "แล้วขอลิงก์เว็บ 1688 ด้วย", "typo": True}],
     "expect": {"intent_family": ["WEBSITE_LINK_REQUEST", "CONTACT_INFO"]}},
    {"id": "j-topic-switch", "turns": [
        {"user": "ขอแปลงลิงก์", "typo": False},
        {"user": "ไม่เอาแล้ว ขอถามค่าตีลังไม้แทน", "typo": True}],
     "expect": {"intent_family": ["SHIPPING_ESTIMATE", "GENERAL", "UNKNOWN", "PRODUCT_POLICY"]}},
    {"id": "j-import-discovery", "turns": [
        {"user": "สนใจนำเข้าสินค้าจากจีน", "typo": True},
        {"user": "เป็นพวกเครื่องมือช่าง", "typo": True}],
     "expect": {"intent_family": ["IMPORT_INTEREST", "SERVICE_DISCOVERY", "PRODUCT_POLICY", "GENERAL", "UNKNOWN"]}},
    {"id": "j-cost-then-dims", "turns": [
        {"user": "ค่าส่งแพงไหมถ้าสั่งโต๊ะ", "typo": True},
        {"user": "โต๊ะพับ 120x60x75 หนัก 12 กิโล", "typo": False}],
     "expect": {"intent_family": ["SHIPPING_ESTIMATE", "GENERAL", "UNKNOWN"],
                "no_structured_change": ["120x60x75", "12"]}},
    {"id": "j-help-affirm", "turns": [
        {"user": "สวัสดีครับ", "typo": False},
        {"user": "ขอสอบถามหน่อยครับ", "typo": True}],
     "expect": {"intent_family": ["HELP_INTENT", "SERVICE_DISCOVERY", "GENERAL", "UNKNOWN"]}},
]


def _clean_family(text: str) -> str:
    """The intent family the CLEAN sentence resolves to today — a typo
    variant that lands on the same family has not regressed, even where
    the clean sentence itself is an intent-coverage gap."""
    try:
        from services.conversation_semantics import interpret
        return interpret(text, []).intent_family
    except Exception:
        return "UNKNOWN"


def build_corpus(*, variants_per_base: int = 5) -> Dict:
    clean: List[Dict] = []
    single: List[Dict] = []
    for base in _SINGLE:
        cf = _clean_family(base["text"])
        clean.append({"case_id": base["id"] + "-clean", "raw_input": base["text"],
                      "history": [], "expected_family": base["family"],
                      "clean_family": cf, "kind": "clean"})
        for k, (tname, mut) in enumerate(variants(base["text"], seed=_seed(base["id"]),
                                                  n=variants_per_base)):
            single.append({"case_id": f"{base['id']}-{tname}-{k}", "raw_input": mut,
                           "history": [], "expected_family": base["family"],
                           "clean_family": cf,
                           "typo_class": tname, "base_id": base["id"], "kind": "typo"})

    multi: List[Dict] = []
    for jj in _JOURNEYS:
        # produce a few variants of the whole journey, each re-typo-ing the
        # marked turns with a different seed
        for vi in range(14):
            turns_out = []
            changed_any = False
            for ti, t in enumerate(jj["turns"]):
                if t.get("typo"):
                    vs = variants(t["user"], seed=_seed(jj["id"]) ^ (vi * 131 + ti), n=3)
                    if vs:
                        turns_out.append(vs[vi % len(vs)][1])
                        changed_any = True
                        continue
                turns_out.append(t["user"])
            if not changed_any:
                continue
            multi.append({"case_id": f"{jj['id']}-v{vi}", "turns": turns_out,
                          "expect": jj["expect"], "base_id": jj["id"], "kind": "typo_multi"})

    return {"clean": clean, "single": single, "multi": multi}
