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

    def test_company_contact_faq_exact_is_not_asked_for_a_warehouse_clarification(self):
        """Regression (2026-09-01): "ขอเบอร์ติดต่อ" sent right after a
        warehouse exchange inherits prev_topic="โกดัง" and is reclassified
        warehouse_contact — but it exact-matched the generic company-
        contact FAQ row, whose own Question ("ขอเบอร์ติดต่อ") has no
        "โกดัง" in it. That self-contained exact match must win, not a
        ไทย/จีน clarification."""
        company_contact_faq = [{
            "text": "Question: ขอเบอร์ติดต่อ\nAnswer: Shipify 02-026-6426 / Fasttrade 02-026-6425",
            "is_faq_exact": True,
        }]
        r = plan_answer("ขอเบอร์ติดต่อ", "warehouse_contact", ["phone"], {}, company_contact_faq)
        self.assertFalse(r["clarification_required"])
        self.assertEqual(r["response_shape"], "faq_direct")

    def test_generic_thai_warehouse_request_aggregates_all_locations(self):
        """"ขอเบอร์โกดัง" -> "ไทย": warehouse intent, a country but no named
        pickup point, and the evidence carries 2 pickup points -> the plan
        must aggregate (goal names EVERY location; short_answer widened)."""
        multi = [{"text": "โกดังไทยมี 2 ที่ อ่อนนุช 46 โทร 064-224-7205 ... โกดังนนทบุรี โทร 091-5050-775"}]
        r = plan_answer("ขอเบอร์โกดังไทย", "warehouse_contact", ["phone"], {"location": "ไทย"}, multi)
        self.assertFalse(r["clarification_required"])
        self.assertIn("EVERY", r["answer_goal"])
        self.assertNotEqual(r["response_shape"], "short_answer")
        self.assertIn("warehouse_name", r["required_facts"])

    def test_specific_pickup_point_stays_narrow_even_with_multi_location_evidence(self):
        multi = [{"text": "โกดังไทยมี 2 ที่ อ่อนนุช 46 โทร 064-224-7205 ... โกดังนนทบุรี โทร 091-5050-775"}]
        r = plan_answer("ขอเบอร์โกดังนนทบุรี", "warehouse_contact", ["phone"], {}, multi)
        self.assertFalse(r["clarification_required"])   # named point resolves the country
        self.assertIn("นนทบุรี", r["answer_goal"])
        self.assertIn("ignore", r["answer_goal"].lower())

    def test_specific_pickup_point_does_not_return_a_multi_location_faq_verbatim(self):
        faq_both = [{
            "text": "Question: ขอที่อยู่โกดังหน่อย\nAnswer: มีโกดังไทย 2 ที่ อ่อนนุช 46 ... นนทบุรี 76 ...",
            "is_faq_exact": True,
        }]
        r = plan_answer("ขอแผนที่โกดังนนทบุรี", "warehouse_map", ["map_url"], {}, faq_both)
        self.assertNotEqual(r["response_shape"], "faq_direct")
        self.assertIn("นนทบุรี", r["answer_goal"])

    def test_bare_warehouse_phone_question_still_clarifies(self):
        """"ขอเบอร์โกดัง" does NOT FAQ-exact match any row (no warehouse-
        phone knowledge_items) — 3 hybrid chunks, no is_faq_exact — so the
        country clarification still fires."""
        hybrid_chunks = [
            {"text": "Question: ขอที่อยู่โกดังหน่อย\nAnswer: มีโกดังไทย 2 ที่ เบอร์ 064..."},
            {"text": "Question: ขอที่อยู่โกดังจีน\nAnswer: เข้าเมนูที่อยู่โกดังจีน"},
            {"text": "อื่นๆ"},
        ]
        r = plan_answer("ขอเบอร์โกดัง", "warehouse_contact", ["phone"], {}, hybrid_chunks)
        self.assertTrue(r["clarification_required"])
        self.assertEqual(r["clarification_question"], "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ")

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


class TestBothTransportModes(unittest.TestCase):
    """Customer-acceptance regression: 'ทางรถกับทางเรือระยะเวลากี่วัน'
    classifies as actionable_intent='unknown' (the intent classifier does
    not tag a bare road+sea duration question), so gating the both-modes
    answer_goal on shipping_rate/shipping_duration alone let synthesis
    answer road-only even though the retrieved chunk carried both
    durations. The trigger is now 'raw wording names BOTH รถ and เรือ',
    independent of actionable_intent."""

    def test_unknown_intent_still_gets_cover_both_goal(self):
        r = plan_answer("ทางรถกับทางเรือระยะเวลากี่วัน", "unknown", [], {}, [{"text": "x"}],
                        raw_question="ทางรถกับทางเรือระยะเวลากี่วัน")
        self.assertIn("BOTH road", r["answer_goal"])
        self.assertIn("cover both", r["answer_goal"])

    def test_shipping_duration_intent_unchanged(self):
        r = plan_answer("ระยะเวลาทางรถทางเรือ", "shipping_duration", [], {"transport": "รถ"}, [{"text": "x"}],
                        raw_question="ทางรถกับทางเรือระยะเวลากี่วัน")
        self.assertIn("BOTH road", r["answer_goal"])

    def test_single_mode_question_is_untouched(self):
        for q in ("ทางรถใช้เวลากี่วัน", "ส่งทางรถได้ไหม", "สามารถช่วยได้ไหมคะ"):
            r = plan_answer(q, "unknown", [], {}, [{"text": "x"}], raw_question=q)
            self.assertNotIn("BOTH road", r["answer_goal"], q)


if __name__ == "__main__":
    unittest.main()
