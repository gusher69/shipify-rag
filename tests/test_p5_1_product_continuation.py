"""P5.1 — a bare product reply to elicit_product_type must CONTINUE the
Shipify service conversation, not answer "ไม่มีข้อมูล".

Unit-level: the two new deterministic helpers in
services/playground_orchestrator.py plus preservation of the standalone
eligibility path. No LLM, no network.
"""
import re
import types
import unittest

from services.playground_orchestrator import (
    _product_answer_continuation_noun, _product_answer_service_continuation,
)
from services.answer_planner import decide_followup


_ELICIT_TURN = {"role": "assistant", "content": "คุณสนใจนำเข้าสินค้าประเภทไหนคะ?"}
_SPEC = types.SimpleNamespace(entities=["รองเท้า"], transport_modes=[], comparison=None,
                              facets=[], sub_questions=[], corrections={})
_SINGLE_ELIG = ("รองเท้า / eligibility", "รองเท้า ...category terms...")


class ContinuationDetection(unittest.TestCase):
    def test_detects_product_answer_after_elicit(self):
        n = _product_answer_continuation_noun("clarification-answer", [_ELICIT_TURN],
                                              _SINGLE_ELIG, _SPEC)
        self.assertEqual(n, "รองเท้า")

    def test_none_when_not_a_clarification_answer(self):
        self.assertIsNone(_product_answer_continuation_noun(
            None, [_ELICIT_TURN], _SINGLE_ELIG, _SPEC))
        self.assertIsNone(_product_answer_continuation_noun(
            "meta-detail-followup", [_ELICIT_TURN], _SINGLE_ELIG, _SPEC))

    def test_none_when_no_recent_elicit_product_type(self):
        other = [{"role": "assistant", "content": "โกดังจีนอยู่ที่กวางโจวค่ะ"}]
        self.assertIsNone(_product_answer_continuation_noun(
            "clarification-answer", other, _SINGLE_ELIG, _SPEC))

    def test_falls_back_to_request_spec_entities(self):
        n = _product_answer_continuation_noun("clarification-answer", [_ELICIT_TURN],
                                              None, _SPEC)
        self.assertEqual(n, "รองเท้า")

    def test_rejects_question_shaped_or_overlong_noun(self):
        bad_spec = types.SimpleNamespace(entities=["รองเท้านำเข้าได้ไหมและมีขั้นตอนยังไงบ้างคะ"],
                                          transport_modes=[], comparison=None,
                                          facets=[], sub_questions=[], corrections={})
        self.assertIsNone(_product_answer_continuation_noun(
            "clarification-answer", [_ELICIT_TURN], None, bad_spec))


class ServiceContinuationReply(unittest.TestCase):
    def _reply(self, noun="รองเท้า", *, lead_stage="COLD", sentiment_status=None,
              history=None, transport_known=False):
        return _product_answer_service_continuation(
            noun, lead_stage=lead_stage, sentiment_status=sentiment_status,
            history=history or [], transport_known=transport_known)

    def test_never_says_no_information(self):
        for kw in ({}, {"lead_stage": "WARM"}, {"sentiment_status": "NEGATIVE"},
                   {"lead_stage": "HOT"}, {"transport_known": True}):
            txt, _ = self._reply(**kw)
            self.assertNotIn("ไม่มีข้อมูล", txt)
            self.assertNotIn("ยังไม่มีข้อมูลยืนยัน", txt)

    def test_acknowledges_the_product(self):
        txt, _ = self._reply("รองเท้า")
        self.assertIn("รองเท้า", txt)

    # A / Example 1 — COLD/WARM, transport unknown -> one transport question
    def test_offers_one_transport_question(self):
        txt, note = self._reply(lead_stage="WARM")
        self.assertEqual(note, "appended:elicit_transport_mode")
        self.assertIn("ทางรถหรือทางเรือ", txt)
        self.assertEqual(txt.count("?") + txt.count("คะ\n"), txt.count("?"))  # sanity
        self.assertLessEqual(len(re.findall(r"ไหมคะ|หรือทางเรือคะ|ประเภทไหนคะ", txt)), 1)

    # G — NEGATIVE: acknowledge only, no service guidance, no question
    def test_negative_is_acknowledge_only(self):
        txt, note = self._reply(sentiment_status="NEGATIVE")
        self.assertEqual(note, "negative-ack-only")
        self.assertNotIn("ทางรถหรือทางเรือ", txt)
        self.assertNotIn("ขนส่งทั้งทางรถและทางเรือ", txt)

    # HOT safety — no exploratory transport question
    def test_hot_gets_service_fact_but_no_question(self):
        txt, note = self._reply(lead_stage="HOT")
        self.assertEqual(note, "ack-service-only")
        self.assertNotIn("สนใจส่งทางรถหรือทางเรือคะ", txt)

    # B — transport already known -> no repeat
    def test_transport_known_no_question(self):
        _, note = self._reply(lead_stage="WARM", transport_known=True)
        self.assertEqual(note, "ack-service-only")

    def test_recently_asked_transport_no_repeat(self):
        hist = [{"role": "assistant", "content": "สนใจส่งทางรถหรือทางเรือคะ"}]
        _, note = self._reply(lead_stage="WARM", history=hist)
        self.assertEqual(note, "ack-service-only")

    # D — a prohibited-category answer ("ของเหลวครับ") gets no transport nudge
    def test_prohibited_category_noun_no_transport_question(self):
        _, note = self._reply("ของเหลว", lead_stage="WARM")
        self.assertEqual(note, "ack-service-only")

    def test_at_most_one_followup_question(self):
        txt, _ = self._reply(lead_stage="WARM")
        self.assertLessEqual(txt.count("คะ?"), 1)
        self.assertLessEqual(txt.count("?"), 1)


class StandaloneEligibilityUnchanged(unittest.TestCase):
    """Branch C/F — an EXPLICIT eligibility question is not a product-answer
    continuation and keeps its existing grounded/unconfirmed behavior."""

    def test_standalone_is_not_a_continuation(self):
        # no preceding elicit_product_type turn
        self.assertIsNone(_product_answer_continuation_noun(
            None, [{"role": "user", "content": "รองเท้านำเข้าได้ไหม"}], _SINGLE_ELIG, _SPEC))

    def test_prohibited_still_offers_alternative_when_warm(self):
        r = decide_followup("prohibited_goods", _SPEC, "น้ำหอมนำเข้าได้ไหม", [], "answerable",
                            None, lead_stage="WARM", entities={"topic": "น้ำหอม"})
        self.assertEqual(r["purpose"], "offer_alternative_product")

    def test_prohibited_still_offers_alternative_stage_less(self):
        r = decide_followup("prohibited_goods", _SPEC, "น้ำหอมนำเข้าได้ไหม", [], "answerable", None)
        self.assertEqual(r["purpose"], "offer_alternative_product")


if __name__ == "__main__":
    unittest.main()
