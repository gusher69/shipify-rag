"""Regression tests for the Clarification State Engine (P0, 2026-07-22):
a short reply to the assistant's own immediately previous clarification
question must resolve against the ORIGINAL user intent — never a
different, unrelated intent pulled in from stale historical entities
(the exact production bug: "ขอเบอร์ติดต่อ" -> clarification -> "ไทย" was
being resolved into "ขอทราบระยะเวลาขนส่งทางรถ").
"""
import unittest

from rag.query_resolution import resolve_conversation
from rag.clarification_state import detect_pending_clarification, resolve_clarification_answer


class TestDetectPendingClarification(unittest.TestCase):
    def test_detects_a_real_clarification_question(self):
        history = [
            {"role": "user", "content": "ขอเบอร์ติดต่อ"},
            {"role": "assistant", "content": "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"},
        ]
        pending = detect_pending_clarification(history)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["original_question"], "ขอเบอร์ติดต่อ")

    def test_a_completed_answer_is_not_a_pending_clarification(self):
        history = [
            {"role": "user", "content": "ขอเบอร์ติดต่อ"},
            {"role": "assistant", "content": "Shipify ฝ่ายบริการลูกค้า: 02-026-6426"},
        ]
        self.assertIsNone(detect_pending_clarification(history))

    def test_no_history_is_not_a_pending_clarification(self):
        self.assertIsNone(detect_pending_clarification(None))
        self.assertIsNone(detect_pending_clarification([]))


class TestClarificationStateEngine(unittest.TestCase):
    """The exact reported production bug plus every reply shape listed
    in the task spec: ไทย/จีน, ใช่/ไม่ใช่, อันแรก/อันที่สอง, ทางรถ/ทางเรือ,
    Shipify/Fasttrade, and a generic bare-entity reply."""

    def _history(self, original_question, clarification_question):
        return [{"role": "user", "content": original_question},
                {"role": "assistant", "content": clarification_question}]

    def test_the_exact_reported_bug_thai_warehouse_contact(self):
        history = self._history("ขอเบอร์ติดต่อ", "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ")
        r = resolve_conversation("ไทย", history)
        self.assertEqual(r["resolved_question"], "ขอเบอร์ติดต่อโกดังไทย")
        self.assertEqual(r["followup_type"], "clarification-answer")
        # Must NOT misfire into an unrelated shipping-duration intent.
        self.assertNotIn("ระยะเวลา", r["resolved_question"])
        self.assertNotIn("ขนส่งทางรถ", r["resolved_question"])

    def test_china_warehouse_contact(self):
        history = self._history("ขอเบอร์ติดต่อ", "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ")
        r = resolve_conversation("จีน", history)
        self.assertEqual(r["resolved_question"], "ขอเบอร์ติดต่อโกดังจีน")

    def test_transport_choice_by_road(self):
        history = self._history("ขอเรทค่าขนส่ง", "ต้องการเรทค่าขนส่งทางรถหรือทางเรือคะ")
        r = resolve_conversation("ทางรถ", history)
        self.assertEqual(r["resolved_question"], "ขอเรทค่าขนส่งทางรถ")

    def test_transport_choice_by_sea(self):
        history = self._history("ขอเรทค่าขนส่ง", "ต้องการเรทค่าขนส่งทางรถหรือทางเรือคะ")
        r = resolve_conversation("ทางเรือ", history)
        self.assertEqual(r["resolved_question"], "ขอเรทค่าขนส่งทางเรือ")

    def test_yes_reply_preserves_original_intent(self):
        history = self._history("ขอยกเลิกออเดอร์", "ต้องการยกเลิกออเดอร์จริงหรือไม่คะ")
        r = resolve_conversation("ใช่", history)
        self.assertTrue(r["resolved_question"].startswith("ขอยกเลิกออเดอร์"))

    def test_no_reply_preserves_original_intent(self):
        history = self._history("ขอยกเลิกออเดอร์", "ต้องการยกเลิกออเดอร์จริงหรือไม่คะ")
        r = resolve_conversation("ไม่ใช่", history)
        self.assertTrue(r["resolved_question"].startswith("ขอยกเลิกออเดอร์"))

    def test_ordinal_choice_first(self):
        history = self._history("เช็คสถานะ", "หมายถึงออเดอร์อันแรกหรืออันที่สองคะ")
        r = resolve_conversation("อันแรก", history)
        self.assertTrue(r["resolved_question"].startswith("เช็คสถานะ"))
        self.assertIn("อันแรก", r["resolved_question"])

    def test_ordinal_choice_second(self):
        history = self._history("เช็คสถานะ", "หมายถึงออเดอร์อันแรกหรืออันที่สองคะ")
        r = resolve_conversation("อันที่สอง", history)
        self.assertTrue(r["resolved_question"].startswith("เช็คสถานะ"))
        self.assertIn("อันที่สอง", r["resolved_question"])

    def test_brand_choice_shipify(self):
        history = self._history("ขอเบอร์ติดต่อ", "ต้องการเบอร์ Shipify หรือ Fasttrade คะ")
        r = resolve_conversation("Shipify", history)
        self.assertTrue(r["resolved_question"].startswith("ขอเบอร์ติดต่อ"))
        self.assertIn("Shipify", r["resolved_question"])

    def test_brand_choice_fasttrade(self):
        history = self._history("ขอเบอร์ติดต่อ", "ต้องการเบอร์ Shipify หรือ Fasttrade คะ")
        r = resolve_conversation("Fasttrade", history)
        self.assertTrue(r["resolved_question"].startswith("ขอเบอร์ติดต่อ"))
        self.assertIn("Fasttrade", r["resolved_question"])

    def test_bare_entity_reply_falls_back_generically(self):
        """A short bare reply not covered by any specific entity pattern
        still preserves the original question rather than being
        discarded or misrouted."""
        history = self._history("ขอราคาสินค้า", "หมายถึงสินค้าตัวไหนคะ")
        r = resolve_conversation("เสื้อยืด", history)
        self.assertTrue(r["resolved_question"].startswith("ขอราคาสินค้า"))


