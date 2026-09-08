# -*- coding: utf-8 -*-
"""PHASE-6E response-style corpus.

Each case is (id, family, turns, style_expect). `style_expect` carries
only STYLE constraints — the routing / source / truth constraints are
inherited from the already-accepted PHASE-6C/6D behaviour and checked by
the delta regression, never re-litigated here.

style_expect keys:
  needs_next_question   the reply should end with a question / next step
  no_repeat_slot        [values already supplied] must not be re-asked
  no_greeting_repeat    reply must not open with a fresh greeting
  honest_no_info        an unavailable business fact -> a natural no-info line
  general_knowhow       a pure how-to -> a real answer, not a deflection
  keep_open             a KB miss must not dead-end the conversation
"""
from __future__ import annotations
from typing import Dict, List

_T = lambda role, content: {"role": role, "content": content}

CASES: List[Dict] = []


def _c(cid, family, turns, **exp):
    CASES.append({"id": cid, "family": family, "turns": turns, "style_expect": exp})


# ── greeting / help ───────────────────────────────────────────────────
_c("greet-1", "greeting_help", ["สวัสดีค่ะ"], no_greeting_repeat=False, needs_next_question=True)
_c("help-1", "greeting_help", ["ขอสอบถามหน่อยค่ะ"], needs_next_question=True)
_c("help-2", "greeting_help", ["มีอะไรให้ช่วยไหมคะ"], needs_next_question=True)
_c("help-affirm", "greeting_help",
   [_T("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ"), "ครับ"], needs_next_question=True, no_greeting_repeat=True)

# ── service discovery / next-best-action ──────────────────────────────
_c("disc-1", "service_discovery", ["ที่นี่มีบริการอะไรบ้าง"], needs_next_question=True)
_c("disc-2", "service_discovery", ["สนใจใช้บริการนำเข้า"], needs_next_question=True)
_c("nba-import-1", "service_discovery", ["อยากนำเข้าเครื่องจักร"], needs_next_question=True,
   no_private_identifier_ask=True)
_c("nba-import-2", "service_discovery", ["อยากสั่งของจากจีนมาขาย"], needs_next_question=True)

# ── calculator discovery + slot flow ─────────────────────────────────
_c("calc-disc-1", "calculator", ["ค่าส่งแพงไหมถ้าสั่งชั้นวางของ"], needs_next_question=True)
_c("calc-slot-1", "calculator", ["ช่วยคำนวณค่าส่งให้หน่อย"], needs_next_question=True)
_c("calc-slot-weight-given", "calculator",
   ["ช่วยคำนวณค่าส่ง", "40x30x20 ทางเรือ", "หนัก 5 กิโล"],
   no_repeat_slot=["5", "40x30x20", "ทางเรือ"])
_c("calc-slot-method-left", "calculator",
   ["ช่วยคำนวณค่าส่ง", "กล่อง 40x30x20 หนัก 5 กิโล"],
   needs_next_question=True, no_repeat_slot=["5", "40x30x20"])

# ── general assistance ───────────────────────────────────────────────
_c("ga-1", "general_assistance", ["ของแตกง่ายควรแพ็กยังไงดี"], general_knowhow=True)
_c("ga-2", "general_assistance", ["จานเซรามิกส่งไกลๆ ห่อแบบไหนถึงจะปลอดภัย"], general_knowhow=True)
_c("ga-3", "general_assistance", ["พวกของบอบบางแพ็กกันกระแทกยังไง"], general_knowhow=True)
_c("ga-4", "general_assistance", ["ของหนักๆ ควรใส่กล่องแบบไหน"], general_knowhow=True)

# ── mixed general + business ─────────────────────────────────────────
_c("mix-1", "mixed", ["ของแก้วควรแพ็กยังไง แล้ว Shipify คิดค่าห่อเพิ่มไหม"],
   general_knowhow=True, honest_no_info=True)
_c("mix-2", "mixed", ["กระเบื้องแตกง่ายไหม แล้วบริษัทมีตีลังไม้ให้หรือเปล่า"], general_knowhow=True)

# ── unavailable business fact ───────────────────────────────────────
_c("nofact-1", "business_unavailable", ["Shipify มีนโยบายรีไซเคิลกล่องยังไง"],
   honest_no_info=True, no_fake_handoff=True, keep_open=True)
_c("nofact-2", "business_unavailable", ["ค่าห่อกันกระแทกกี่บาท"],
   honest_no_info=True, no_fake_handoff=True)
