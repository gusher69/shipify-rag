# -*- coding: utf-8 -*-
"""PHASE 6 CUSTOMER MASTER PASS — general delivery-capability/coverage
question must reach RAG, not a private customer-code request.

Source (raw, read from docs/customer-service/source/ this pass; also
present at docs/customer_uat_sources/"ปัญหาที่เจอในการตอบ (1).xlsx",
sheet "สรุปการเทรน AI", row 10):

    customer: จัดส่งสินค้าถึงหน้าบ้านเลยไหม
    current (production, real LLM tier): กรุณาแจ้งรหัสลูกค้าค่ะ
    expected (the CS team's own confirmed correct answer):
        ใช่ค่ะ บริการจัดส่งสินค้าถึงหน้าบ้านเลยนะคะ ...

ROOT CAUSE (traced end to end, never assumed): the deterministic
recognizers involved (_classify_private_state_inquiry in
services/decision_engine.py; the deterministic branches of
services/conversation_semantics.py::_compose) do NOT fire for this
message at all -- proven directly by calling both offline. The bug is
in services/decision_engine.py's SEMANTIC-FIRST-2 synthesis block: when
the gated LLM family call (semantic.intent_family) names SHIPMENT_STATUS
for ANY conversational Thai turn the deterministic tier could not
resolve, a private_state_inquiry is synthesized from that name alone
(one exclusion existed: _TRANSIT_TIME_Q_RE for "กี่วัน"-style duration
questions). "จัดส่งสินค้าถึงหน้าบ้านเลยไหม" asks whether the SERVICE covers
a destination type AT ALL (no ownership marker, no identifier) -- a
public FAQ, not a question about the customer's OWN shipment -- but the
real production LLM tier evidently misclassifies this shape as
SHIPMENT_STATUS (this file cannot force that live classification
offline; it simulates the exact reported production outcome the same
way the existing tests/test_semantic_first_2.py suite already does, by
patching services.conversation_semantics._llm_family directly).

FIX: a second exclusion, _DELIVERY_CAPABILITY_Q_RE, guards the same
synthesis block -- symmetric with _TRANSIT_TIME_Q_RE, and itself gated
off whenever the message ALSO carries an ownership marker (so a genuine
"ของผมจะถึงบ้านไหม" private status question is unaffected).
"""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-phase6delivery")

from services.decision_engine import (
    _classify_private_state_inquiry, _DELIVERY_CAPABILITY_Q_RE,
)
from services.conversation_semantics import interpret
from tests.test_semantic_first_2 import _mk_engine, _decide


def _llm_shipment_status(message, history=None):
    # simulates the reported real production LLM classification for
    # this exact shape -- a delivery-CAPABILITY question, not a status
    # question about an owned record.
    return {"family": "SHIPMENT_STATUS", "is_private": True}


class TestDeterministicTierDoesNotFireEither(unittest.TestCase):
    """Prove the root cause first: neither deterministic recognizer
    involved (private-state classifier, central interpreter's own
    deterministic branches) names this a private inquiry on its own --
    the misclassification is confined to the gated LLM synthesis path."""

    def test_private_state_classifier_returns_none(self):
        self.assertIsNone(_classify_private_state_inquiry("จัดส่งสินค้าถึงหน้าบ้านเลยไหม"))

    def test_central_interpreter_deterministic_tier_is_not_shipment_status(self):
        # module-level OPENAI_API_KEY=sk-invalid-... (see top of file)
        # already forces _llm_family's own internal call to degrade to
        # UNKNOWN via a real (401) AuthenticationError -- this asserts
        # the DETERMINISTIC _compose() branches alone never reach
        # SHIPMENT_STATUS for this message, isolating where the real
        # misclassification actually happens (the gated LLM call).
        r = interpret("จัดส่งสินค้าถึงหน้าบ้านเลยไหม", [])
        self.assertNotEqual(r.intent_family, "SHIPMENT_STATUS")


class TestDeliveryCapabilityExclusion(unittest.TestCase):
    EXACT = "จัดส่งสินค้าถึงหน้าบ้านเลยไหม"
    PARAPHRASES = [
        "ส่งถึงคอนโดได้ไหมคะ",
        "จัดส่งต่างจังหวัดได้ไหมครับ",
        "ส่งถึงที่ทำงานได้มั้ย",
    ]
    GENUINE_PRIVATE = [
        "ของผมจะถึงบ้านหรือยังคะ",
        "บิลผมส่งถึงหน้าบ้านหรือยัง",
    ]

    def test_regex_matches_the_exact_case_and_paraphrases(self):
        for m in [self.EXACT] + self.PARAPHRASES:
            with self.subTest(m=m):
                self.assertTrue(_DELIVERY_CAPABILITY_Q_RE.search(m), m)

    def test_regex_does_not_fire_on_genuine_ownership_wording(self):
        # the exclusion itself only looks at destination-type wording;
        # the caller additionally requires NO ownership marker, tested
        # end-to-end below.
        for m in self.GENUINE_PRIVATE:
            with self.subTest(m=m):
                self.assertFalse(_DELIVERY_CAPABILITY_Q_RE.search(m), m)

    def setUp(self):
        self.eng = _mk_engine()

    def test_exact_case_reaches_rag_not_customer_code(self):
        o = _decide(self.eng, self.EXACT, llm=_llm_shipment_status)
        self.assertNotIn("รหัสลูกค้า", o["reply"])
        self.assertFalse(o["synth"], "must not synthesize a private_state_inquiry")

    def test_paraphrases_reach_rag_not_customer_code(self):
        for m in self.PARAPHRASES:
            with self.subTest(m=m):
                o = _decide(self.eng, m, llm=_llm_shipment_status)
                self.assertNotIn("รหัสลูกค้า", o["reply"])
                self.assertFalse(o["synth"])

    def test_genuine_private_status_with_ownership_marker_still_synthesizes(self):
        # the guard must not overcorrect: a real "MY shipment, is it home
        # yet" question is still a private inquiry when the LLM (or a
        # future deterministic rule) names it SHIPMENT_STATUS.
        for m in self.GENUINE_PRIVATE:
            with self.subTest(m=m):
                o = _decide(self.eng, m, llm=_llm_shipment_status)
                self.assertTrue(o["synth"], f"{m!r} should still synthesize a private inquiry")


if __name__ == "__main__":
    unittest.main()
