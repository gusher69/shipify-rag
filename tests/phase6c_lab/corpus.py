# -*- coding: utf-8 -*-
"""PHASE-6C — exploratory conversation corpus (>=150 NEW cases, >=50
multi-turn). None are copied verbatim from the existing UAT / Part-2
sentences; every high-risk behaviour family has >=5 paraphrase variants
(short Thai, typos, spoken Thai, polite/impolite, missing subject,
follow-up-only, correction, rejection, topic switch, ambiguous pronoun,
repeated question, long history).

A case:
  case_id, family, turns:[str|{"user":str}], context:{...}, expect:{...},
  mode: "deterministic" | "live"

Production logic must NOT hardcode any of these exact sentences.
"""
from __future__ import annotations

from typing import Dict, List

# ── shared history prefixes (realistic long histories) ───────────────
def _t(role, c):
    return {"role": role, "content": c}


LONG_HISTORY_A = [  # ~24 turns: import interest -> link conv -> withdrawal -> misc
    _t("user", "สวัสดีครับ"), _t("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ"),
    _t("user", "อยากนำเข้ากระเป๋าหนังมาขาย"),
    _t("assistant", "รับทราบค่ะ สนใจนำเข้ากระเป๋าหนังนะคะ รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"),
    _t("user", "สัก 300 ใบ"),
    _t("assistant", "รับทราบค่ะ ปรับเป็นจำนวนประมาณ 300 ชิ้นนะคะ สนใจส่งทางรถหรือทางเรือคะ"),
    _t("user", "ทางเรือ"),
    _t("assistant", "รับทราบค่ะ เปลี่ยนเป็นขนส่งทางเรือนะคะ รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"),
    _t("user", "แปลงลิงก์นี้ให้หน่อย https://detail.1688.com/offer/700123456789.html"),
    _t("assistant", "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\nhttps://fasttrade.in.th/PageProductDetailGuest/1688/700123456789/home/guest/index"),
    _t("user", "ขอบคุณครับ"), _t("assistant", "ยินดีค่ะ 😊"),
    _t("user", "ถอนเงินขนส่งทำยังไง"),
    _t("assistant", "เรื่องนี้คำตอบจะแตกต่างกันไปตามแบรนด์ค่ะ รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ"),
    _t("user", "SP1008"),
    _t("assistant", "คุณลูกค้าสามารถกรอกข้อมูลรายละเอียดในแบบฟอร์มรูปภาพที่แอดมินส่งให้ และส่งเอกสารสำเนาบัตรประชาชนมาให้แอดมินได้เลยค่ะ"),
    _t("user", "นำเข้าน้ำหอมได้ไหม"),
    _t("assistant", "น้ำหอมจัดเป็นสินค้าประเภทของเหลว ซึ่งทางเราไม่สามารถนำเข้าสินค้าประเภทของเหลวได้ค่ะ"),
    _t("user", "อ๋อ เข้าใจแล้ว"), _t("assistant", "ค่ะ หากมีคำถามอื่นแจ้งได้เลยนะคะ"),
    _t("user", "คูปองใช้ยังไง"),
    _t("assistant", "คูปองสามารถใช้ลดค่านำเข้าและค่าส่งในไทยได้ค่ะ เข้าที่เมนูสมาชิก เลือกคูปองของฉัน แล้วนำโค้ดไปใส่ตอนชำระเงินค่ะ"),
]

LONG_HISTORY_B = [  # ~30 turns, ends on a shipment-status exchange
    _t("user", "หวัดดีค่ะ"), _t("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ"),
    _t("user", "มีบริการอะไรบ้าง"),
    _t("assistant", "Shipify เป็นผู้ช่วยสั่งซื้อและนำเข้าสินค้าจากจีนแบบครบวงจรค่ะ มีบริการฝากสั่งซื้อ ฝากนำเข้า ฝากโอนเงินให้ร้านค้าจีน\n\nสนใจบริการไหนเป็นพิเศษคะ"),
    _t("user", "อยากรู้ค่าส่งทางเรือ"),
    _t("assistant", "ทางเรือคิด 19 บาท/กิโลกรัม หรือ 4,500 บาท/CBM แล้วใช้ค่าที่สูงกว่าค่ะ"),
    _t("user", "แล้วทางรถล่ะ"),
    _t("assistant", "ทางรถคิด 35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM ค่ะ"),
    _t("user", "โกดังจีนอยู่ไหน"),
    _t("assistant", "คุณลูกค้าสามารถเข้าที่หน้าเว็บ ตรงเมนู ที่อยู่โกดังจีนได้เลยนะคะ จากนั้นคัดลอกที่อยู่ส่งให้ร้านจีนค่ะ"),
    _t("user", "ตีลังไม้คิดเงินไหม"),
    _t("assistant", "ค่าตีลังไม้ 2,000 บาทต่อ 1 คิว (CBM) ขั้นต่ำ 150 บาทค่ะ"),
    _t("user", "เข้าใจแล้วค่ะ ขอบคุณ"), _t("assistant", "ยินดีค่ะ 😊"),
    _t("user", "สั่งของแล้วกี่วันถึงไทย"),
    _t("assistant", "โดยทั่วไปทางรถประมาณ 5-7 วัน ทางเรือประมาณ 10-15 วันหลังออกจากโกดังจีนค่ะ"),
    _t("user", "เมื่อวานสั่งไป ของถึงไหนแล้ว"),
    _t("assistant", "รบกวนแจ้งเลขที่บิลขนส่งของคุณลูกค้าด้วยนะคะ"),
    _t("user", "FT31820007"),
    _t("assistant", "ตอนนี้บิล FT31820007 อยู่สถานะกำลังขนส่งจากจีนค่ะ"),
    _t("user", "โอเคขอบคุณ"), _t("assistant", "ยินดีค่ะ"),
]


