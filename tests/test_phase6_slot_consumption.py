# -*- coding: utf-8 -*-
"""PHASE 6 OWNER UAT — KNOWN SLOT / NEXT-MISSING-SLOT CONSISTENCY.

Real LINE OWNER_TEST repro (production SHA 495e0ce...):

    user: 20 คู่อยากสั่งของจากจีน
    bad:  ได้ค่ะ รับทราบว่าต้องการนำเข้า20คู่นะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ

ROOT CAUSE (traced end to end, see reports/customer_feedback_master_inventory.json
records PHASE6-SLOT-01..05): a quantity+unit span ("20 คู่") had no
recognizer of its own in the IMPORT_INTEREST opener path — it either got
returned AS the product noun (services/playground_orchestrator.py::
_product_interest_noun(), when nothing else survived the generic filler
strip) or glued onto a real product noun. Separately, services/
service_intent_flow.py::import_interest_reply() built a brand-new Frame
from ONLY the (corrupted) product guess, discarding any quantity/method
the canonical entity extraction had already found; and services/
conversation_semantics.py::frame_ack_reply()'s ask-priority cascade never
checked `frame.product` at all, so quantity was always asked first
regardless of which slot was genuinely missing.

CENTRAL FIX (never a per-phrase patch):
  1. A quantity+unit span (reusing the SAME _USER_QTY_RE vocabulary
     already used for a bare quantity ANSWER) is stripped from the text
     BEFORE the product-noun remnant is computed, and is captured as its
     own `quantity` entity — never returned as / glued onto a product.
  2. The same treatment for a shipping-method mention (_METHOD_WORD_RE),
     now broadened to include the bare "ส่งเรือ/ส่งรถ" phrasing alongside
     "ทางเรือ/ทางรถ".
  3. `_is_product_import_interest()`'s ordering-verb recognizer now
     accepts a bare "สั่ง<product noun>" opener (previously required the
     verb glued to a generic "ของ/สินค้า" placeholder), matching what
     the filler-strip regex already treated as an ordering verb.
  4. `import_interest_reply()` threads quantity/method through to the
     Frame it builds, and fires its acknowledgement whenever ANY slot
     (product OR quantity OR method) is known -- never only product.
  5. `frame_ack_reply()`'s ask-priority cascade checks product FIRST,
     ahead of quantity/method/weight.

PHASE-6 CUSTOMER MASTER PASS follow-up (owner UAT completion round) --
the compound-product-noun truncation gap above ("ชั้นวางของ" -> "ชั้นวาง")
IS fixed here, generally: _FIX23_STRIP_RE / _BARE_PRODUCT_STRIP_RE no
longer strip "สินค้า"/"ของ"/"ใส่ของ" unconditionally -- both are also
legitimate word-formants inside a real compound noun, and Thai script has
no spaces to disambiguate "the generic placeholder" from "the same
syllables sitting inside a real product name" by position alone. The new
rule: "สินค้า"/"ของ" are treated as a placeholder (no product named) ONLY
when the ENTIRE remnant, after every OTHER filler word is stripped, is
exactly that bare word with nothing else left -- never when they are part
of a longer surviving span, whether as prefix, suffix, or infix. See
TestCompoundProductNounProtection below.
"""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-phase6slot")

from services.conversation_semantics import (
    _compose, Frame, frame_ack_reply, _USER_QTY_RE, _METHOD_WORD_RE,
    _bare_product_noun,
)
from services.playground_orchestrator import (
    _is_product_import_interest, _product_interest_noun,
)
from services.service_intent_flow import import_interest_reply


def _engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


_ENGINE = _engine()


def _reply(msg, history=None):
    from tests.phase6c_lab.harness import WebhookConversation
    c = WebhookConversation(engine=_ENGINE, mode="deterministic",
                            rag_answer="[RAG-GENERIC-IMPORT-ADVICE]", rag_conf=0.55)
    if history:
        c.seed_history(list(history))
    return c.send(msg)


# ── A. exact owner repro ──────────────────────────────────────────────
class TestExactOwnerRepro(unittest.TestCase):
    def test_quantity_first_opener_asks_product_not_quantity(self):
        r = _reply("20 คู่อยากสั่งของจากจีน")
        self.assertIn("20", r["reply"])
        self.assertIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณ", r["reply"])
        self.assertNotIn("20คู่นะคะ", r["reply"])  # never re-echo qty AS the product


