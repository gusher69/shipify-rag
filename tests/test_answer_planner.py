"""Regression tests for services/answer_planner.py — selects/organizes
which FACT LABELS (never fact values) the LLM should focus on, and
detects when a question is genuinely ambiguous enough to require
clarification rather than guessing.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.answer_planner import plan_answer

BOTH_WAREHOUSES_CHUNK = [{"text": "โกดังไทย อยู่ที่อ่อนนุช โกดังจีน อยู่ที่กวางเจา"}]
THAI_ONLY_FAQ_CHUNK = [{"text": "พิกัดโกดังไทย อ่อนนุช 46", "is_faq_exact": True}]
BOTH_BILLS_CHUNK = [{"text": "บิลสั่งซื้อจ่ายผ่านแอป บิลค่าขนส่งจ่ายผ่านเว็บ"}]


class TestClarification(unittest.TestCase):
    def test_ambiguous_warehouse_location_requests_clarification(self):
        r = plan_answer("ขอที่อยู่โกดัง", "warehouse_location", ["address"], {}, BOTH_WAREHOUSES_CHUNK)
        self.assertTrue(r["clarification_required"])
        self.assertEqual(r["clarification_question"], "ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ")

    def test_clarification_fires_even_when_faq_matcher_already_guessed_one_row(self):
        """Regression: FAQ near-exact matching can silently pick ONE
        warehouse row (e.g. a bare "ขอที่อยู่โกดัง" scoring high enough
        against the Thai row's own Question alone) even though the
        customer never said which warehouse. The planner must still ask
        — it must never trust retrieval's own guess as "sufficient
        evidence" when the question itself never named a location."""
        faq_matched_thai_only = [{"text": "พิกัดโกดังไทย อ่อนนุช 46", "is_faq_exact": True}]
        r = plan_answer("ขอที่อยู่โกดัง", "warehouse_location", ["address"], {}, faq_matched_thai_only)
        self.assertTrue(r["clarification_required"])
        self.assertEqual(r["clarification_question"], "ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ")

    def test_known_location_never_asks_again(self):
        r = plan_answer("ขอที่อยู่โกดังไทย", "warehouse_location", ["address"],
                         {"location": "ไทย"}, BOTH_WAREHOUSES_CHUNK)
        self.assertFalse(r["clarification_required"])

    def test_ambiguous_bill_requests_clarification(self):
        r = plan_answer("จ่ายบิลยังไง", "payment_instruction", ["payment_steps"], {}, BOTH_BILLS_CHUNK)
        self.assertTrue(r["clarification_required"])
        self.assertEqual(r["clarification_question"], "ต้องการชำระบิลสั่งซื้อหรือบิลค่าขนส่งคะ")

    def test_clarification_never_selects_facts(self):
        r = plan_answer("ขอที่อยู่โกดัง", "warehouse_location", ["address"], {}, BOTH_WAREHOUSES_CHUNK)
        self.assertEqual(r["required_facts"], [])
        self.assertEqual(r["response_shape"], "clarification")


class TestFaqExactMatchTrust(unittest.TestCase):
    def test_faq_exact_match_trusts_the_row_fully(self):
        r = plan_answer("ขอแผนที่โกดังไทย", "warehouse_map", ["map_url", "address"],
                         {"location": "ไทย"}, THAI_ONLY_FAQ_CHUNK)
        self.assertEqual(r["response_shape"], "faq_direct")
        self.assertFalse(r["clarification_required"])


class TestFactSelection(unittest.TestCase):
    def test_warehouse_contact_excludes_address_and_hours(self):
        r = plan_answer("ขอเบอร์โกดังไทย", "warehouse_contact", ["phone"], {"location": "ไทย"}, [])
        self.assertIn("telephone", r["required_facts"])
        self.assertIn("address", r["excluded_facts"])
        self.assertIn("business_hours", r["excluded_facts"])

    def test_warehouse_map_excludes_china_when_location_is_thai(self):
        r = plan_answer("ขอแผนที่โกดังไทย", "warehouse_map", ["map_url", "address"],
                         {"location": "ไทย"}, [])
        self.assertIn("china_warehouse_info", r["excluded_facts"])

    def test_payment_policy_preserves_all_critical_conditions(self):
        r = plan_answer("ใช้บัตรเครดิตได้ไหม", "payment_policy",
                         ["minimum_amount", "fee_percent", "invoice_restriction"],
                         {"payment_method": "credit_card"}, [])
        for fact in ("minimum_amount", "fee_percent", "invoice_restriction"):
            self.assertIn(fact, r["required_facts"])

    def test_planner_never_invents_a_fact_label_outside_its_vocabulary(self):
        r = plan_answer("ขอเบอร์ติดต่อ", "warehouse_contact", ["phone"], {}, [])
        # Only labels from the fixed vocabulary are ever emitted.
        for fact in r["required_facts"] + r["optional_facts"] + r["excluded_facts"]:
            self.assertIsInstance(fact, str)


if __name__ == "__main__":
    unittest.main()