class TestFlowScoping(unittest.TestCase):
    """Part 2 — an entity from an earlier, unrelated, already-completed
    flow must never leak into the resolution of a later clarification
    reply belonging to a completely different flow."""

    def test_no_stale_transport_or_duration_entity_leaks_in(self):
        history = [
            {"role": "user", "content": "ระยะเวลาขนส่งทางรถกี่วัน"},
            {"role": "assistant", "content": "ทางรถใช้เวลาประมาณ 7 วันค่ะ"},
            {"role": "user", "content": "ขอเบอร์ติดต่อ"},
            {"role": "assistant", "content": "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"},
        ]
        r = resolve_conversation("ไทย", history)
        self.assertEqual(r["resolved_question"], "ขอเบอร์ติดต่อโกดังไทย")
        self.assertNotIn("ขนส่ง", r["resolved_question"])
        self.assertNotIn("รถ", r["resolved_question"])
        self.assertNotIn("ระยะเวลา", r["resolved_question"])

    def test_no_stale_location_entity_leaks_in_from_older_flow(self):
        history = [
            {"role": "user", "content": "ขอที่อยู่โกดังจีน"},
            {"role": "assistant", "content": "โกดังจีนอยู่ที่..."},
            {"role": "user", "content": "ขอเรทค่าขนส่ง"},
            {"role": "assistant", "content": "ต้องการเรทค่าขนส่งทางรถหรือทางเรือคะ"},
        ]
        r = resolve_conversation("ทางเรือ", history)
        self.assertEqual(r["resolved_question"], "ขอเรทค่าขนส่งทางเรือ")
        self.assertNotIn("จีน", r["resolved_question"])


class TestExplicitTopicChange(unittest.TestCase):
    """Priority 1 — the user ignoring the clarification to ask something
    completely different must be left untouched, not force-merged into
    the pending clarification's topic."""

    def test_fresh_unrelated_question_bypasses_clarification_resolution(self):
        history = [
            {"role": "user", "content": "ขอเบอร์ติดต่อ"},
            {"role": "assistant", "content": "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"},
        ]
        r = resolve_conversation("ค่าขนส่งทางเรือคิดยังไง", history)
        self.assertEqual(r["resolved_question"], "ค่าขนส่งทางเรือคิดยังไง")
        self.assertNotEqual(r["followup_type"], "clarification-answer")


