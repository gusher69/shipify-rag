"""Regression tests for Unified Intent Classification
(rag/query_understanding.py::classify_actionable_intent()) — consolidates
the existing broad intent classifier (detect_intent, unchanged) and the
narrow purpose-boost classifier (rag/intent_classifier.py, unchanged,
untouched by this module) with a new, precise `actionable_intent` for
answer/attachment planning.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_understanding import classify_actionable_intent, ACTIONABLE_INTENTS
from rag.query_resolution import extract_entities


def _classify(question, carried_entities=None):
    """`carried_entities` simulates conversation-carried state — current
    wording always wins (matches services/playground_orchestrator.py's
    own merge order), so it only fills SLOTS the current text itself
    doesn't specify."""
    current = extract_entities(question)
    merged = dict(carried_entities or {})
    for key, value in current.items():
        if value:
            merged[key] = value
    return classify_actionable_intent(question, merged)


class TestDocumentedExamples(unittest.TestCase):
    def test_warehouse_location(self):
        r = _classify("ขอที่อยู่โกดังไทย")
        self.assertEqual(r["actionable_intent"], "warehouse_location")
        self.assertEqual(r["entities"]["location"], "ไทย")

    def test_warehouse_map(self):
        r = _classify("มีแผนที่ไหม", {"topic": "โกดัง"})
        self.assertEqual(r["actionable_intent"], "warehouse_map")

    def test_warehouse_contact(self):
        r = _classify("ขอเบอร์โกดัง")
        self.assertEqual(r["actionable_intent"], "warehouse_contact")

    def test_shipping_rate(self):
        r = _classify("เรททางเรือเท่าไหร่")
        self.assertEqual(r["actionable_intent"], "shipping_rate")

    def test_shipping_duration(self):
        r = _classify("กี่วัน", {"topic": "เรท", "transport": "รถ"})
        self.assertEqual(r["actionable_intent"], "shipping_duration")

    def test_payment_instruction(self):
        r = _classify("จ่ายบิลยังไง")
        self.assertEqual(r["actionable_intent"], "payment_instruction")

    def test_payment_policy(self):
        r = _classify("ใช้บัตรเครดิตได้ไหม")
        self.assertEqual(r["actionable_intent"], "payment_policy")
        self.assertEqual(r["entities"]["payment_method"], "credit_card")

    def test_attachment_request(self):
        r = _classify("ขอรูปขั้นตอน")
        self.assertEqual(r["actionable_intent"], "attachment_request")


class TestOutputShape(unittest.TestCase):
    def test_all_actionable_intents_are_from_the_supported_set(self):
        for q in ("ขอที่อยู่โกดังไทย", "มีแผนที่ไหม", "จ่ายบิลยังไง", "สวัสดีค่ะ"):
            r = _classify(q)
            self.assertIn(r["actionable_intent"], ACTIONABLE_INTENTS)

    def test_broad_intent_still_present_unchanged(self):
        r = _classify("ขอที่อยู่โกดังไทย")
        self.assertIn("broad_intent", r)

    def test_requested_attributes_present(self):
        r = _classify("ขอเบอร์โกดัง")
        self.assertEqual(r["requested_attributes"], ["phone"])

    def test_uncertain_input_returns_unknown_with_zero_confidence(self):
        r = _classify("สวัสดีค่ะ วันนี้อากาศดีมาก")
        self.assertEqual(r["actionable_intent"], "unknown")
        self.assertEqual(r["confidence"], 0.0)


class TestCurrentQuestionPriority(unittest.TestCase):
    def test_current_wording_wins_over_carried_entities(self):
        """Even if a carried entity says location=ไทย, the current
        question's own "จีน" mention must win."""
        r = _classify("แล้วจีน", {"topic": "โกดัง", "location": "ไทย"})
        # extract_entities("แล้วจีน") itself finds location="จีน" — current wins.
        self.assertEqual(r["entities"]["location"], "จีน")


if __name__ == "__main__":
    unittest.main()