_c("nofact-3", "business_unavailable", ["บริษัทรับประกันของเสียหายระหว่างขนส่งไหม"],
   honest_no_info=True, no_fake_handoff=True)

# ── follow-up / short replies ───────────────────────────────────────
_c("fu-ok", "followup", [_T("assistant", "ทางเรือคิด 19 บาท/กิโลกรัม หรือ 4,500 บาท/CBM ค่ะ"), "โอเคค่ะ"],
   no_greeting_repeat=True)
_c("fu-what-next", "followup",
   [_T("assistant", "รับทราบค่ะ (ขนาด 40x30x20) ต้องการประเมินทางรถหรือทางเรือคะ"), "แล้วไงต่อ"],
   no_greeting_repeat=True)
_c("fu-sea", "followup",
   [_T("assistant", "ทางรถคิด 35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM ค่ะ"), "แล้วทางเรือล่ะ"],
   no_greeting_repeat=True)

# ── correction / rejection / topic switch ──────────────────────────
_c("corr-brand", "correction",
   [_T("assistant", "รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ"), _T("user", "SP1008"),
    _T("assistant", "สำหรับแบรนด์ SP ต้องเตรียมแบบฟอร์มรูปภาพและสำเนาบัตรประชาชนค่ะ"), "เอฟทีค่ะ"],
   no_greeting_repeat=True)
_c("rej-1", "rejection",
   [_T("assistant", "ลิงก์เว็บไซต์หลักของแต่ละแพลตฟอร์มค่ะ • Taobao: https://www.taobao.com"),
    "ไม่ใช่ค่ะ หมายถึงลิงก์ที่จะเข้าไปดูของ"], no_greeting_repeat=True)
_c("topic-switch", "topic_switch",
   [_T("user", "ขอแปลงลิงก์"), _T("assistant", "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"),
    "ไม่เอาแล้ว ขอถามค่าตีลังไม้แทน"], no_greeting_repeat=True)

# ── typo input (style must still be natural) ───────────────────────
_c("typo-calc", "typo", ["คำนวนค่าส่งให้หนอย"], needs_next_question=True)
_c("typo-contact", "typo", ["ขอเบอติดต่อ"])
_c("typo-link", "typo", ["ช่วยแปลงลิ้ง"])

# ── private-data request (must gate, must not leak, must be polite) ─
_c("priv-1", "private", ["เช็คสถานะพัสดุของผมหน่อย"], expect_polite_gate=True, no_private_leak=True)
_c("priv-2", "private", ["บิลล่าสุดของผมจ่ายไปหรือยัง"], expect_polite_gate=True, no_private_leak=True)

# ── link conversion ───────────────────────────────────────────────
_c("link-1", "link_conversion", ["ช่วยแปลงลิงก์ให้หน่อย"], needs_next_question=True)
_c("link-2", "link_conversion", ["แปลงลิงก์นี้ https://item.taobao.com/item.htm?id=12345678"])

# ── withdrawal ────────────────────────────────────────────────────
_c("wd-1", "withdrawal", ["ถอนเงินขนส่งยังไงคะ"], needs_next_question=True)
_c("wd-2", "withdrawal",
   [_T("assistant", "รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ"), "FT ค่ะ"], no_greeting_repeat=True)

# ── invoice / warehouse / tracking ───────────────────────────────
_c("inv-1", "invoice", ["ขอใบกำกับภาษีได้ไหมคะ"])
_c("wh-1", "warehouse", ["ให้โรงงานส่งของไปโกดังจีนของคุณต้องทำยังไง"], keep_open=True)
_c("trk-1", "tracking", ["ของที่สั่งไปถึงไหนแล้ว"], expect_polite_gate=True, no_private_leak=True)

# ── contact info ────────────────────────────────────────────────
_c("contact-1", "contact", ["ขอเบอร์ติดต่อหน่อยค่ะ"])
_c("contact-2", "contact", ["มีไลน์บริษัทไหมคะ"])


def _paraphrase(base_id, family, texts, **exp):
    for i, t in enumerate(texts):
        _c(f"{base_id}-p{i}", family, [t], **exp)