class TestSelfContainedQuestionIsNotAClarificationReply(unittest.TestCase):
    """Real LINE regression (P8.1, 2026-09-02): A6 "มีขนส่งทางเครื่องบินไหม"
    was answered with a canned FAQ ending "...ต้องการขนส่งสินค้าประเภทไหนคะ".
    A7 "น้ำหอมนำเข้าได้ไหม" — a fully self-contained eligibility question —
    was then mis-resolved as a bare answer to that elicitation, so the
    resolver prepended A6's core ("มีขนส่งทางเครื่องบิน") onto the retrieval
    query, pulling the air-freight FAQ chunk into A7's evidence and
    bleeding "ไม่มีบริการขนส่งทางเครื่องบิน" into the perfume answer.

    Invariant: a message carrying its own interrogative marker
    (ไหม/มั้ย/เท่าไหร่/กี่/ยังไง/หรือเปล่า/?) owns its own answer and is
    never a bare clarification reply — however short, and even if it also
    names a product noun. A genuine bare reply ("รองเท้า") is unaffected.
    """

    _A6_AIR_FAQ_ANSWER = ("ตอนนี้ทางเรามีบริการขนส่งทางรถและทางเรือเท่านั้นค่ะ "
                          "ยังไม่มีบริการขนส่งทางเครื่องบินนะคะ "
                          "คุณลูกค้าต้องการขนส่งสินค้าประเภทไหนคะ")

    def _hist(self, user_q, assistant_a):
        return [{"role": "user", "content": user_q},
                {"role": "assistant", "content": assistant_a}]

    def test_a6_air_freight_then_a7_perfume_does_not_inherit(self):
        history = self._hist("มีขนส่งทางเครื่องบินไหม", self._A6_AIR_FAQ_ANSWER)
        r = resolve_conversation("น้ำหอมนำเข้าได้ไหม", history)
        self.assertEqual(r["resolved_question"], "น้ำหอมนำเข้าได้ไหม")
        self.assertNotEqual(r["followup_type"], "clarification-answer")
        self.assertNotIn("เครื่องบิน", r["resolved_question"])

    def test_reverse_perfume_then_air_freight_does_not_inherit(self):
        history = self._hist(
            "น้ำหอมนำเข้าได้ไหม",
            "น้ำหอมจัดเป็นสินค้าประเภทของเหลว ทางเราไม่สามารถนำเข้าได้ค่ะ มีสินค้าอื่นที่ต้องการให้ช่วยเช็กไหมคะ")
        r = resolve_conversation("มีขนส่งทางเครื่องบินไหม", history)
        self.assertEqual(r["resolved_question"], "มีขนส่งทางเครื่องบินไหม")
        self.assertNotIn("น้ำหอม", r["resolved_question"])

    def test_rate_then_self_contained_product_question_does_not_inherit(self):
        history = self._hist("ค่าขนส่งเท่าไหร่",
                             "ทางรถ 35 บาท/กก. ทางเรือ 19 บาท/กก. ค่ะ")
        r = resolve_conversation("น้ำหอมนำเข้าได้ไหม", history)
        self.assertEqual(r["resolved_question"], "น้ำหอมนำเข้าได้ไหม")

    def test_genuine_bare_product_reply_still_resolves_as_clarification_answer(self):
        history = self._hist("อยากนำเข้าสินค้าจากจีน",
                             "รับทราบค่ะ ต้องการนำเข้าสินค้าประเภทไหนคะ")
        r = resolve_conversation("รองเท้า", history)
        self.assertEqual(r["resolved_question"], "รองเท้านำเข้าได้ไหม")
        self.assertEqual(r["followup_type"], "clarification-answer")


class TestClarificationQuestionWording(unittest.TestCase):
    """The clarification question itself must match what was actually
    asked (phone vs address vs map), not always say "ที่อยู่"."""

    def test_contact_intent_clarification_mentions_phone(self):
        from services.answer_planner import plan_answer
        plan = plan_answer("ขอเบอร์ติดต่อ", "warehouse_contact", entities={})
        self.assertIn("เบอร์ติดต่อ", plan["clarification_question"])

    def test_map_intent_clarification_mentions_map(self):
        from services.answer_planner import plan_answer
        plan = plan_answer("ขอแผนที่โกดัง", "warehouse_map", entities={})
        self.assertIn("แผนที่", plan["clarification_question"])


if __name__ == "__main__":
    unittest.main()