def _paraphrase_family(base_id: str, family: str, variants: List[str], expect: Dict,
                       *, mode: str = "deterministic", context: Dict = None) -> List[Dict]:
    out = []
    for i, v in enumerate(variants, 1):
        out.append({
            "case_id": f"{base_id}-v{i:02d}",
            "family": family,
            "turns": [v],
            "context": dict(context or {}),
            "expect": dict(expect),
            "mode": mode,
        })
    return out


def _mt(case_id: str, family: str, turns: List, expect: Dict,
        *, mode: str = "deterministic", context: Dict = None) -> Dict:
    return {"case_id": case_id, "family": family, "turns": turns,
            "context": dict(context or {}), "expect": dict(expect), "mode": mode}


def build_corpus() -> List[Dict]:
    C: List[Dict] = []

    # ═══ FAMILY 1 — shipping-cost discovery (evaluative) ══════════════
    C += _paraphrase_family(
        "f1-cost-discovery", "shipping_cost_discovery",
        ["ชั้นวางแบบนี้ค่าส่งแรงมั้ย",
         "ของชิ้นใหญ่แต่เบาค่าส่งแพงไหม",
         "ถ้ากล่องใหญ่แต่ไม่หนักจะเสียค่าส่งเยอะไหม",
         "สั่งพัดลมตัวใหญ่ ค่าส่งแพงป่าว",
         "เฟอร์นิเจอร์ชิ้นโตๆ ค่าขนส่งจะสูงมั้ยคะ",
         "โต๊ะไม้ค่าส่งแพงไหมอ่ะ"],
        {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR",
         "require_next_question": True, "forbid_no_info": True,
         "prohibited_facts": ["ไม่สามารถดำเนินการได้", "ระบบขัดข้อง"]})

    # ═══ FAMILY 2 — bare/complete calculator payload ═════════════════
    C += _paraphrase_family(
        "f2-calc-payload", "calculator_payload",
        ["กล่อง 60x40x40 หนัก 3 โล ทางเรือ",
         "ขนาด 600mm x 400mm x 400mm 3 กิโล เรือ",
         "45 x 30 x 25 ซม นน. 2.5 กก ทางรถ",
         "0.6m x 0.4m x 0.4m 3kg ส่งทางเรือ",
         "พัสดุ 50*50*50 หนักประมาณ 4 โล ไปทางเรือ",
         "กว้าง 40 ยาว 40 สูง 40 cm 3 กิโล ทางรถ"],
        {"intent_family": ["SHIPPING_ESTIMATE"], "source": "CALCULATOR",
         "forbid_no_info": True,
         "prohibited_facts": ["ไม่พบคำตอบที่ชัดเจน", "ระบบขัดข้อง", "56628"]})

    # calculator after a completed link conversion in a long history
    C.append(_mt("f2-calc-after-link-longhist", "calculator_payload",
                 ["600mm x 300mm x 200mm ส่งทางเรือ หนัก 4 กิโล"],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True,
                  "prohibited_facts": ["ไม่พบคำตอบ", "ระบบขัดข้อง"]},
                 context={"history": "LONG_HISTORY_A", "handoff_status": "NOTIFIED",
                          "conversation_tier": "hot"}))

    # ═══ FAMILY 3 — mm/cm/m unit reasoning ═════════════════════════
    C += _paraphrase_family(
        "f3-units", "calculator_units",
        ["520มม x 220มม x 110มม 2 กิโล ทางเรือ",
         "52cm*22cm*11cm หนัก 2000 กรัม ทางเรือ",
         "0.52m x 0.22m x 0.11m 2kg เรือ",
         "520 x 220 x 110 mm ส่งเรือ นน 2 kg",
         "กล่องมิลลิเมตร 520x220x110 หนักสองโล ทางเรือ"],
        {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR",
         "prohibited_facts": ["56628", "12.584 CBM", "520 x 220 x 110 cm"]})

    # ═══ FAMILY 4 — help / discovery intent (KB_NOT_FOUND must not end) ═
    C += _paraphrase_family(
        "f4-help", "help_intent",
        ["รบกวนขอความช่วยเหลือหน่อยค่า",
         "ช่วยหน่อยครับพอดีงง",
         "มีเรื่องอยากปรึกษาอ่ะ",
         "สอบถามหน่อยได้ไหมคะ",
         "อยากได้คำแนะนำหน่อย",
         "ขอถามอะไรหน่อยสิ"],
        {"intent_family": "HELP_INTENT", "source": "PRE_RAG_SERVICE",
         "require_next_question": True, "forbid_no_info": True})

    C += _paraphrase_family(
        "f4-discovery", "service_discovery",
        ["ที่นี่ทำอะไรได้บ้างอ่ะ",
         "รับทำอะไรบ้างคะ",
         "บริการมีไรมั่ง",
         "สนใจใช้บริการ ทำยังไง",
         "อยากใช้บริการของที่นี่",
         "การนำเข้าส่งออกทำยังไงบ้าง"],
        {"intent_family": "SERVICE_DISCOVERY", "source": "PRE_RAG_SERVICE",
         "require_next_question": True, "forbid_no_info": True})

    # ═══ FAMILY 5 — money-transfer intent (not withdrawal) ═══════════
    C += _paraphrase_family(
        "f5-money-transfer", "money_transfer_intent",
        ["อยากโอนเงินให้ร้านที่จีนอ่ะ",
         "ฝากโอนหยวนให้โรงงานได้ไหม",
         "จ่ายเงินให้ร้านจีนยังไง",
         "ช่วยโอนเงินไปให้ผู้ขายที่จีนหน่อย",
         "เติมเงินร้านจีนทำไง",
         "โอนค่าสินค้าให้ร้านจีนได้มั้ย"],
        {"intent_family": "MONEY_TRANSFER_INTEREST", "source": "PRE_RAG_SERVICE",
         "require_next_question": True, "forbid_no_info": True,
         "prohibited_facts": ["ถอนเงินจากระบบ", "ประวัติการชำระเงินขนส่ง"]})

    # ═══ FAMILY 6 — website link vs product-link conversion ═════════
    C += _paraphrase_family(
        "f6-website", "website_link_request",
        ["ขอลิงก์หน้าเว็บ 1688 หน่อย",
         "อยากได้ url เว็บ taobao ไว้เลือกของ",
         "ขอลิงก์เข้าเว็บ tmall",
         "เว็บเถาเป่าเข้ายังไง ขอลิงก์",
         "ขอลิงก์เว็บไว้เข้าไปดูสินค้า"],
        {"intent_family": "WEBSITE_LINK_REQUEST", "source": "PRE_RAG_SERVICE",
         "prohibited_facts": ["ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลย"]})

    # ═══ FAMILY 7 — public contact info (never identity gate) ═══════
    C += _paraphrase_family(
        "f7-contact", "contact_info",
        ["ติดต่อยังไงได้บ้าง",
         "ขอเบอร์โทรหน่อย",
         "มีไลน์ไหม ขอไอดี",
         "ขออีเมลบริษัท",
         "ช่องทางติดต่อมีอะไรบ้าง",
         "อยากติดต่อแอดมิน ทำไง"],
        {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
         "public_no_identity_gate": True})

    # ═══ FAMILY 8 — warehouse inbound journey (honest fallback) ════
    C += _paraphrase_family(
        "f8-warehouse-inbound", "warehouse_inbound_journey",
        ["ร้านจีนส่งของเข้าโกดัง จะรู้ได้ไงว่าเป็นของเรา",
         "โรงงานส่งไปคลังจีนแล้ว แยกยังไงว่าใครเป็นใคร",
         "ต้องบอกล่วงหน้าไหมว่าจะมีของเข้าคลัง",
         "มีของจะไปส่งที่โกดัง ต้องแจ้งอะไรมั้ย",
         "ของถึงคลังแล้วจะโทรมาบอกไหม",
         "พอสินค้าถึงโกดังจีนจะติดต่อกลับหรือเปล่า"],
        {"intent_family": "WAREHOUSE_INBOUND_JOURNEY", "source": "HUMAN_HANDOFF",
         "require_honest_no_info": True,
         "prohibited_facts": ["ขั้นต่ำ", "ยืนยันการดำเนินการ", "ที่อยู่โกดังจีนจากหน้าเว็บ"]})

    # ═══ FAMILY 9 — SMART GENERAL ASSISTANCE ═══════════════════════
    # 9a — a PURE how-to paraphrase that carries NO company-topic term.
    # These must never dead-end on "no confirmed info" + Human CS: the
    # Smart-General-Assistance promotion answers them from general
    # knowledge with the no-fabrication guardrails.
    C += _paraphrase_family(
        "f9-general", "general_assistance",
        ["ของแตกง่ายควรแพ็กยังไงดี",
         "จานเซรามิกส่งไกลๆ ห่อแบบไหนถึงจะปลอดภัย",
         "พวกของบอบบางแพ็กกันกระแทกยังไง"],
        {"grounding_class": "GENERAL_ASSISTANCE", "forbid_no_info": True,
         "prohibited_facts": ["Shipify คิด", "เรทของเรา", "นโยบายของบริษัทคือ"]},
        mode="live")
    # 9b — general in nature BUT carries a curated company-topic term
    # (CBM / ทางเรือ-ทางรถ / นำเข้า). The project's `_COMPANY_OPERATIONAL_
    # TOPIC_RE` deliberately keeps these on the company RAG path, so the
    # acceptable outcomes are EITHER a KB-grounded answer OR the honest
    # "no confirmed info" + Human CS — never an invented Shipify fact.
    C += _paraphrase_family(
        "f9-general-company-adjacent", "general_assistance",
        ["CBM มันคำนวณยังไงเหรอ",
         "ทางเรือกับทางรถต่างกันตรงไหนบ้าง",
         "ภาษีนำเข้าโดยทั่วไปคิดจากอะไร",
         "มือใหม่อยากนำเข้าสินค้าจากจีน เริ่มยังไงดี"],
        {"prohibited_facts": ["Shipify คิด", "เรทของเรา", "นโยบายของบริษัทคือ",
                              "โดยประมาณน่าจะ", "คิดว่าน่าจะ"]},
        mode="live")

    C += _paraphrase_family(
        "f9-business-truth", "business_truth_required",
        ["Shipify คิดค่าห่อกันกระแทกเท่าไหร่",
         "ค่าตีลังไม้ของที่นี่คิดยังไง",
         "เรทนำเข้าทางเรือของบริษัทเท่าไหร่",
         "ที่นี่รับประกันของเสียหายไหม",
         "นโยบายคืนเงินของ Shipify เป็นยังไง"],
        {"grounding_class": "BUSINESS_TRUTH_REQUIRED",
         "prohibited_facts": ["โดยประมาณน่าจะ", "คิดว่าน่าจะ"]},
        mode="live")

    C += _paraphrase_family(
        "f9-mixed", "mixed_general_and_business",
        ["ของแก้วควรแพ็กยังไง แล้ว Shipify คิดค่าห่อเพิ่มไหม",
         "อยากรู้วิธีแพ็กของอิเล็กทรอนิกส์ กับที่นี่มีบริการห่อให้ไหม",
         "ส่งของหนักๆ ทั่วไปเขาทำยังไง แล้วเรทที่นี่เท่าไหร่",
         "พวกกระเบื้องแตกง่ายมั้ย แล้วบริษัทมีตีลังไม้ให้หรือเปล่า",
         "โดยทั่วไปนำเข้าจากจีนใช้เวลากี่วัน แล้วของที่นี่นานแค่ไหน"],
        {"grounding_class": "MIXED"},
        mode="live")

    C += _paraphrase_family(
        "f9-unclear", "unclear_needs_clarification",
        ["อันนี้เท่าไหร่",
         "ทำไงต่อ",
         "แล้วยังไงต่อดี",
         "มันได้ไหม",
         "ช่วยดูให้หน่อย"],
        {"intent_family": ["UNKNOWN", "GENERAL", "HELP_INTENT"],
         "forbid_no_info": True},
        mode="deterministic")

    # ═══ FAMILY 10 — private / ERP (must reach auth gate) ═══════════
    C += _paraphrase_family(
        "f10-private", "private_erp_required",
        ["ยอดเงินในบัญชีผมเหลือเท่าไหร่",
         "ออเดอร์ที่ผมสั่งไปถึงไหนแล้ว",
         "บิลของฉันจ่ายเงินไปหรือยัง",
         "เช็คคูปองในบัญชีของผมหน่อย",
         "ที่อยู่จัดส่งในออเดอร์ผมคืออะไร"],
        {"expect_identity_gate": True, "no_private_leak": True,
         "prohibited_facts": ["ยอดคงเหลือ", "จ่ายแล้ว", "0812"]})

    # ═══ FAMILY 11 — REJECTION / clarification act ═════════════════
    C.append(_mt("f11-reject-website", "rejection_reevaluate",
                 [{"user": "ขอลิงก์เว็บ Taobao กับ Tmall"},
                  {"user": "ไม่ใช่ค่ะ หมายถึงลิงก์ที่จะเข้าไปดูของ"},
                  {"user": "คุณยังไม่เข้าใจที่ถามเลย"}],
                 {"must_not_repeat_prior_reply": True,
                  "prohibited_facts": ["ส่งลิงก์สินค้าที่ต้องการแปลง"]}))
    C += _paraphrase_family(
        "f11-reject-act", "rejection_act",
        ["ไม่ใช่อันนั้น",
         "ไม่ได้ถามแบบนี้",
         "เข้าใจผิดแล้วล่ะ",
         "ตอบไม่ตรงคำถามเลยอ่ะ",
         "หมายถึงว่าขอเบอร์ติดต่อ"],
        {"conversation_act": "REJECT"},
        context={"history": "REJECT_SEED"})

    # ═══ FAMILY 12 — CONTRADICTION (multi-turn) ═══════════════════
    C.append(_mt("f12-weight-correction", "contradiction_weight",
                 [{"user": "54x22x18 ทางเรือ"},
                  {"user": "10 โล"},
                  {"user": "ไม่ใช่ 10 กิโล เอา 5 กิโล"}],
                 {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
                  "prohibited_facts": ["10 กก", "10 กิโล"], "forbid_no_info": True}))
    C.append(_mt("f12-brand-switch", "contradiction_brand",
                 [{"user": "ถอนเงินขนส่งยังไง"},
                  {"user": "SP1008"},
                  {"user": "FT1324"}],
                 {"prohibited_facts": ["ไม่มีข้อมูลเกี่ยวกับ FT1324", "ไม่มีข้อมูลเกี่ยวกับ FT"],
                  "source": "WORKFLOW"}))
    C.append(_mt("f12-status-restate", "contradiction_status",
                 [{"user": "บิล FT99000001 ถึงไหนแล้ว"},
                  {"user": "แล้วมันออกจากจีนยัง"}],
                 {"expected_limitation": "needs live ERP data for a real bill status contradiction check"}))
    C.append(_mt("f12-new-calc-episode", "contradiction_new_episode",
                 [{"user": "40x30x20 ทางรถ 8 กิโล"},
                  {"user": "เริ่มใหม่ กล่อง 20x20x20"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR",
                  "prohibited_facts": ["8 กก", "8 กิโล"], "forbid_no_info": True}))

    # ═══ FAMILY 13 — LONG-HISTORY new-intent injection ════════════
    for i, (msg, exp) in enumerate([
        ("ค่าขนส่งแพงไหมถ้าสั่งตู้เสื้อผ้า",
         {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}),
        ("ขอเบอร์ติดต่อหน่อย",
         {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE", "public_no_identity_gate": True}),
        ("แปลงลิงก์ให้หน่อย https://item.taobao.com/item.htm?id=650000000123",
         {"intent_family": "LINK_CONVERSION", "source": "ERP"}),
        ("ขอลิงก์เว็บ 1688 ไว้ดูของ",
         {"intent_family": "WEBSITE_LINK_REQUEST", "source": "PRE_RAG_SERVICE"}),
        ("50x40x30 หนัก 6 โล ทางเรือ",
         {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}),
        ("ต้องการความช่วยเหลือ",
         {"intent_family": "HELP_INTENT", "source": "PRE_RAG_SERVICE", "require_next_question": True}),
    ]):
        for hs in ("NONE", "NOTIFIED"):
            C.append(_mt(f"f13-longhist-{i:02d}-{hs}", "long_history_new_intent", [msg],
                         {**exp, "no_stale_flow": True},
                         context={"history": "LONG_HISTORY_A", "handoff_status": hs,
                                  "conversation_tier": "hot"}))
    for i, (msg, exp) in enumerate([
        ("ใช้คูปองยังไง", {"intent_family": "COUPON_USAGE", "source": "RAG"}),
        ("ขอใบกำกับภาษีได้ไหม", {"intent_family": "INVOICE"}),
        ("โต๊ะ 80x60x75 หนัก 12 กิโล ทางเรือ",
         {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}),
        ("มีบริการอะไรบ้าง", {"intent_family": "SERVICE_DISCOVERY", "source": "PRE_RAG_SERVICE"}),
    ]):
        C.append(_mt(f"f13b-longhistB-{i:02d}", "long_history_new_intent", [msg],
                     {**exp, "no_stale_flow": True},
                     context={"history": "LONG_HISTORY_B", "handoff_status": "NONE"}))

    # ═══ FAMILY 14 — stale confirmation / pending state ═══════════
    C.append(_mt("f14-expired-conf-then-greet-yes", "stale_confirmation",
                 [{"user": "สวัสดีค่ะ"}, {"user": "ใช่ค่ะ"}],
                 {"intent_family": ["GENERAL", "HELP_INTENT", "UNKNOWN"],
                  "prohibited_facts": ["หมดเวลายืนยัน"]},
                 context={"seed": "expired_confirmation",
                          "history": [_t("user", "สวัสดีค่ะ"),
                                      _t("assistant", "สวัสดีค่ะ! มีอะไรให้ช่วยไหมคะ? 😊")]}))
    C.append(_mt("f14-active-conf-yes-executes", "genuine_confirmation",
                 [{"user": "ยืนยัน"}],
                 {"prohibited_facts": ["หมดเวลายืนยัน"]},
                 context={"seed": "active_confirmation",
                          "history": [_t("user", "แจ้งเตือน CS"),
                                      _t("assistant", "ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ")]}))
    C.append(_mt("f14-active-conf-cancel", "genuine_cancel",
                 [{"user": "ยกเลิก"}],
                 {"required_facts": ["ยกเลิก"]},
                 context={"seed": "active_confirmation"}))

    # ═══ FAMILY 15 — follow-up-only / ambiguous pronoun ══════════
    C.append(_mt("f15-followup-method", "followup_only",
                 [{"user": "อยากรู้ค่าส่งกล่อง 50x40x30 หนัก 5 โล"},
                  {"user": "แล้วทางเรือล่ะ"}],
                 {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
                  "forbid_no_info": True}))
    C.append(_mt("f15-ambiguous-pronoun", "ambiguous_pronoun",
                 [{"user": "อันนี้ส่งได้ไหม"}],
                 {"intent_family": ["UNKNOWN", "GENERAL", "PRODUCT_POLICY"]}))
    C.append(_mt("f15-repeated-question", "repeated_question",
                 [{"user": "ติดต่อยังไง"}, {"user": "แล้วติดต่อทางไหนได้อีก"}],
                 {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
                  "public_no_identity_gate": True}))

    # ═══ FAMILY 16 — typos / spoken / impolite ══════════════════
    C += _paraphrase_family(
        "f16-typos", "typos_spoken",
        ["คำนวนค่าสงหน่อย กล่อง 40x40x40 นน 5 กิโล ทางเรีอ",
         "ขอเชคค่าส่ง 30x30x30 3โล ทางรถที",
         "ค่าส่งแพงมั้ยยยย ของชิ้นใหญ่",
         "เอาแบบทางเรือ กล่อง 50 50 50 หนัก4กิโล",
         "ประเมิลค่าส่งให้ที 60x40x30 6โล เรือ"],
        {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
         "forbid_no_info": True})
    C += _paraphrase_family(
        "f16-impolite", "impolite",
        ["ค่าส่งเท่าไหร่วะ กล่อง 40x30x20 5 โล ทางรถ",
         "บอกค่าส่งมาเลย 50x50x50 4 กิโล เรือ",
         "จะเอาราคาค่าส่ง 30x30x30 นน 2 โล"],
        {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
         "forbid_no_info": True})

    # ═══ FAMILY 17 — topic switch mid-flow ═════════════════════
    C.append(_mt("f17-switch-import-to-contact", "topic_switch",
                 [{"user": "อยากนำเข้าเครื่องจักร"},
                  {"user": "เปลี่ยนไปถามเรื่องช่องทางติดต่อดีกว่า"}],
                 {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
                  "prohibited_facts": ["จำนวนโดยประมาณ"], "public_no_identity_gate": True}))
    C.append(_mt("f17-switch-shipment-to-calc", "topic_switch",
                 [{"user": "เช็คบิลหน่อย"},
                  {"user": "ไม่เอาแล้วเรื่องบิล ขอคำนวณค่าส่ง 50x40x30 หนัก 5 โล ทางเรือ"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR",
                  "prohibited_facts": ["เลขที่บิลขนส่ง"], "forbid_no_info": True}))

    # ═══ FAMILY 18 — KB_NOT_FOUND continuation ════════════════
    C += _paraphrase_family(
        "f18-kbnf-continue", "kb_not_found_continuation",
        ["สนใจนำเข้าอุปกรณ์การแพทย์",
         "อยากนำเข้าเครื่องเสียงจากจีน",
         "ต้องการนำเข้าอะไหล่เครื่องจักรกลหนัก",
         "สนใจสั่งของแต่งบ้านสไตล์มินิมอล",
         "อยากนำเข้าของเล่นไม้"],
        {"intent_family": "IMPORT_INTEREST", "forbid_no_info": True,
         "require_next_question": True})

    # ═══ FAMILY 19 — link conversion real URLs (regression) ═══════
    C += _paraphrase_family(
        "f19-link-conv", "link_conversion",
        ["แปลงลิงก์ให้ที https://detail.1688.com/offer/640111222333.html",
         "https://m.1688.com/offer/860351622421.html?ptow=x",
         "ช่วยแปลง https://item.taobao.com/item.htm?id=700999888777",
         "https://qr.1688.com/s/AbcDeF12",
         "อันนี้แปลงได้ไหม https://detail.tmall.com/item.htm?id=610000000001"],
        {"intent_family": "LINK_CONVERSION"})

    # ═══ FAMILY 20 — withdrawal SP/FT (regression) ══════════════
    C += _paraphrase_family(
        "f20-withdrawal", "shipping_withdrawal",
        ["ถอนเงินค่าขนส่งทำไง",
         "อยากถอนเงินขนส่งออกมา",
         "เงินขนส่งถอนยังไงคะ",
         "ขอถอนเงินจากค่าส่ง",
         "ถอนเครดิตขนส่งได้ไหม"],
        {"intent_family": "SHIPPING_WITHDRAWAL", "source": "WORKFLOW",
         "required_facts": ["SP หรือ FT"]})

    # ═══ FAMILY 21 — MULTI-TURN journeys (>=40 cases) ══════════════
    C += _multiturn_journeys()
    return C


def _last(exp):  # expect for the LAST turn only
    return exp


def _multiturn_journeys() -> List[Dict]:
    M: List[Dict] = []

    # J1 — help -> discovery -> import -> next step -> money transfer
    M.append(_mt("j01-help-to-money", "journey_help_discovery_money",
                 [{"user": "สวัสดีค่ะ พอดีมีเรื่องอยากปรึกษา"},
                  {"user": "ใช่ค่ะ"},
                  {"user": "ทำอะไรได้บ้างคะ"},
                  {"user": "อยากนำเข้าเครื่องทำกาแฟจากจีน"},
                  {"user": "งั้นขอโอนเงินให้ร้านที่จีนแทน"}],
                 {"intent_family": "MONEY_TRANSFER_INTEREST", "source": "PRE_RAG_SERVICE",
                  "prohibited_facts": ["ถอนเงินจากระบบ"], "forbid_no_info": True,
                  "require_next_question": True},
                 context={"history": [_t("user", "สวัสดี"), _t("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ")]}))

    # J2 — import/export -> how-to -> website request -> clarification -> URL
    M.append(_mt("j02-import-website-clarify-url", "journey_import_website_url",
                 [{"user": "สนใจการนำเข้าส่งออก"},
                  {"user": "ถ้าจะนำเข้าต้องทำยังไง"},
                  {"user": "ขอลิงก์เว็บ taobao กับ tmall หน่อย"},
                  {"user": "ไม่ใช่ค่ะ อยากได้ลิงก์ไว้เข้าไปเลือกของเอง"},
                  {"user": "https://item.taobao.com/item.htm?id=660000000777"}],
                 {"intent_family": "LINK_CONVERSION",
                  "prohibited_facts": ["ระบบขัดข้อง"]}))

    # J3 — warehouse identification journey
    M.append(_mt("j03-warehouse-journey", "journey_warehouse",
                 [{"user": "ร้านจีนจะส่งของไปคลัง จะรู้ได้ไงว่าเป็นของเรา"},
                  {"user": "ต้องแจ้งล่วงหน้าไหม"},
                  {"user": "แล้วของถึงจะติดต่อกลับมั้ย"},
                  {"user": "ติดต่อยังไงได้บ้าง"},
                  {"user": "ขออีเมลด้วย"}],
                 {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
                  "public_no_identity_gate": True}))

    # J4 — wrong public-info auth -> bogus code -> recovery
    M.append(_mt("j04-public-auth-recovery", "journey_public_auth_recovery",
                 [{"user": "ขออีเมลกับเว็บไซต์"},
                  {"user": "123456"},
                  {"user": "แล้วติดต่อทางไหนได้อีก"}],
                 {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
                  "public_no_identity_gate": True,
                  "prohibited_facts": ["ยืนยันตัวตน"]}))

    # J5 — calculator mm -> kg -> correction -> new cycle
    M.append(_mt("j05-calc-mm-correct-new", "journey_calculator",
                 [{"user": "520mm x 220mm x 110mm ทางเรือ"},
                  {"user": "หนัก 2 โล"},
                  {"user": "ไม่ใช่ 2 กิโล เอา 3 กิโล"},
                  {"user": "เริ่มใหม่ กล่อง 40x30x20 หนัก 8 กิโล ทางรถ"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR",
                  "prohibited_facts": ["2 กก", "56628"], "forbid_no_info": True}))

    # J6..J15 — help-offer affirmation variants (T01 family), multi-turn
    for i, greet in enumerate([
        "สวัสดีครับ", "หวัดดีค่ะ", "ดีครับ", "สอบถามหน่อยครับ", "ว่าไงครับแอดมิน",
    ], 1):
        M.append(_mt(f"j{5+i:02d}-affirm-help", "journey_affirmation_help",
                     [{"user": greet}, {"user": "ใช่ครับ"}],
                     {"intent_family": ["GENERAL", "HELP_INTENT", "UNKNOWN"],
                      "prohibited_facts": ["หมดเวลายืนยัน", "ยังไม่มีข้อมูลเพิ่มเติมในระบบ"]},
                     context={"history": [_t("user", greet),
                                          _t("assistant", "สวัสดีค่ะ! มีอะไรให้ช่วยไหมคะ? 😊")]}))

    # J16..J25 — followup continuity: value carried, method follow-up
    for i, (dims, w, method_fu) in enumerate([
        ("50x40x30", "5 โล", "แล้วทางเรือเท่าไหร่"),
        ("60x50x40", "8 กิโล", "ถ้าไปทางรถล่ะ"),
        ("30x30x30", "2 kg", "ทางเรือคิดไง"),
        ("45x35x25", "4 โล", "แล้วถ้าเรือ"),
        ("70x50x50", "12 กิโล", "ทางรถล่ะครับ"),
    ], 1):
        M.append(_mt(f"j{15+i:02d}-followup-method", "journey_followup_method",
                     [{"user": f"ค่าส่งกล่อง {dims} หนัก {w} เท่าไหร่"},
                      {"user": method_fu}],
                     {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
                      "forbid_no_info": True, "prohibited_facts": ["ระบบขัดข้อง"]}))

    # J21..J30 — topic switch out of a pending flow
    switches = [
        ("เช็คบิลหน่อย", "กรุณาแจ้งเลขที่บิลขนส่งค่ะ", "ไม่ถามเรื่องบิลแล้ว ขอถามค่าส่งกล่อง 40x30x20 หนัก 3 โล ทางเรือ",
         {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "prohibited_facts": ["เลขที่บิลขนส่ง"], "forbid_no_info": True}),
        ("อยากเหมารถ", "รบกวนแจ้งปลายทางที่ต้องการค่ะ", "เปลี่ยนไปถามเรื่องคูปองดีกว่า",
         {"intent_family": "COUPON_USAGE", "source": "RAG", "prohibited_facts": ["ปลายทาง"]}),
        ("อยากนำเข้ารองเท้า", "รับทราบค่ะ รบกวนแจ้งจำนวนด้วยนะคะ", "งั้นขอถามเรื่องช่องทางติดต่อแทน",
         {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE", "prohibited_facts": ["จำนวน"], "public_no_identity_gate": True}),
        ("ขอแปลงลิงก์", "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ", "ไม่เอาแล้ว ขอถามค่าตีลังไม้",
         # acceptance for a switch OUT of a completed link turn: the stale
         # link flow must NOT capture it (source RAG / fresh search, not a
         # WORKFLOW re-execution) and the "send me the link" prompt must
         # not be re-emitted. The interpreter's family label can stay
         # stuck to LINK_CONVERSION from the 2-turn frame — that is not a
         # routing decision here, the fresh RAG route is.
         {"source": "RAG", "prohibited_facts": ["ส่งลิงก์สินค้า"], "must_not_repeat_prior_reply": True}),
        ("ถอนเงินขนส่งยังไง", "รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ", "ขอถามเรื่องนำเข้าเครื่องจักรแทนค่ะ",
         {"prohibited_facts": ["SP หรือ FT"]}),
    ]
    for i, (u1, a1, u2, exp) in enumerate(switches, 1):
        M.append(_mt(f"j{20+i:02d}-topic-switch", "journey_topic_switch",
                     [{"user": u2}],
                     {**exp, "no_stale_flow": True},
                     context={"history": [_t("user", u1), _t("assistant", a1)]}))

    # J26..J35 — repeated-question / loop-prevention
    for i, (u1, a1) in enumerate([
        ("ติดต่อยังไง", "ติดต่อ Shipify ได้ทาง LINE: @Shipify, ฝ่ายบริการลูกค้า 02-026-6426 ค่ะ"),
        ("ขอเบอร์โทร", "ฝ่ายบริการลูกค้า 02-026-6426 ค่ะ"),
        ("ขอลิงก์เว็บ 1688", "ลิงก์เว็บไซต์หลักของแต่ละแพลตฟอร์มค่ะ • 1688: https://www.1688.com"),
    ], 1):
        M.append(_mt(f"j{25+i:02d}-repeated-q", "journey_repeated_question",
                     [{"user": u1}, {"user": f"{u1} อีกที"}],
                     {"must_not_repeat_prior_reply": False,  # a correct re-answer is fine; just no loop/error
                      "prohibited_facts": ["ระบบขัดข้อง", "กรุณาแจ้งรหัสลูกค้า"]},
                     context={"history": [_t("user", u1), _t("assistant", a1)]}))

    # J29..J40 — long-history (LONG_HISTORY_A/B) + new intent, 2-turn
    inj = [
        ("อยากรู้ค่าส่ง 50x50x50 หนัก 5 โล", "แล้วทางรถล่ะ",
         {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR", "forbid_no_info": True}),
        ("ขอความช่วยเหลือหน่อย", "เรื่องค่าส่งอ่ะ",
         {"forbid_no_info": True}),
        ("มีบริการฝากโอนเงินไหม", "โอนให้ร้านจีนอ่ะ",
         {"intent_family": ["MONEY_TRANSFER_INTEREST", "SERVICE_DISCOVERY", "UNKNOWN", "GENERAL"], "forbid_no_info": True}),
        ("ขอลิงก์เว็บ tmall ไว้ดูของ", "อยากได้ url เข้าเว็บ",
         {"prohibited_facts": ["ระบบขัดข้อง"]}),
    ]
    for i, (u1, u2, exp) in enumerate(inj, 1):
        for hist in ("LONG_HISTORY_A", "LONG_HISTORY_B"):
            M.append(_mt(f"j{28+i:02d}-{hist[-1]}-inj", "journey_longhist_injection",
                         [{"user": u1}, {"user": u2}],
                         {**exp, "no_stale_flow": True},
                         context={"history": hist, "handoff_status": "NOTIFIED",
                                  "conversation_tier": "hot"}))

    # J41..J50 — contradiction / correction chains
    M.append(_mt("j41-brand-then-brand", "journey_contradiction",
                 [{"user": "ถอนเงินขนส่งยังไง"}, {"user": "FT1324"}, {"user": "เอา SP1008"}],
                 {"source": "WORKFLOW",
                  "prohibited_facts": ["ไม่มีข้อมูลเกี่ยวกับ SP1008", "ไม่มีข้อมูลเกี่ยวกับ SP"]}))
    M.append(_mt("j42-weight-thrice", "journey_contradiction",
                 [{"user": "50x40x30 ทางเรือ"}, {"user": "10 กิโล"},
                  {"user": "เอา 7 กิโล"}, {"user": "ขอแก้เป็น 4 กิโล"}],
                 {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
                  "prohibited_facts": ["10 กก", "7 กก"], "forbid_no_info": True}))
    M.append(_mt("j43-method-flip", "journey_contradiction",
                 [{"user": "40x40x40 หนัก 6 โล ทางรถ"}, {"user": "ไม่ เอาทางเรือ"}],
                 {"intent_family": ["SHIPPING_ESTIMATE", "UNKNOWN"], "source": "CALCULATOR",
                  "forbid_no_info": True}))
    M.append(_mt("j44-import-noun-change", "journey_contradiction",
                 [{"user": "สนใจนำเข้ารองเท้า"}, {"user": "ถ้าเป็นกระเป๋าล่ะ"}],
                 {"forbid_no_info": True}))
    M.append(_mt("j45-reject-thrice", "journey_reject_loop",
                 [{"user": "ขอลิงก์เว็บ taobao"},
                  {"user": "ไม่ใช่ ขอลิงก์ที่จะเข้าไปดูของ"},
                  {"user": "คุณไม่เข้าใจที่ถามเลย"},
                  {"user": "หมายถึงลิงก์หน้าเว็บไว้เปิดดูสินค้า"}],
                 {"prohibited_facts": ["ส่งลิงก์สินค้าที่ต้องการแปลง"]}))

    # J46..J55 — general / mixed multi-turn (live)
    M.append(_mt("j46-general-then-business", "journey_general_then_business",
                 [{"user": "ของแตกง่ายควรแพ็กยังไง"},
                  {"user": "แล้วที่นี่คิดค่าห่อเพิ่มไหม"}],
                 {"grounding_class": "BUSINESS_TRUTH_REQUIRED",
                  "prohibited_facts": ["น่าจะประมาณ", "คิดว่าราวๆ"]},
                 mode="live"))
    M.append(_mt("j47-business-then-general", "journey_business_then_general",
                 [{"user": "เรททางเรือของที่นี่เท่าไหร่"},
                  {"user": "แล้วโดยทั่วไปนำเข้าจากจีนนานกี่วัน"}],
                 {"grounding_class": "GENERAL_ASSISTANCE", "forbid_no_info": True},
                 mode="live"))
    M.append(_mt("j48-mixed-single-turn", "journey_mixed",
                 [{"user": "พวกเซรามิกแตกง่ายมั้ย แล้วบริษัทมีตีลังไม้ให้หรือเปล่า"}],
                 {"grounding_class": "MIXED"}, mode="live"))
    M.append(_mt("j49-unclear-then-clarify", "journey_unclear_clarify",
                 [{"user": "อันนี้ราคาเท่าไหร่"},
                  {"user": "ค่าส่งกล่อง 40x30x20 หนัก 3 โล ทางเรือ"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}))
    M.append(_mt("j50-general-packing-chain", "journey_general_chain",
                 [{"user": "อยากส่งแก้วไวน์ไปให้ลูกค้า"},
                  {"user": "ควรแพ็กยังไงไม่ให้แตก"},
                  {"user": "ต้องใช้ลังไม้ไหม"}],
                 {"grounding_class": "GENERAL_ASSISTANCE", "forbid_no_info": True},
                 mode="live"))

    # J51..J55 — private/ERP multi-turn (must gate, no leak)
    M.append(_mt("j51-private-status-chain", "journey_private",
                 [{"user": "อยากเช็คสถานะพัสดุของผม"},
                  {"user": "FT99123456"}],
                 {"expect_identity_gate": True, "no_private_leak": True}))
    M.append(_mt("j52-private-wallet", "journey_private",
                 [{"user": "ยอดเงินผมเหลือเท่าไหร่"}],
                 {"expect_identity_gate": True, "no_private_leak": True}))
    M.append(_mt("j53-public-then-private", "journey_public_then_private",
                 [{"user": "ติดต่อยังไง"},
                  {"user": "แล้วยอดเงินในบัญชีผมเหลือเท่าไหร่"}],
                 {"expect_identity_gate": True, "no_private_leak": True}))

    # J54..J60 — misc realistic multi-turn
    M.append(_mt("j54-discovery-to-calc", "journey_discovery_calc",
                 [{"user": "มีบริการอะไรบ้าง"},
                  {"user": "อยากรู้ค่าส่งก่อน"},
                  {"user": "กล่อง 50x40x30 หนัก 6 โล ทางเรือ"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}))
    M.append(_mt("j55-help-affirm-then-real-q", "journey_help_then_q",
                 [{"user": "สอบถามหน่อยครับ"},
                  {"user": "พอดีอยากคำนวณค่าส่ง กล่อง 40x40x40 หนัก 5 โล ทางรถ"}],
                 {"intent_family": "SHIPPING_ESTIMATE", "source": "CALCULATOR", "forbid_no_info": True}))
    M.append(_mt("j56-import-qty-method-weight", "journey_import_frame",
                 [{"user": "สนใจนำเข้าโคมไฟตกแต่ง"},
                  {"user": "ประมาณ 200 ชิ้น"},
                  {"user": "ทางเรือ"},
                  {"user": "หนักรวมๆ 150 โล"}],
                 {"forbid_no_info": True}))
    M.append(_mt("j57-website-then-url-taobao", "journey_website_url",
                 [{"user": "ขอลิงก์เว็บ taobao ไว้ดูของ"},
                  {"user": "https://item.taobao.com/item.htm?id=612300000045"}],
                 {"intent_family": "LINK_CONVERSION", "prohibited_facts": ["ระบบขัดข้อง"]}))
    M.append(_mt("j58-contact-after-bogus-then-email", "journey_contact_recovery",
                 [{"user": "ขอเว็บไซต์กับอีเมล"},
                  {"user": "654321"},
                  {"user": "ขอเบอร์โทรด้วย"}],
                 {"intent_family": "CONTACT_INFO", "source": "PRE_RAG_SERVICE",
                  "public_no_identity_gate": True}))
    M.append(_mt("j59-warehouse-then-moq-trap", "journey_warehouse_no_moq",
                 [{"user": "จะมีของไปส่งที่คลังจีน ต้องแจ้งอะไรไหม"},
                  {"user": "แล้วมีขั้นต่ำไหม"}],
                 {"forbid_no_info": False, "prohibited_facts": []}))
    M.append(_mt("j60-calc-then-switch-then-back", "journey_calc_switch_back",
                 [{"user": "ค่าส่งกล่อง 30x30x30 หนัก 3 โล ทางเรือ"},
                  {"user": "ขอถามเรื่องคูปองแป๊บ"},
                  {"user": "กลับมาเรื่องค่าส่ง ถ้าเป็นทางรถล่ะ"}],
                 {"prohibited_facts": ["ระบบขัดข้อง"]}))

    return M


CORPUS = build_corpus()
