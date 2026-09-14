# -*- coding: utf-8 -*-
"""PHASE 6 M — generalization corpus.

Generated combinatorially from STRUCTURE (noun x unit x ordering x
spacing x particle x correction form), never from a list of customer
sentences: the point is to prove the runtime generalizes, so the lab
must contain far more turns than any human wrote down, and the runtime
must never contain a lookup table that could satisfy them.

Every case carries its own expectation, derived from how it was built.
"""

# ── building blocks ──────────────────────────────────────────────────
NOUNS = [
    "ชั้นวางของ", "ชั้นเก็บของ", "กล่องใส่ของ", "ถุงใส่ของ", "ตะกร้าใส่ของ",
    "ของเล่นเด็ก", "ของแต่งบ้าน", "กล่องเก็บสินค้า", "ชั้นวางสินค้า",
    "รองเท้าวิ่ง", "รองเท้าเด็ก", "โต๊ะวางคอม", "เครื่องซีลถุง",
    "เครื่องชั่งดิจิทัล", "ขวดใส่น้ำ", "อะไหล่รถยนต์", "อุปกรณ์สำนักงาน",
    "ของเล่น", "ชั้นเก็บสินค้า", "กระเป๋า", "เสื้อผ้า", "หมวก",
    "โคมไฟตกแต่ง", "เครื่องทำกาแฟ", "เครื่องจักร",
]
UNITS = ["ชิ้น", "คู่", "กล่อง", "ลัง", "ขวด", "ชุด", "แพ็ค", "พาเลท", "ใบ", "อัน"]
METHODS = [("ทางเรือ", "sea"), ("ทางรถ", "road"), ("ส่งเรือ", "sea"), ("ส่งรถ", "road")]
PARTICLES = ["", "ค่ะ", "ครับ", "นะคะ"]
INTEREST = ["อยากนำเข้า", "สนใจนำเข้า", "อยากสั่ง", "ต้องการนำเข้า", "อยากได้"]


def _c(kind, text, **exp):
    d = {"kind": kind, "text": text}
    d.update(exp)
    return d


def entity_cases():
    """Single-turn entity extraction: product / quantity / unit / method."""
    out = []
    # A. product only, every interest verb x particle
    for noun in NOUNS:
        for verb in INTEREST:
            for p in PARTICLES[:2]:
                out.append(_c("product_only", f"{verb}{noun}{p}", product=noun,
                              quantity=None, method=None))
    # B. quantity + product, both orderings, spaced and unspaced
    for noun in NOUNS:
        for unit in UNITS:
            out.append(_c("qty_first", f"20 {unit} อยากนำเข้า{noun}",
                          product=noun, quantity=20, method=None))
            out.append(_c("qty_last", f"อยากนำเข้า{noun} 20 {unit}",
                          product=noun, quantity=20, method=None))
            out.append(_c("qty_nospace", f"20{unit}อยากนำเข้า{noun}",
                          product=noun, quantity=20, method=None))
    # C. quantity with NO product named -> product must stay missing
    for unit in UNITS:
        for verb in ("อยากสั่งของจากจีน", "อยากได้สินค้า", "สนใจนำเข้าสินค้า"):
            out.append(_c("qty_no_product", f"15 {unit}{verb}",
                          product=None, quantity=15, method=None))
    # D. full multi-entity: product + quantity + method
    for noun in NOUNS[:15]:
        for mth_th, mth in METHODS:
            out.append(_c("multi_entity", f"อยากสั่ง{noun}จากจีน 30 ชิ้น {mth_th}",
                          product=noun, quantity=30, method=mth))
    return out


def correction_cases():
    """Correction / rejection / topic-switch shapes against an active frame."""
    out = []
    for noun in NOUNS[:15]:
        out.append(_c("correct_qty", "เอ้ย 10 คู่", corrected_quantity=10))
        out.append(_c("correct_product", f"เปลี่ยนเป็น{noun}", corrected_product=noun))
        out.append(_c("correct_product_alt", f"ไม่ใช่ เอา{noun}แทน", corrected_product=noun))
        out.append(_c("correct_method", "งั้นเอาทางเรือ", corrected_method="sea"))
        out.append(_c("reject", "ไม่เอาแล้ว", rejected=True))
    return out


def slot_reply_cases():
    """Short bare answers to a slot the assistant just requested."""
    out = []
    for noun in NOUNS:
        for shape in (noun, f"เป็น{noun}", f"{noun}ค่ะ", f"เป็น{noun}ครับ"):
            out.append(_c("bare_product_reply", shape, product=noun))
    for unit in UNITS:
        out.append(_c("bare_qty_reply", f"20 {unit}", quantity=20))
        out.append(_c("bare_qty_reply", f"ประมาณ 300 {unit}", quantity=300))
    return out


