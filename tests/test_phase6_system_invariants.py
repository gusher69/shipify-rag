# -*- coding: utf-8 -*-
"""PHASE 6 — SYSTEM-WIDE CONVERSATION INTELLIGENCE INVARIANTS.

Executable acceptance for the system-level properties the owner asked
for, rather than one assertion per customer sentence:

  G. PRODUCT-NOUN ADVERSARIAL — 150+ generated Thai product-noun cases
     (compound nouns containing / starting with / ending with "ของ" or
     "สินค้า") across quantity position, spacing, particles and typos.
  I. RESPONSE / NEXT-ACTION CONSISTENCY — a slot that was acknowledged,
     or is known from compatible state, can never be requested in the
     same breath; a current-turn entity can never be dropped.
  F. MULTI-INTENT CONSUMPTION — a question asked alongside a workflow
     trigger must be answered, not silently discarded.

Test tier is pinned by tests/__init__.py (offline by default).
"""
import re
import unittest

from services.conversation_semantics import (
    _compose, _bare_product_noun, Frame, frame_ack_reply,
)
from services.playground_orchestrator import _product_interest_noun
from services.shipping_estimate_flow import (
    asks_rate_basis, rate_basis_answer, derive_estimate_state,
    extract_estimate_fields, estimate_missing_prompt, RATES,
)


# ══════════════════════════════════════════════════════════════════
# G. PRODUCT-NOUN ADVERSARIAL SET
# ══════════════════════════════════════════════════════════════════
# Every noun the owner listed, plus ones the real sources use.
# Deliberately loaded with the hard class: a generic formant ("ของ",
# "สินค้า") sitting at the START, the END, or INSIDE a genuine product
# name, where Thai's lack of word spaces gives a regex no boundary.
_NOUNS = [
    "ชั้นวางของ", "ชั้นเก็บของ", "กล่องใส่ของ", "ถุงใส่ของ", "ตะกร้าใส่ของ",
    "ของเล่นเด็ก", "ของแต่งบ้าน", "กล่องเก็บสินค้า", "ชั้นวางสินค้า",
    "รองเท้าวิ่ง", "รองเท้าเด็ก", "โต๊ะวางคอม", "เครื่องซีลถุง",
    "เครื่องชั่งดิจิทัล", "ขวดใส่น้ำ", "อะไหล่รถยนต์", "อุปกรณ์สำนักงาน",
    "ของเล่น", "ชั้นเก็บสินค้า", "กระเป๋า",
]
_UNITS = ["ชิ้น", "คู่", "กล่อง", "ลัง", "ขวด", "ชุด", "แพ็ค", "พาเลท", "ใบ", "อัน"]


def _opener_variants(noun, unit="ชิ้น", qty=30):
    """The realistic ways one customer turn can carry this noun."""
    return [
        (f"อยากนำเข้า{noun}", "no quantity"),
        (f"{qty} {unit} อยากนำเข้า{noun}", "quantity first, spaced"),
        (f"{qty}{unit}อยากนำเข้า{noun}", "quantity first, no spaces"),
        (f"อยากนำเข้า{noun} {qty} {unit}", "quantity after, spaced"),
        (f"อยากนำเข้า{noun}{qty}{unit}", "quantity after, no spaces"),
        (f"อยากนำเข้า{noun}ค่ะ", "polite particle"),
        (f"อยากสั่ง{noun}จากจีน", "bare order verb + origin"),
        (f"สนใจนำเข้า{noun}ครับ", "interest marker + particle"),
    ]