# ── B. entity-extraction unit tests (fast, no full decide()) ──────────
class TestEntityExtractionUnit(unittest.TestCase):
    def test_quantity_only_opener_product_is_none(self):
        fam, conf, ent = _compose("20 คู่อยากสั่งของจากจีน")
        self.assertEqual(fam, "IMPORT_INTEREST")
        self.assertIsNone(ent.get("product"))
        self.assertEqual(ent.get("quantity"), 20)

    def test_product_first_recognized_as_import_interest(self):
        self.assertTrue(_is_product_import_interest("อยากสั่งรองเท้าจากจีน"))
        self.assertEqual(_product_interest_noun("อยากสั่งรองเท้าจากจีน"), "รองเท้า")

    def test_product_then_quantity_both_captured_cleanly(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าจากจีน 20 คู่")
        self.assertEqual(ent.get("product"), "รองเท้า")
        self.assertEqual(ent.get("quantity"), 20)

    def test_quantity_then_product_reordered(self):
        # generalization: quantity BEFORE product, product still isolated
        noun = _product_interest_noun("20 คู่ รองเท้า อยากสั่งจากจีน")
        self.assertEqual(noun, "รองเท้า")

    def test_product_quantity_method_all_in_one_turn(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าจากจีน 20 คู่ ส่งเรือ")
        self.assertEqual(ent.get("product"), "รองเท้า")
        self.assertEqual(ent.get("quantity"), 20)
        self.assertEqual(ent.get("method"), "sea")

    def test_quantity_leading_different_unit_and_product(self):
        fam, conf, ent = _compose("30 ชิ้น อยากนำเข้าโคมไฟ")
        self.assertEqual(ent.get("quantity"), 30)
        self.assertEqual(ent.get("product"), "โคมไฟ")

    def test_bare_method_word_song_form_recognized(self):
        self.assertIsNotNone(_METHOD_WORD_RE.search("ส่งเรือ"))
        self.assertIsNotNone(_METHOD_WORD_RE.search("ส่งรถ"))

    def test_quantity_unit_at_end_of_string_now_matches(self):
        # regression: _USER_QTY_RE previously failed when the unit word
        # (ending in a bare Thai tone mark) was the LAST thing in the
        # string -- a real word-boundary edge case, not a phrase patch.
        m = _USER_QTY_RE.search("อยากสั่งรองเท้าจากจีน 20 คู่")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("q"), "20")


# ── C. frame_ack_reply ask-priority ────────────────────────────────────
class TestAskPriority(unittest.TestCase):
    def test_missing_product_asked_first(self):
        text = frame_ack_reply(Frame(product=None, quantity=20, method=None), changed="none")
        self.assertIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", text)
        self.assertNotIn("จำนวนโดยประมาณด้วยนะคะ", text)  # not re-asking qty

    def test_placeholder_never_leaks_as_product_word(self):
        # the internal placeholder must never appear as if it were itself
        # a confirmed product name a later turn could read back.
        text = frame_ack_reply(Frame(product=None, quantity=20), changed="none")
        self.assertNotIn("ต้องการนำเข้าสินค้า", text)

    def test_product_known_asks_quantity(self):
        text = frame_ack_reply(Frame(product="รองเท้า"), changed="none")
        self.assertIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", text)

    def test_product_and_quantity_known_asks_method(self):
        text = frame_ack_reply(Frame(product="รองเท้า", quantity=20), changed="none")
        self.assertIn("สนใจส่งทางรถหรือทางเรือคะ", text)

    def test_all_three_known_asks_weight_never_reasks_earlier_slots(self):
        text = frame_ack_reply(Frame(product="รองเท้า", quantity=20, method="sea"), changed="none")
        self.assertNotIn("ชื่อหรือประเภทสินค้า", text)
        self.assertNotIn("จำนวนโดยประมาณด้วยนะคะ", text)
        self.assertNotIn("ส่งทางรถหรือทางเรือคะ", text)


# ── D. required Step-4 matrix (A-H, exact owner phrasing) ──────────────
class TestOwnerRequiredMatrix(unittest.TestCase):
    def test_A_quantity_only(self):
        r = _reply("20 คู่อยากสั่งของจากจีน")
        self.assertIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r["reply"])

    def test_B_product_only_asks_quantity(self):
        r = _reply("อยากสั่งรองเท้าจากจีน")
        self.assertIn("รองเท้า", r["reply"])
        self.assertIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r["reply"])

    def test_C_product_and_quantity_neither_reasked(self):
        r = _reply("อยากสั่งรองเท้าจากจีน 20 คู่")
        self.assertIn("รองเท้า", r["reply"])
        self.assertIn("20", r["reply"])
        self.assertNotIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r["reply"])

    def test_D_product_quantity_method_none_reasked(self):
        r = _reply("อยากสั่งรองเท้าจากจีน 20 คู่ ส่งเรือ")
        self.assertIn("รองเท้า", r["reply"])
        self.assertIn("20", r["reply"])
        self.assertIn("เรือ", r["reply"])
        self.assertNotIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r["reply"])
        self.assertNotIn("สนใจส่งทางรถหรือทางเรือคะ", r["reply"])

    def test_F_bare_method_answer_when_requested_not_reasked(self):
        history = [
            {"role": "user", "content": "อยากสั่งรองเท้าจากจีน 20 คู่"},
            {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 ชิ้นนะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"},
        ]
        r = _reply("ส่งทางเรือค่ะ", history=history)
        self.assertNotIn("สนใจส่งทางรถหรือทางเรือคะ", r["reply"])

    def test_G_bare_product_answer_when_requested_not_reasked(self):
        history = [
            {"role": "user", "content": "อยากสั่งของจากจีน"},
            {"role": "assistant", "content": "ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ"},
        ]
        r = _reply("เป็นชั้นวางของ", history=history)
        self.assertNotIn("อยากสั่งสินค้าอะไร", r["reply"])

    def test_H_correction_updates_quantity_not_reasked(self):
        history = [
            {"role": "user", "content": "อยากสั่งรองเท้าจากจีน"},
            {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้านะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"},
            {"role": "user", "content": "20 คู่"},
            {"role": "assistant", "content": "รับทราบค่ะ (สินค้า รองเท้า) จำนวนประมาณ 20 ชิ้น สนใจส่งทางรถหรือทางเรือคะ"},
        ]
        r = _reply("เอ้ย 10 คู่", history=history)
        self.assertIn("10", r["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r["reply"])


# ── E. cross-turn consistency (Step 5) ─────────────────────────────────
class TestCrossTurnConsistency(unittest.TestCase):
    def test_full_sequence_never_reasks_known_slot(self):
        from tests.phase6c_lab.harness import WebhookConversation
        c = WebhookConversation(engine=_ENGINE, mode="deterministic",
                                rag_answer="[RAG-GENERIC-IMPORT-ADVICE]", rag_conf=0.55)
        r1 = c.send("20 คู่อยากสั่งของจากจีน")
        self.assertIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r1["reply"])

        r2 = c.send("เป็นชั้นวางของ")
        self.assertNotIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", r2["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r2["reply"])

        r3 = c.send("เปลี่ยนเป็นรองเท้า")
        self.assertIn("รองเท้า", r3["reply"])
        self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", r3["reply"])


# ── F. generalization matrix (Step 6/8, product x quantity x method) ──
_UNITS = ["ชิ้น", "คู่", "กล่อง", "ลัง", "ขวด", "ชุด", "แพ็ค", "พาเลท"]
_PRODUCTS = ["รองเท้า", "กระเป๋า", "โคมไฟ", "เครื่องทำกาแฟ", "หมวก"]


class TestUnitGeneralization(unittest.TestCase):
    """Every unit in the vocabulary, quantity-first opener: product must
    stay missing (never the quantity+unit itself), quantity must be
    captured correctly. Generalization, not per-unit hardcoding — one
    parametrized loop over the SAME shared regex the runtime uses."""

    def test_every_known_unit_never_becomes_the_product(self):
        for unit in _UNITS:
            msg = f"15 {unit}อยากสั่งของจากจีน"
            with self.subTest(unit=unit):
                fam, conf, ent = _compose(msg)
                self.assertIsNone(ent.get("product"), msg)
                self.assertEqual(ent.get("quantity"), 15, msg)

    def test_every_product_with_trailing_quantity_separated(self):
        for product in _PRODUCTS:
            msg = f"อยากสั่ง{product}จากจีน 12 ชิ้น"
            with self.subTest(product=product):
                fam, conf, ent = _compose(msg)
                self.assertEqual(ent.get("product"), product, msg)
                self.assertEqual(ent.get("quantity"), 12, msg)

    def test_every_product_leading_quantity_separated(self):
        for product in _PRODUCTS:
            msg = f"12 ชิ้น อยากนำเข้า{product}"
            with self.subTest(product=product):
                fam, conf, ent = _compose(msg)
                self.assertEqual(ent.get("product"), product, msg)
                self.assertEqual(ent.get("quantity"), 12, msg)


# ── H. compound-product-noun protection (owner UAT completion round) ──
# Real Thai compound nouns that contain "สินค้า"/"ของ" as a prefix,
# suffix, or infix -- exactly the class of case the original commit
# left as a known, deferred gap. Tested via BOTH independent product-
# noun extractors the runtime has (Step 5's split-brain audit found
# two): _product_interest_noun (fresh IMPORT_INTEREST opener) and
# _bare_product_noun (a bare reply to "which product?", FIX-04/05/06).
_COMPOUND_NOUNS = [
    "ชั้นวางของ", "กล่องใส่ของ", "รองเท้าวิ่ง", "เครื่องซีลถุง",
    "ขวดใส่น้ำ", "อะไหล่รถยนต์", "โต๊ะวางคอม", "ชั้นเก็บสินค้า",
]


class TestCompoundProductNounProtection(unittest.TestCase):
    def test_fresh_opener_quantity_before_no_truncation(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_product_interest_noun(f"30 ชิ้น อยากนำเข้า{noun}"), noun)

    def test_fresh_opener_quantity_after_no_truncation(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_product_interest_noun(f"อยากนำเข้า{noun} 30 ชิ้น"), noun)

    def test_fresh_opener_no_quantity_no_truncation(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_product_interest_noun(f"อยากนำเข้า{noun}"), noun)

    def test_fresh_opener_no_space_between_quantity_and_noun(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_product_interest_noun(f"30ชิ้นอยากนำเข้า{noun}"), noun)

    def test_fresh_opener_polite_particle_no_truncation(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_product_interest_noun(f"อยากนำเข้า{noun}ค่ะ"), noun)

    def test_compose_end_to_end_product_entity_intact(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                fam, conf, ent = _compose(f"20 ชิ้น อยากสั่ง{noun}จากจีน")
                self.assertEqual(ent.get("product"), noun)
                self.assertEqual(ent.get("quantity"), 20)

    def test_bare_reply_to_which_product_question_no_truncation(self):
        for noun in _COMPOUND_NOUNS:
            with self.subTest(noun=noun):
                self.assertEqual(_bare_product_noun(f"เป็น{noun}"), noun)
                self.assertEqual(_bare_product_noun(f"{noun}ค่ะ"), noun)

    def test_placeholder_alone_still_yields_no_product(self):
        self.assertIsNone(_product_interest_noun("อยากสั่งของจากจีน"))
        self.assertIsNone(_product_interest_noun("อยากได้สินค้าค่ะ"))
        self.assertIsNone(_bare_product_noun("เป็นของครับ"))

    def test_existing_protected_redundant_container_phrase_unchanged(self):
        # tests/test_invoice_product_regression2.py's own confirmed case:
        # "ใส่ของ" IS stripped as descriptive padding when the noun in
        # front of it already fully names the product on its own.
        self.assertEqual(_bare_product_noun("เป็นพวกกล่องพลาสติกใส่ของครับ"), "กล่องพลาสติก")


# ── G. typo / noisy-Thai tolerance (Step 4/6) ──────────────────────────
class TestTypoNoisyTolerance(unittest.TestCase):
    def test_no_space_between_quantity_and_unit(self):
        fam, conf, ent = _compose("20คู่อยากสั่งของจากจีน")
        self.assertIsNone(ent.get("product"))
        self.assertEqual(ent.get("quantity"), 20)

    def test_polite_particle_after_product(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าค่ะจากจีน 20 คู่ค่ะ")
        self.assertEqual(ent.get("quantity"), 20)


# ── I. split-brain audit (Step 4/5/13) — P1 canonical resolver must not
# disagree with the legacy runtime on quantity+unit vocabulary ─────────
class TestP1QuantityUnitVocabularySync(unittest.TestCase):
    """Found via direct comparison, not assumed: services/
    conversation_resolution.py's OWN quantity+unit regex (_QTY_RE, P1's
    canonical entity extraction) had quietly drifted out of sync with
    services/conversation_semantics.py::_USER_QTY_RE (the legacy runtime's
    vocabulary) -- "ขวด"/"พาเลท" were recognized by the legacy runtime but
    invisible to P1. P1 is shadow-only (never read-authoritative), so this
    could not change customer-visible behaviour, but it WOULD have shown
    up as a false STRUCTURED_WRONG in the P2.1 frame_parity telemetry the
    moment a real customer used either unit."""

    def test_qty_re_recognizes_every_legacy_unit(self):
        from services.conversation_resolution import _QTY_RE
        from services.conversation_semantics import _USER_QTY_RE
        import re as _re
        legacy_units = _re.search(r"\(ตัว\|.+?\)", _USER_QTY_RE.pattern).group(0)[1:-1].split("|")
        for unit in legacy_units:
            if unit in ("pcs?",):
                continue  # regex alternative, not a literal unit string
            msg = f"20 {unit}อยากสั่งของจากจีน"
            with self.subTest(unit=unit):
                self.assertIsNotNone(_QTY_RE.search(msg), f"P1 _QTY_RE missed unit {unit!r}")

    def test_end_to_end_resolution_captures_bottle_and_pallet_quantity(self):
        from services.conversation_resolution import resolve_conversation
        for msg, qty in [("20 ขวดอยากสั่งของจากจีน", 20), ("5 พาเลทอยากนำเข้าสินค้า", 5)]:
            with self.subTest(msg=msg):
                r = resolve_conversation(msg, history=None)
                self.assertEqual(r.entities.get("quantity").value, qty)


if __name__ == "__main__":
    unittest.main()