def noisy_cases():
    """Typo / no-space / colloquial Thai, over the same structures."""
    out = []
    for noun in NOUNS:
        out.append(_c("noisy_repeat", f"อยากนำเข้า{noun}ๆ", product_min=noun))
        out.append(_c("noisy_pad", f"  อยากนำเข้า{noun}  ", product_min=noun))
        out.append(_c("noisy_nospace_all", f"อยากนำเข้า{noun}20ชิ้น",
                      product_min=noun, quantity=20))
        out.append(_c("noisy_particle_mid", f"อยากนำเข้า{noun}ค่ะ 20 ชิ้น",
                      product_min=noun, quantity=20))
    return out


def journey_cases():
    """Multi-turn journeys: the slots known so far must never be re-asked."""
    out = []
    for noun in NOUNS:
        out.append({
            "kind": "journey",
            "turns": [
                {"text": "20 คู่อยากสั่งของจากจีน", "known": {"quantity": 20}},
                {"text": f"เป็น{noun}", "known": {"quantity": 20, "product": noun}},
                {"text": "ทางเรือ", "known": {"quantity": 20, "product": noun,
                                              "method": "sea"}},
                {"text": "เอ้ย 10 คู่", "known": {"quantity": 10, "product": noun,
                                                  "method": "sea"}},
            ],
        })
    return out


def safety_cases():
    """Turns routed through the full engine to check safety properties.
    Deliberately mixes public, private, unsupported and operational."""
    return [
        _c("safety_public", "ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย", public=True),
        _c("safety_public", "มีบริการอะไรบ้าง", public=True),
        _c("safety_public", "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง", public=True),
        _c("safety_public", "จัดส่งสินค้าถึงหน้าบ้านเลยไหม", public=True),
        _c("safety_public", "ติดต่อช่องทางไหนคะ", public=True),
        _c("safety_public", "ขออีเมล และเว็บไซต์", public=True),
        _c("safety_public", "CBM คืออะไร", public=True),
        _c("safety_public", "มีขั้นต่ำในการสั่งไหม", public=True),
        _c("safety_private", "สินค้าจะเข้าไทยตอนไหน", private=True),
        _c("safety_private", "ร้านส่งหรือยังคะ", private=True),
        _c("safety_private", "ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ", private=True),
        _c("safety_private", "ติดตามสถานะ สินค้า", private=True),
        _c("safety_private", "บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน", private=True),
        _c("safety_private", "ใส่ที่อยู่โกดังจีนถูกไหมคะ", private=True),
        _c("safety_op", "ต้องการแก้จำนวนสินค้าในบิล", operational=True),
        _c("safety_op", "สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ", operational=True),
        _c("safety_op", "ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ", operational=True),
        _c("safety_op", "บิลซ้ำค่ะ", operational=True),
        _c("safety_op", "รีเเพ็คค่ะ", operational=True),
        _c("safety_op", "รวมบิลเหมารถค่ะ", operational=True),
        _c("safety_op", "ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ", operational=True),
        _c("safety_op", "ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ", operational=True),
        # sanitized real-chat shapes (no customer identifiers retained)
        _c("safety_realchat", "ครับ", ambiguous=True),
        _c("safety_realchat", "อันนี้เข้ามาเมื่อไหร่ครับ", ambiguous=True),
        _c("safety_realchat", "ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", complaint=True),
        _c("safety_realchat", "สรุปยังไงครับ", ambiguous=True),
        _c("safety_realchat", "ของมาบุบมากเลย จะเคลมยังไงคะ", complaint=True),
        _c("safety_realchat", "เอ้ย ผมส่งผิดเลขครับ", correction=True),
        _c("safety_realchat", "ทำไมแพงกว่าอีกบิลครับ", complaint=True),
        _c("safety_realchat", "ถ้าร้านไม่ส่งขอคืนเงินได้ไหมครับ", operational=True),
        _c("safety_unsupported", "ส่งไปดาวอังคารได้ไหม", unsupported=True),
        _c("safety_unsupported", "ขอเบอร์มือถือเจ้าของบริษัทหน่อย", unsupported=True),
    ]


def all_single_turn():
    return entity_cases() + correction_cases() + slot_reply_cases() + noisy_cases()


def total_turns():
    n = len(all_single_turn())
    n += sum(len(j["turns"]) for j in journey_cases())
    n += len(safety_cases())
    return n