class TestProductNounAdversarial(unittest.TestCase):
    """PRODUCT-NOUN TRUNCATION = 0 and QUANTITY COLLISION = 0 across the
    whole generated matrix, in BOTH product-noun extractors."""

    def test_fresh_opener_matrix_never_truncates_or_collides(self):
        bad = []
        checked = 0
        for noun in _NOUNS:
            for msg, label in _opener_variants(noun):
                checked += 1
                got = _product_interest_noun(msg)
                if got != noun:
                    bad.append(f"[{label}] {msg!r} -> {got!r} (want {noun!r})")
        self.assertGreaterEqual(checked, 150, "adversarial set must be >= 150 cases")
        self.assertEqual(bad, [], f"{len(bad)}/{checked} truncated or mis-captured:\n"
                                  + "\n".join(bad[:25]))

    def test_every_unit_with_every_noun_keeps_slots_separate(self):
        bad = []
        checked = 0
        for noun in _NOUNS[:10]:
            for unit in _UNITS:
                checked += 1
                msg = f"20 {unit} อยากนำเข้า{noun}"
                fam, conf, ent = _compose(msg)
                if ent.get("product") != noun or ent.get("quantity") != 20:
                    bad.append(f"{msg!r} -> product={ent.get('product')!r} "
                               f"qty={ent.get('quantity')!r}")
        self.assertEqual(bad, [], f"{len(bad)}/{checked} slot collisions:\n"
                                  + "\n".join(bad[:25]))

    def test_bare_slot_reply_extractor_agrees_with_opener_extractor(self):
        """The two extractors must not disagree about the same noun."""
        bad = []
        for noun in _NOUNS:
            for msg in (noun, f"เป็น{noun}", f"{noun}ค่ะ", f"เป็น{noun}ครับ"):
                got = _bare_product_noun(msg)
                if got != noun:
                    bad.append(f"{msg!r} -> {got!r} (want {noun!r})")
        self.assertEqual(bad, [], "bare-reply extractor truncated:\n" + "\n".join(bad[:25]))

    def test_typo_and_noisy_variants_do_not_truncate(self):
        # PHASE-6D convention: no fuzzy-fixing at intent time, so the
        # assertion is only that a typo never CORRUPTS the captured noun
        # into a truncated form -- it may legitimately capture the typo.
        for noun in ["ชั้นวางของ", "กล่องใส่ของ", "ของเล่นเด็ก"]:
            for noisy in (noun + "ๆ", " " + noun + " "):
                got = _product_interest_noun(f"อยากนำเข้า{noisy}")
                self.assertIsNotNone(got, f"{noisy!r} lost the product entirely")
                stripped = re.sub(r"\s+", "", got)
                self.assertGreaterEqual(
                    len(stripped), len(re.sub(r"\s+", "", noun)),
                    f"{noisy!r} -> {got!r} looks truncated")

    def test_bare_placeholder_still_means_no_product(self):
        for msg in ("อยากสั่งของจากจีน", "อยากได้สินค้าค่ะ"):
            self.assertIsNone(_product_interest_noun(msg), msg)
        self.assertIsNone(_bare_product_noun("เป็นของครับ"))
        self.assertIsNone(_bare_product_noun("สินค้าค่ะ"))

    def test_no_length_or_phrase_heuristic_remains(self):
        """The mechanism must be structural, not tuned. Guards against a
        regression back to 'strip when the remnant is long enough'."""
        import services.conversation_semantics as cs
        self.assertFalse(hasattr(cs, "_MIN_QUALIFIED_CONTAINER_LEN"),
                         "length-threshold heuristic reintroduced")
        self.assertFalse(hasattr(cs, "_BARE_PRODUCT_CONTAINER_SUFFIX_RE"),
                         "phrase-specific container-suffix strip reintroduced")


# ══════════════════════════════════════════════════════════════════
# I. RESPONSE / NEXT-ACTION CONSISTENCY
# ══════════════════════════════════════════════════════════════════
_ASK_RE = {
    "product": re.compile(r"แจ้งชื่อ\S{0,6}สินค้า|ประเภทสินค้า|สินค้าอะไร|สินค้าชนิดไหน"),
    "quantity": re.compile(r"แจ้งจำนวน|จำนวนโดยประมาณ|กี่ชิ้น|จำนวนเท่าไหร่"),
    "method": re.compile(r"ทางรถหรือทางเรือ|เลือกวิธีขนส่ง|ขนส่งแบบไหน"),
}


