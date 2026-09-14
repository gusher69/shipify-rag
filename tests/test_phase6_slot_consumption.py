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

Known, separately-tracked, NOT-fixed-here gap: a generic filler particle
embedded at the TAIL of a real compound product noun ("ชั้นวางของ") has no
word-boundary protection in the Thai script and gets truncated
("ชั้นวางของ" -> "ชั้นวาง") independent of any quantity involvement. See
the Technical Debt entry in this task's final report -- fixing it
generally requires real Thai word segmentation, not a phrase patch, and
is out of scope for this pass.
"""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-phase6slot")

from services.conversation_semantics import (
    _compose, Frame, frame_ack_reply, _USER_QTY_RE, _METHOD_WORD_RE,
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


# ── G. typo / noisy-Thai tolerance (Step 4/6) ──────────────────────────
class TestTypoNoisyTolerance(unittest.TestCase):
    def test_no_space_between_quantity_and_unit(self):
        fam, conf, ent = _compose("20คู่อยากสั่งของจากจีน")
        self.assertIsNone(ent.get("product"))
        self.assertEqual(ent.get("quantity"), 20)

    def test_polite_particle_after_product(self):
        fam, conf, ent = _compose("อยากสั่งรองเท้าค่ะจากจีน 20 คู่ค่ะ")
        self.assertEqual(ent.get("quantity"), 20)


if __name__ == "__main__":
    unittest.main()
