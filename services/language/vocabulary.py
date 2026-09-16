# -*- coding: utf-8 -*-
"""BOUNDED vocabularies the fuzzy matcher may correct TOWARDS.

The normaliser never fuzzy-matches arbitrary customer text into a
business meaning: RapidFuzz is only ever asked "is this noisy span a
misspelling of one of THESE closed-class words?". Product names,
customer names and identifiers are not in any group here, so they can
never be rewritten (task §2/§11 — unknown product names are valid).

Every group carries:

  terms          canonical spellings — the SAME forms the central
                 interpreter's own alternations already recognise, so a
                 correction lands on wording the engine understands.
  left_context   a structural collocation that turns a MEDIUM match into
                 a HIGH one (a number before a unit, a transport verb
                 before a shipping mode, "ถึง" before a destination).
                 These are grammatical roles, not phrases.
  right_context  the mirror image.
  min_len        terms shorter than this are only matched when the
                 difference is marks-only (skeleton-equal) — a 2-letter
                 word one edit away from another 2-letter word is noise,
                 not evidence.

Customer configuration: a second deployment replaces THIS module's
group contents (or, later, loads them from configuration); the matcher
in thai_normalizer.py does not change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class VocabGroup:
    name: str
    terms: Tuple[str, ...]
    left_context: Optional[str] = None
    right_context: Optional[str] = None
    min_len: int = 3
    # may a span that is itself a dictionary word be corrected towards a
    # term of this group? False for function words: "คะ" is not a typo
    # of "ค่ะ", it is the other particle.
    correct_dictionary_words: bool = True


# end-of-message is deliberately NOT a confirming context: every final
# word is at the end, so "$" would confirm anything ("3 ลัง" -> "ลิงก์").
_POLITE_TAIL = r"^(?:ได้|ไหม|มั้ย|ครับ|ค่ะ|คะ|ปะ|ป่ะ|หรอ|เหรอ|นะ|หน่อย|ด้วย|เลย|แล้ว|ยัง)"

GROUPS: Tuple[VocabGroup, ...] = (
    VocabGroup(
        name="shipping_method",
        terms=("ทางรถ", "ทางเรือ", "ทางอากาศ", "ทางเครื่องบิน", "โดยรถ", "โดยเรือ",
               "โดยเครื่องบิน", "ส่งเรือ", "ส่งรถ", "เรือ", "รถ", "เครื่องบิน"),
        left_context=r"(?:ส่ง|ทาง|โดย|เอา|ไป|ใช้|มา|ขนส่ง|ขอ|เป็น|แบบ)\s*$",
        right_context=_POLITE_TAIL + r"|^(?:ดีกว่า|ถูกกว่า|เร็วกว่า|กี่วัน|ราคา)",
    ),
    VocabGroup(
        name="count_unit",
        terms=("ชิ้น", "คู่", "กล่อง", "ลัง", "ขวด", "ชุด", "แพ็ค", "พาเลท",
               "ตัว", "อัน", "ใบ", "โหล", "ผืน", "เครื่อง", "หลัง", "คิว"),
        left_context=r"\d\s*$|(?:กี่|หลาย|สอง|สาม|สี่|ห้า|สิบ|ร้อย|พัน)\s*$",
        min_len=2,
    ),
    # TYPED MEASUREMENT (REAL LINE 2026-09-16) — weight units are their
    # OWN group, separate from count units. "โล" (colloquial กิโล) and
    # "กก" are canonical weight words here, so the matcher's "an exact
    # vocabulary word is canonical" rule keeps them as they are; before
    # this they were out-of-vocabulary and "30 โล" was fuzzy-corrected
    # (edit distance 1, digit context) to the COUNT unit "30 โหล" — the
    # customer's weight became an order quantity of thirty dozen. The
    # vocabulary is evidence only: which typed slot a number belongs to
    # is decided by the semantic layer's measurement parser, never here.
    VocabGroup(
        name="weight_unit",
        terms=("กิโล", "กิโลกรัม", "กรัม", "โล", "กก"),
        left_context=r"\d\s*$|(?:หนัก|น้ำหนัก|กี่|หลาย|สอง|สาม|สี่|ห้า|สิบ|ร้อย|พัน)\s*$",
        min_len=2,
    ),
    VocabGroup(
        name="business_action",
        terms=("ถอนเงิน", "ยกเลิก", "เคลม", "ติดตาม", "ใบกำกับ", "ใบกำกับภาษี",
               "คูปอง", "เช็ค", "ออเดอร์", "สถานะ", "พัสดุ", "สั่งของ", "สั่งซื้อ",
               "นำเข้า", "ฝากสั่ง", "จัดส่ง", "ราคา", "ค่าส่ง", "ที่อยู่", "คืนเงิน",
               "ติดต่อ", "เบอร์", "โกดัง", "ชิปปิ้ง", "บริการ", "สมัคร", "ลงทะเบียน",
               "เลขพัสดุ", "ใบเสร็จ", "วอลเล็ท", "ส่วนลด", "ยอดเงิน", "บิล", "เปลี่ยน",
               "แก้ไข", "น้ำหนัก", "ขนาด", "โอนเงิน", "ชำระ", "จ่าย", "ค่าบริการ",
               "สินค้า", "เว็บ", "ลิงก์"),
        left_context=r"(?:ขอ|อยาก|ต้องการ|ช่วย|จะ|เช็ค|เช็ก|ดู|ตาม|แจ้ง|ทำ|เลข|ของ|"
                     r"เปลี่ยน|แก้|ยกเลิก|ติดตาม|มี|ใช้|เอา|สั่ง|นำ|ฝาก)\s*$",
        right_context=_POLITE_TAIL + r"|^(?:ของ|ให้|ผม|ฉัน|หน่อย|ยัง|ได้|ไม่|เท่าไหร่|กี่)",
    ),
    VocabGroup(
        name="generic_object",
        # the generic placeholder object ("สั่งของ", "ซื้อของ") — a real
        # product noun is never in this list.
        terms=("ของ",),
        left_context=r"(?:สั่ง|ซื้อ|ขน|เอา|นำเข้า|ส่ง|ฝาก|เช็ค|ดู|รับ)\s*$",
        min_len=2,
    ),
    VocabGroup(
        name="origin_destination",
        terms=("จีน", "ไทย", "ถึงไทย", "จากจีน", "เข้าไทย", "มาไทย"),
        left_context=r"(?:ถึง|เข้า|มา|จาก|ของ|สินค้า|เว็บ|ประเทศ|ไป|ที่|กลับ|ส่ง)\s*$",
        right_context=r"^(?:หรือยัง|ยัง|แล้ว|ได้|ไหม|มั้ย|กี่วัน|เมื่อไหร่)",
        min_len=2,
    ),
    VocabGroup(
        name="function_word",
        terms=("เป็น", "ได้", "อยาก", "ต้องการ", "สนใจ", "เท่าไหร่", "เท่าไร", "ยังไง",
               "อย่างไร", "ทำไม", "เมื่อไหร่", "ที่ไหน", "อะไร", "ประมาณ", "ช่วย",
               "หน่อย", "แบบนี้", "แบบไหน", "หรือยัง", "หรือเปล่า", "ตอนนี้", "แล้ว",
               "ครับ", "ค่ะ", "ไหม", "มั้ย", "เอ้ย", "เอ๊ย",
               # demonstrative references (closed class; never a product)
               "อันนี้", "ตัวนี้", "สินค้านี้"),
        # "ได" before a yes/no particle is "ได้" ("นำเข้าไดไหม"); a verb
        # before it confirms the same role.
        left_context=r"(?:นำเข้า|ส่ง|สั่ง|ซื้อ|ทำ|ใช้|จ่าย|ถอน|ยกเลิก|เปลี่ยน|แก้|ออก|รับ)\s*$",
        right_context=r"^(?:ไหม|มั้ย|มัย|หรอ|เหรอ|ปะ|ป่ะ|หรือเปล่า|รึเปล่า)",
        correct_dictionary_words=False,
    ),
)

# INFORMAL PARTICLE SPELLINGS -> canonical. Exact-token aliases, applied
# only in the sentence-final particle position (the customer's
# politeness marker), never inside a noun. "คับ" mid-sentence means
# "tight" and is left alone.
PARTICLE_ALIASES: Dict[str, str] = {
    "คับ": "ครับ", "ค้าบ": "ครับ", "คร้าบ": "ครับ", "ครัช": "ครับ", "คั้บ": "ครับ",
    "ครับผม": "ครับ", "งับ": "ครับ", "ค๊าบ": "ครับ", "ขอรับ": "ครับ", "ครั้บ": "ครับ",
    "คร่า": "ค่ะ", "ค่าา": "ค่ะ", "ค๊ะ": "คะ", "ค้ะ": "ค่ะ", "คะ่": "ค่ะ",
    "จ้าา": "จ้า", "จ๊า": "จ้า", "น้า": "นะ", "น๊า": "นะ",
    # "ได้ป่าว" is the same colloquial permission question as "ได้ป่ะ",
    # which the interpreter's permission alternation already knows.
    "ป่าว": "ป่ะ",
    # "ครบ" (a dropped ั) is the commonest misspelling of ครับ, but also
    # the real word "complete" — guarded below.
    "ครบ": "ครับ",
}
# an alias is NOT applied when the text before the particle tail ends
# with one of these: "ได้รับของครบ" means "received everything".
# The guard is a COMPLETION VERB (optionally followed by its object): "ได้รับของ|ครบ"
# is guarded, "ชั้นวางของ|ครบ" (a product noun that happens to end in ของ) is not.
PARTICLE_ALIAS_GUARDS: Dict[str, str] = {
    "ครบ": r"(?:รับ|ได้|ส่ง|มา|ถึง|จ่าย|ชำระ|โอน|ไม่|ยัง|ให้)(?:ของ|สินค้า|เงิน|ยอด)?\s*$",
}
# particles that may FOLLOW another particle at the tail ("ครับผม",
# "นะครับ", "ค่ะๆ") — the alias pass walks back over these.
PARTICLE_TAIL = ("ครับ", "ค่ะ", "คะ", "นะ", "จ้า", "จ้ะ", "เลย", "ด้วย", "หน่อย",
                 "อ่ะ", "อะ", "ล่ะ", "ๆ", "ฮะ", "ฮับ", "ขา")


def all_terms() -> Tuple[str, ...]:
    seen, out = set(), []
    for g in GROUPS:
        for t in g.terms:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return tuple(out)


def group_of(term: str) -> Optional[VocabGroup]:
    for g in GROUPS:
        if term in g.terms:
            return g
    return None