class TestAcknowledgedSlotCannotBeRequested(unittest.TestCase):
    """ACKNOWLEDGED_SLOT_CANNOT_BE_REQUESTED — the exact contradiction
    from the owner's original repro ("รับทราบ 20 คู่" then "รบกวนแจ้ง
    จำนวน") must be impossible for every slot, not just quantity."""

    def test_known_quantity_never_reasked(self):
        r = frame_ack_reply(Frame(product="รองเท้า", quantity=20), changed="none")
        self.assertIn("20", r)
        self.assertIsNone(_ASK_RE["quantity"].search(r), r)

    def test_known_product_never_reasked(self):
        r = frame_ack_reply(Frame(product="ชั้นวางของ"), changed="none")
        self.assertIn("ชั้นวางของ", r)
        self.assertIsNone(_ASK_RE["product"].search(r), r)

    def test_known_method_never_reasked(self):
        r = frame_ack_reply(Frame(product="รองเท้า", quantity=20, method="sea"),
                            changed="none")
        self.assertIsNone(_ASK_RE["method"].search(r), r)

    def test_every_known_combination_only_asks_unknown_slots(self):
        bad = []
        for product in (None, "ชั้นวางของ"):
            for quantity in (None, 20):
                for method in (None, "sea"):
                    r = frame_ack_reply(Frame(product=product, quantity=quantity,
                                              method=method), changed="none")
                    known = {"product": product, "quantity": quantity, "method": method}
                    for slot, val in known.items():
                        if val and _ASK_RE[slot].search(r):
                            bad.append(f"known {slot}={val!r} but reply re-asks it: {r!r}")
        self.assertEqual(bad, [], "\n".join(bad))


class TestCurrentTurnEntityCannotBeDropped(unittest.TestCase):
    """Anything the customer states in the CURRENT turn must reach the
    frame that plans the reply."""

    def test_product_quantity_method_all_survive_one_turn(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ")
        self.assertEqual(ent.get("product"), "รองเท้า")
        self.assertEqual(ent.get("quantity"), 30)
        self.assertEqual(ent.get("method"), "sea")

    def test_stated_slots_are_echoed_not_reasked(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ")
        r = frame_ack_reply(Frame(product=ent.get("product"),
                                  quantity=ent.get("quantity"),
                                  method=ent.get("method")), changed="none")
        for slot in ("product", "quantity", "method"):
            self.assertIsNone(_ASK_RE[slot].search(r), f"{slot} re-asked in {r!r}")


# ══════════════════════════════════════════════════════════════════
# F. MULTI-INTENT CONSUMPTION
# ══════════════════════════════════════════════════════════════════
class TestMultiIntentQuestionNotDropped(unittest.TestCase):
    """customer_uat CUS-G04 / CUS-P07: a PUBLIC question asked alongside
    a workflow trigger must be answered in the same turn."""

    BASIS_ASKED = [
        "ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย",
        "ค่าขนส่งคำนวณอย่างไรคะ ช่วยคิดให้หน่อย",
        "ราคาคิดจากอะไร ประเมินให้หน่อยค่ะ",
        "ค่าส่งคิดยังไงครับ",
    ]

    def test_basis_question_is_recognised(self):
        for m in self.BASIS_ASKED:
            self.assertTrue(asks_rate_basis(m), m)

    def test_pure_calculation_request_is_not_a_basis_question(self):
        for m in ("คำนวนค่าส่งให้หน่อย", "ช่วยประเมินค่าขนส่ง", "20 กิโล ทางเรือ"):
            self.assertFalse(asks_rate_basis(m), m)

    def test_basis_answer_uses_the_single_rate_source(self):
        a = rate_basis_answer()
        for method in ("road", "sea"):
            for basis in ("kg", "cbm"):
                self.assertIn(f"{RATES[method][basis]:g}", a,
                              f"{method}/{basis} rate missing from the basis answer")

    def test_cold_estimate_turn_answers_before_asking(self):
        """A first estimate turn with nothing known must not be a bare
        slot request -- it must carry the public rate truth too."""
        for m in ("ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย",
                  "คิดค่านำเข้าให้หน่อย / คำนวนค่าขนส่ง"):
            st = derive_estimate_state(None, m, interpretation=None)
            self.assertIsNotNone(st, m)
            extract_estimate_fields(m, st)
            reply = estimate_missing_prompt(st, basis_question=asks_rate_basis(m))
            self.assertIn("น้ำหนัก", reply)          # still collects
            self.assertIn("บาท/กก.", reply)          # and answers
            self.assertIn(f"{RATES['sea']['cbm']:g}", reply)

    def test_partial_state_turn_still_acknowledges_what_is_known(self):
        msg = "คำนวนค่าส่งให้หน่อย น้ำหนัก 20 กิโล"
        st = derive_estimate_state(None, msg, interpretation=None)
        self.assertIsNotNone(st)
        extract_estimate_fields(msg, st)
        reply = estimate_missing_prompt(st, basis_question=False)
        self.assertIn("รับทราบ", reply)


if __name__ == "__main__":
    unittest.main()
