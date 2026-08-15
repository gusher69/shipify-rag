"""Tests for services/hybrid_question_classifier.py (Hybrid Question
Segmentation sprint, 2026-08-02) — the deterministic, Registry-driven
classifier that lets AI Playground's Auto mode select RAG_ONLY / ERP_ONLY
/ HYBRID / CLARIFICATION_REQUIRED / UNKNOWN, and the question segmenter
used for Hybrid execution. Uses the same _FakeSupabase/BusinessActionRegistry
convention as tests/test_decision_engine.py — never a real DB/network call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.hybrid_question_classifier import classify_question, _split_clauses, _segment_by_value


def _seed_action(reg, *, key, action_type="API", category=None, ai_description="", keywords=None,
                  enabled=True, priority=0, params=None):
    action = reg.create({
        "action_key": key, "name": key, "display_name": key, "action_type": action_type,
        "category": category, "ai_description": ai_description, "search_keywords": keywords or [],
        "enabled": enabled, "priority": priority,
    })
    if params:
        reg.replace_parameters(action["id"], params)
    return action["id"]


class TestClauseSegmentation(unittest.TestCase):
    def test_splits_on_thai_conjunction(self):
        clauses = _split_clauses("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร")
        self.assertEqual(clauses, ["ลูกค้า C00001 มีคูปองอะไร", "คูปองใช้งานอย่างไร"])

    def test_no_conjunction_returns_single_clause(self):
        clauses = _split_clauses("ขอดูข้อมูลลูกค้ารหัส C00001")
        self.assertEqual(clauses, ["ขอดูข้อมูลลูกค้ารหัส C00001"])

    def test_segment_by_value_matches_the_spec_example(self):
        result = _segment_by_value("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "C00001")
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")

    def test_segment_returns_none_when_not_separable(self):
        self.assertIsNone(_segment_by_value("ขอดูข้อมูลลูกค้ารหัส C00001", "C00001"))

    def test_segment_returns_none_when_value_not_in_any_clause(self):
        # Conjunction present but the matched value isn't literally in
        # either clause -> cannot safely attribute a clause to ERP.
        self.assertIsNone(_segment_by_value("ขอทราบนโยบาย และเวลาทำการ", "C00001"))


class TestClassifyQuestion(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def _seed_customer_lookup(self):
        action_id = _seed_action(self.reg, key="get_customer_coupons", category="customer",
                                  keywords=["คูปอง", "ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        return action_id

    def test_empty_message_is_unknown(self):
        result = classify_question("", self.reg)
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_greeting_only_is_unknown(self):
        result = classify_question("สวัสดีค่ะ", self.reg)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertIsNone(result["selected_action_id"])

    def test_no_business_action_match_is_rag_only(self):
        self._seed_customer_lookup()
        result = classify_question("ขอทราบนโยบายการคืนสินค้า", self.reg)
        self.assertEqual(result["classification"], "RAG_ONLY")
        self.assertIsNone(result["selected_action_id"])
        self.assertEqual(result["rag_sub_question"], "ขอทราบนโยบายการคืนสินค้า")

    def test_action_matched_no_parameter_value_is_erp_only(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("สวัสดีค่ะ ช่วยดูข้อมูลลูกค้าให้หน่อย", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertIsNone(result["rag_sub_question"])

    def test_single_clause_with_value_is_erp_only_not_hybrid(self):
        self._seed_customer_lookup()
        result = classify_question("ขอดูข้อมูลลูกค้ารหัส C00001", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["erp_sub_question"], "ขอดูข้อมูลลูกค้ารหัส C00001")

    def test_compound_question_is_hybrid_and_segments(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", self.reg)
        self.assertEqual(result["classification"], "HYBRID")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")
        self.assertGreater(result["confidence"], 0.0)

    def test_ambiguous_actions_require_clarification(self):
        _seed_action(self.reg, key="action_one", category="customer", keywords=["ข้อมูลลูกค้า"])
        _seed_action(self.reg, key="action_two", category="customer", keywords=["ข้อมูลลูกค้า"])
        result = classify_question("ข้อมูลลูกค้า", self.reg)
        self.assertEqual(result["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(result["candidate_action_ids"]), 2)
        self.assertIsNone(result["selected_action_id"])

    def test_forced_action_id_skips_detection_but_still_segments(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", self.reg,
                                     forced_action_id=action_id)
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")

    def _seed_tracking_vs_shipment_list(self):
        tracking_id = _seed_action(self.reg, key="search_tracking", category="shipment", keywords=["tracking"])
        self.reg.replace_parameters(tracking_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True,
             "input_source": "customer_message"},
        ])
        list_id = _seed_action(self.reg, key="search_shipment_list", category="shipment", keywords=["tracking"])
        self.reg.replace_parameters(list_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        return tracking_id, list_id

    def test_decisive_parameter_evidence_breaks_a_keyword_tie(self):
        """Final Conversational Correctness (2026-08-15) — a specific
        Tracking-shaped value decisively selects the tracking action even
        though both candidates tie on the bare "tracking" keyword."""
        tracking_id, list_id = self._seed_tracking_vs_shipment_list()
        result = classify_question("เช็ก tracking TRACK123456 ของลูกค้า C00001", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], tracking_id)

    def test_shared_identifier_alone_still_requires_clarification(self):
        """A bare CustCode both tied actions require identically is never
        discriminating on its own — must still ask for clarification, not
        guess based on which action happens to have a keyword edge."""
        self._seed_tracking_vs_shipment_list()
        result = classify_question("ขอดู tracking ของผม C00001", self.reg)
        self.assertEqual(result["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(result["candidate_action_ids"]), 2)

    def test_registry_failure_falls_back_to_rag_only(self):
        class _BrokenRegistry:
            def enabled_actions(self):
                raise RuntimeError("db down")

            def get_full(self, *a, **k):
                return None

        result = classify_question("ขอทราบนโยบาย", _BrokenRegistry())
        self.assertEqual(result["classification"], "RAG_ONLY")


if __name__ == "__main__":
    unittest.main()