# broaden to >= 100
_paraphrase("ga-more", "general_assistance", [
    "ส่งของมีคมควรแพ็กยังไง", "ขวดแก้วหลายใบแพ็กรวมกันได้ไหม", "ของอิเล็กทรอนิกส์ต้องกันชื้นไหม",
    "กระเป๋าหนังส่งทางเรือจะเสียหายไหม", "ของชิ้นใหญ่แต่เบา คิดค่าส่งยังไงโดยทั่วไป",
    "พวกเฟอร์นิเจอร์ไม้ต้องตีลังไหม", "ผ้าเป็นม้วนพันกันกระแทกยังไง", "ของเหลวส่งได้ไหมโดยทั่วไป",
], general_knowhow=True)
_paraphrase("nofact-more", "business_unavailable", [
    "Shipify คิดค่าเก็บของที่โกดังวันละเท่าไหร่", "มีประกันพัสดุสูญหายไหม ราคาเท่าไหร่",
    "ค่าธรรมเนียมโอนเงินไปจีนกี่เปอร์เซ็นต์", "ส่งด่วนพิเศษมีไหม กี่วันถึง",
    "คิดค่าแพ็กใส่ลังไม้ใบละเท่าไหร่",
], honest_no_info=True, no_fake_handoff=True, keep_open=True)
_paraphrase("disc-more", "service_discovery", [
    "อยากนำเข้าเสื้อผ้ามาขายเริ่มยังไง", "รับส่งของจากจีนมาไทยไหม", "มีบริการฝากสั่งของไหมคะ",
    "อยากเริ่มนำเข้าของจากจีน ต้องทำอะไรบ้าง", "สนใจส่งของทางเรือ มีขั้นต่ำไหม",
], needs_next_question=True)
_paraphrase("calc-more", "calculator", [
    "อยากรู้ค่าส่งกล่องนึง", "ประเมินค่าขนส่งให้หน่อย", "ส่งของ 10 กิโลไปไทยกี่บาท",
    "คิดค่าส่งทางเรือให้หน่อยค่ะ", "ค่าส่งโต๊ะทำงานประมาณเท่าไหร่",
], needs_next_question=True)
_paraphrase("fu-more", "followup", [
    "อันนี้ล่ะ", "แบบนี้ได้ไหม", "โอเค", "ได้ค่ะ", "แล้วอันไหนถูกกว่า",
], no_greeting_repeat=True)
_paraphrase("contact-more", "contact", [
    "ขอไลน์แอดมิน", "อีเมลบริษัทคืออะไร", "โทรหาได้ที่เบอร์ไหน", "เพจเฟซบุ๊กชื่ออะไร",
])
_paraphrase("link-more", "link_conversion", [
    "แปลงลิงก์ Taobao ให้หน่อย", "เอาลิงก์นี้ไปเปิดบิล", "ช่วยเปลี่ยนลิงก์เป็นภาษาไทย",
    "ส่งลิงก์ 1688 มาแปลงได้ไหม",
])
_paraphrase("wd-more", "withdrawal", [
    "อยากถอนเครดิตค่าขนส่ง", "ถอนเงินค่าสั่งซื้อทำยังไง", "เงินในระบบถอนออกมายังไง",
])
_paraphrase("web-more", "website_link", [
    "ขอลิงก์เว็บ 1688", "เข้าเว็บ Taobao ยังไง", "เว็บ Tmall คือลิงก์อะไร",
])
_paraphrase("invwh-more", "invoice_warehouse", [
    "โหลดใบกำกับภาษียังไง", "โกดังอ่อนนุชเปิดกี่โมง", "ที่อยู่โกดังจีนขอหน่อย",
])
_paraphrase("greet-more", "greeting_help", [
    "หวัดดีค่ะ", "สวัสดีตอนเช้าครับ", "ทักมาสอบถามหน่อยค่ะ",
], needs_next_question=True)
_paraphrase("ga-more2", "general_assistance", [
    "รูปภาพติดกรอบส่งยังไงไม่ให้แตก", "ของที่กลัวความร้อนควรส่งทางไหน",
    "แพ็กของให้ประหยัดพื้นที่ทำยังไง", "กล่องกระดาษกับลังไม้ต่างกันยังไง",
    "ส่งอาหารแห้งไปต่างประเทศได้ไหมโดยทั่วไป", "ของมือสองแพ็กยังไงให้ดูเรียบร้อย",
], general_knowhow=True)
_paraphrase("fu-more2", "followup", [
    "เปลี่ยนเป็น FT", "ไม่ใช่", "ไม่เอาแล้ว", "อันนี้ได้ไหม", "แล้วถ้าหนักกว่านี้ล่ะ",
], no_greeting_repeat=True)
_paraphrase("nofact-more2", "business_unavailable", [
    "มีโปรลดค่าส่งช่วงนี้ไหม", "ค่าปรับถ้ายกเลิกออเดอร์เท่าไหร่",
    "ส่งของไปประเทศอื่นนอกจากไทยได้ไหม",
], honest_no_info=True, no_fake_handoff=True, keep_open=True)
