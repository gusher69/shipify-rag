import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.intent_classifier import classify_query_intent


class TestClassifyQueryIntent(unittest.TestCase):
    def test_room_rate_question_is_coverage_benefit(self):
        self.assertEqual(classify_query_intent("Plan 4 ค่าห้องเท่าไหร่"), "coverage_benefit")

    def test_icu_question_is_coverage_benefit(self):
        self.assertEqual(classify_query_intent("ICU Plan 2 ได้เท่าไหร่"), "coverage_benefit")

    def test_kidney_failure_question_is_coverage_benefit(self):
        self.assertEqual(classify_query_intent("ถ้าไตวายเรื้อรัง ได้เงินเท่าไหร่"), "coverage_benefit")

    def test_monthly_premium_question_is_premium_price(self):
        self.assertEqual(classify_query_intent("Plan 4 เบี้ยรายเดือนเท่าไหร่"), "premium_price")

    def test_annual_premium_question_is_premium_price(self):
        self.assertEqual(classify_query_intent("Plan 4 เบี้ยรายปีเท่าไหร่"), "premium_price")

    def test_age_plus_premium_resolves_to_premium_not_eligibility(self):
        # Premium vocabulary must dominate over the incidental age mention.
        self.assertEqual(classify_query_intent("อายุ 35 ปี Plan 3 เบี้ยเท่าไหร่"), "premium_price")

    def test_exclusion_question(self):
        self.assertEqual(classify_query_intent("มีข้อยกเว้นอะไรบ้าง"), "exclusion")

    def test_eligibility_question(self):
        self.assertEqual(classify_query_intent("อายุเท่าไหร่ถึงสมัครได้"), "eligibility")

    def test_empty_question_is_unknown(self):
        self.assertEqual(classify_query_intent(""), "unknown")

    def test_unrelated_question_is_unknown(self):
        self.assertEqual(classify_query_intent("วันนี้อากาศเป็นอย่างไร"), "unknown")


if __name__ == "__main__":
    unittest.main()
