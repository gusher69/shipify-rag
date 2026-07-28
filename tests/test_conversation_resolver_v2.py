"""Regression tests for Conversation Resolver 2.0 (rag/query_resolution.py
::resolve_conversation()) — lightweight conversation entity tracking
(topic/location/transport/attribute) extracted from prior USER turns
only, supporting a much wider range of short follow-ups than the
original "แล้ว...ล่ะ"-only pattern.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_resolution import resolve_conversation


def _hist(*pairs):
    history = []
    for u, a in pairs:
        history.append({"role": "user", "content": u})
        history.append({"role": "assistant", "content": a})
    return history


class TestDocumentedExamples(unittest.TestCase):
    def test_transport_switch_bare_followup(self):
        history = _hist(("ขอเรททางเรือ", "19 บาท/กก."))
        r = resolve_conversation("แล้วรถล่ะ", history)
        self.assertEqual(r["resolved_question"], "ขอเรททางรถ")
        self.assertGreaterEqual(r["confidence"], 0.7)

    def test_bare_duration_attribute_followup(self):
        history = _hist(("ขอเรททางรถ", "35 บาท/กก."))
        r = resolve_conversation("กี่วัน", history)
        self.assertEqual(r["resolved_question"], "ขอทราบระยะเวลาขนส่งทางรถ")

    def test_bare_location_switch_followup(self):
        history = _hist(("ขอที่อยู่โกดังไทย", "..."))
        r = resolve_conversation("แล้วจีน", history)
        self.assertEqual(r["resolved_question"], "ขอที่อยู่โกดังจีน")

    def test_ambiguous_map_question_resolves_against_latest_topic(self):
        history = _hist(("ขอที่อยู่โกดังไทย", "..."))
        r = resolve_conversation("มีแผนที่ไหม", history)
        self.assertIn("โกดัง", r["resolved_question"])

    def test_new_unrelated_question_does_not_inherit_warehouse_entities(self):
        history = _hist(("ขอที่อยู่โกดังไทย", "..."), ("แล้วจีน", "..."))
        r = resolve_conversation("CBM คืออะไร", history)
        self.assertEqual(r["resolved_question"], "CBM คืออะไร")
        self.assertIsNone(r["followup_type"])
        self.assertEqual(r["entities_carried"], {})


class TestExplainabilityFields(unittest.TestCase):
    def test_all_required_fields_present(self):
        history = _hist(("ขอเรททางเรือ", "..."))
        r = resolve_conversation("แล้วรถล่ะ", history)
        for key in ("resolved_question", "followup_type", "entities_carried", "confidence", "prev_topic"):
            self.assertIn(key, r)

    def test_prev_topic_reported(self):
        history = _hist(("ขอเรททางเรือ", "..."))
        r = resolve_conversation("แล้วรถล่ะ", history)
        self.assertEqual(r["prev_topic"], "เรท")


class TestOnlyUserTurnsUsed(unittest.TestCase):
    def test_assistant_content_never_used_as_evidence(self):
        history = [
            {"role": "user", "content": "ขอเรททางเรือ"},
            {"role": "assistant", "content": "ทางรถราคาถูกกว่านะคะ 19 บาท/กก."},  # mentions "รถ" — must be ignored
        ]
        r = resolve_conversation("กี่วัน", history)
        # Entities must come from the USER turn ("เรือ"), never the
        # assistant's incidental mention of "รถ".
        self.assertEqual(r["resolved_question"], "ขอทราบระยะเวลาขนส่งทางเรือ")


class TestNoHistoryOrNoMarker(unittest.TestCase):
    def test_no_history_returns_unchanged(self):
        r = resolve_conversation("แล้วรถล่ะ", None)
        self.assertEqual(r["resolved_question"], "แล้วรถล่ะ")

    def test_complete_standalone_question_unchanged(self):
        history = _hist(("ขอเรททางเรือ", "..."))
        r = resolve_conversation("ขอแผนที่โกดัง", history)
        self.assertEqual(r["resolved_question"], "ขอแผนที่โกดัง")
        self.assertIsNone(r["followup_type"])


if __name__ == "__main__":
    unittest.main()
