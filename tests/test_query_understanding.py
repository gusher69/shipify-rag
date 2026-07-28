"""Tests for Phase 1 of the AI Playground Intelligence Pipeline
(rag/query_understanding.py): normalize -> detect intent -> rewrite ->
expand. Every step is deterministic (no LLM), so these tests assert exact
behavior, not just "returns something"."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_understanding import (
    normalize_query, detect_intent, rewrite_query, understand_query, INTENTS,
)


class TestNormalizeQuery(unittest.TestCase):
    def test_collapses_repeated_whitespace(self):
        self.assertEqual(normalize_query("Plan   4    ค่าห้อง"), "Plan 4 ค่าห้อง")

    def test_strips_stray_trailing_punctuation(self):
        self.assertEqual(normalize_query("ICU Plan 2 ได้เท่าไหร่?"), "ICU Plan 2 ได้เท่าไหร่")

    def test_preserves_important_entities_verbatim(self):
        self.assertIn("Plan 4", normalize_query("Plan 4 ค่าห้องเท่าไหร่?"))
        self.assertIn("ICU", normalize_query("ICU Plan 2 ได้เท่าไหร่"))

    def test_known_abbreviation_survives_normalization(self):
        result = normalize_query("ICU. ได้เท่าไหร่?")
        self.assertIn("ICU", result)

    def test_empty_string_returns_empty(self):
        self.assertEqual(normalize_query(""), "")

    def test_none_like_falsy_input_returns_empty(self):
        self.assertEqual(normalize_query(None), "")


class TestDetectIntent(unittest.TestCase):
    def test_coverage_intent(self):
        self.assertEqual(detect_intent("Plan 4 ค่าห้องเท่าไหร่"), "coverage")
        self.assertEqual(detect_intent("ICU Plan 2 ได้เท่าไหร่"), "coverage")

    def test_premium_intent(self):
        self.assertEqual(detect_intent("Plan 4 เบี้ยรายเดือนเท่าไหร่"), "premium")
        self.assertEqual(detect_intent("What is the monthly premium?"), "premium")

    def test_policy_intent(self):
        self.assertEqual(detect_intent("เงื่อนไขกรมธรรม์เป็นอย่างไร"), "policy")

    def test_faq_intent(self):
        self.assertEqual(detect_intent("มี FAQ เกี่ยวกับเรื่องนี้ไหม"), "faq")

    def test_company_intent(self):
        self.assertEqual(detect_intent("บริษัทเรามีพันธกิจอะไร"), "company")
        self.assertEqual(detect_intent("What is our company mission?"), "company")

    def test_contact_intent(self):
        self.assertEqual(detect_intent("ติดต่อบริษัทได้อย่างไร"), "contact")
        self.assertEqual(detect_intent("How can I contact support?"), "contact")

    def test_excel_calculation_intent(self):
        self.assertEqual(detect_intent("ยอดขายรวมเดือนนี้เท่าไหร่"), "excel_calculation")
        self.assertEqual(detect_intent("What is the total sum?"), "excel_calculation")

    def test_location_intent(self):
        self.assertEqual(detect_intent("สาขาที่ใกล้ที่สุดอยู่ที่ไหน"), "location")
        self.assertEqual(detect_intent("What is your office address?"), "location")

    def test_unknown_intent_for_unrelated_text(self):
        self.assertEqual(detect_intent("สวัสดีตอนเช้า"), "unknown")

    def test_empty_text_is_unknown(self):
        self.assertEqual(detect_intent(""), "unknown")

    def test_every_intent_is_a_declared_intent(self):
        for text in ["ค่าห้อง", "เบี้ย", "กรมธรรม์", "FAQ", "บริษัท", "ติดต่อ", "ยอดรวม", "สาขา", "xyz"]:
            self.assertIn(detect_intent(text), INTENTS)


class TestRewriteQuery(unittest.TestCase):
    def test_icu_expands_with_full_form_and_thai_gloss(self):
        result = rewrite_query("ICU Plan 2 ได้เท่าไหร่")
        self.assertIn("Intensive Care Unit", result)
        self.assertIn("ห้อง ICU", result)

    def test_kidney_failure_expands_with_ckd(self):
        result = rewrite_query("ถ้าไตวาย ได้เงินเท่าไหร่")
        self.assertIn("CKD", result)
        self.assertIn("โรคไตวายเรื้อรัง", result)

    def test_mission_term_expands(self):
        self.assertIn("Mission", rewrite_query("มิชชั่นของบริษัทคืออะไร"))

    def test_vision_term_expands(self):
        self.assertIn("Vision", rewrite_query("วิสัยทัศน์ของบริษัทคืออะไร"))

    def test_original_query_is_always_a_prefix_of_the_result(self):
        original = "ICU Plan 2 ได้เท่าไหร่"
        result = rewrite_query(original)
        self.assertTrue(result.startswith(original))

    def test_no_matching_term_returns_original_unchanged(self):
        original = "สวัสดีตอนเช้า"
        self.assertEqual(rewrite_query(original), original)

    def test_empty_string_returns_empty(self):
        self.assertEqual(rewrite_query(""), "")


class TestUnderstandQueryPipeline(unittest.TestCase):
    def test_returns_all_required_explainability_fields(self):
        result = understand_query("ICU Plan 2 ได้เท่าไหร่")
        for key in ("original_query", "normalized_query", "detected_intent",
                    "rewritten_query", "expanded_queries", "detected_language"):
            self.assertIn(key, result)

    def test_original_query_field_is_exact_literal_input(self):
        q = "  ICU Plan 2   ได้เท่าไหร่?  "
        result = understand_query(q)
        self.assertEqual(result["original_query"], q)

    def test_expanded_queries_first_element_is_exact_original_question(self):
        """Several downstream callers (rag/hybrid_scoring.py's heading-
        match is_original check) depend on variants[0] being the exact
        literal question — Phase 1 must never break this contract."""
        q = "ICU Plan 2 ได้เท่าไหร่"
        result = understand_query(q)
        self.assertEqual(result["expanded_queries"][0], q)

    def test_rewritten_query_included_in_expanded_queries(self):
        result = understand_query("ICU Plan 2 ได้เท่าไหร่")
        self.assertIn(result["rewritten_query"], result["expanded_queries"])

    def test_detected_intent_matches_direct_call(self):
        q = "Plan 4 เบี้ยรายเดือนเท่าไหร่"
        result = understand_query(q)
        self.assertEqual(result["detected_intent"], detect_intent(normalize_query(q)))

    def test_backward_compatible_when_no_synonyms_match(self):
        """A question with no recognized abbreviation/synonym must behave
        identically to the pre-Phase-1 expand_query() output plus the
        new explainability fields — no regression for the common case."""
        from rag.query_expansion import expand_query
        q = "แล้วมิชชั่น คือ อะไร"
        result = understand_query(q)
        old_variants = expand_query(q)
        for v in old_variants:
            self.assertIn(v, result["expanded_queries"])


if __name__ == "__main__":
    unittest.main()
