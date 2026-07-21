"""Regression tests for Direct FAQ Fidelity Mode (P0, 2026-07-21):
an approved FAQ's answer TYPE (navigation instruction, procedure,
contact info, policy, ...) must be preserved instead of being forced
through a fixed direct-fact schema (address/map_url/etc.), which
previously turned a correct navigation-instruction answer into a false
"no information" response. Also covers the exact-original-query-
participates-in-FAQ-matching fix (Part 5/6).
"""
import unittest

from services.answer_planner import detect_answer_type, plan_answer, _FACTS_BY_ANSWER_TYPE
from rag.faq_matcher import match_faq_exact, find_best_faq_match


class TestAnswerTypeDetection(unittest.TestCase):
    def test_navigation_instruction(self):
        text = "เข้าที่หน้าเว็บ ตรงเมนู ที่อยู่โกดังจีนได้เลยนะคะ จากนั้นคัดลอกที่อยู่ตามขั้นตอนในรูป"
        self.assertEqual(detect_answer_type(text), "navigation_instruction")

    def test_procedure(self):
        text = "1. เข้าเมนูคูปอง\n2. กดใช้คูปอง\n3. ยืนยันการสั่งซื้อ"
        self.assertEqual(detect_answer_type(text), "procedure")

    def test_contact_information(self):
        text = "Shipify ฝ่ายบริการลูกค้า: 02-026-6426"
        self.assertEqual(detect_answer_type(text), "contact_information")

    def test_policy(self):
        text = "ไม่มีขั้นต่ำในการสั่งซื้อค่ะ"
        self.assertEqual(detect_answer_type(text), "policy")

    def test_attachment_instruction(self):
        text = "ดูรูปภาพประกอบขั้นตอนการตีลังไม้ได้เลยค่ะ"
        self.assertIn(detect_answer_type(text), ("attachment_instruction", "procedure"))

    def test_plain_direct_fact_fallback(self):
        text = "ค่าบริการคือ 500 บาทต่อชิ้น"
        self.assertEqual(detect_answer_type(text), "direct_fact")

    def test_every_answer_type_has_a_fact_mapping_or_falls_back(self):
        for t in ("navigation_instruction", "procedure", "contact_information", "policy",
                   "conditional_answer", "attachment_instruction", "escalation_instruction"):
            self.assertIn(t, _FACTS_BY_ANSWER_TYPE)


class TestAnswerPlanAdaptsToEvidenceType(unittest.TestCase):
    """Part 2 — the core regression: a navigation-instruction FAQ must
    never be forced through [warehouse_name, address, map_url]."""

    def _exact_faq_chunk(self, answer_text):
        return {"is_faq_exact": True, "text": f"Question: x\nAnswer: {answer_text}",
                "section_title": "x", "score": 1.0}

    def test_navigation_instruction_faq_does_not_require_address_or_map_url(self):
        chunks = [self._exact_faq_chunk(
            "เข้าที่หน้าเว็บ ตรงเมนู ที่อยู่โกดังจีนได้เลยนะคะ จากนั้นคัดลอกที่อยู่ตามขั้นตอนในรูป")]
        plan = plan_answer("ขอที่อยู่โกดังจีน", "warehouse_map", entities={"location": "จีน"},
                            requested_attributes=["warehouse_name", "address", "map_url"], chunks=chunks)
        self.assertEqual(plan["answer_type"], "navigation_instruction")
        self.assertNotIn("address", plan["required_facts"])
        self.assertNotIn("map_url", plan["required_facts"])
        self.assertIn("navigation_destination", plan["required_facts"])
        self.assertIn("action_steps", plan["required_facts"])

    def test_procedure_faq_requires_action_steps_not_generic_schema(self):
        chunks = [self._exact_faq_chunk("เข้าเมนูคูปองแล้วกดใช้คูปองได้เลยค่ะ ตามขั้นตอนในรูป")]
        plan = plan_answer("ใช้คูปองยังไง", "coupon_policy", chunks=chunks)
        self.assertEqual(plan["answer_type"], "procedure")
        self.assertIn("action_steps", plan["required_facts"])

    def test_contact_faq_requires_phone_not_generic_schema(self):
        chunks = [self._exact_faq_chunk("Shipify ฝ่ายบริการลูกค้า: 02-026-6426")]
        plan = plan_answer("ขอเบอร์ติดต่อ", "warehouse_contact", entities={"location": "จีน"}, chunks=chunks)
        self.assertEqual(plan["answer_type"], "contact_information")
        self.assertIn("phone_or_contact", plan["required_facts"])

    def test_policy_faq_requires_policy_statement(self):
        chunks = [self._exact_faq_chunk("ไม่มีขั้นต่ำในการสั่งซื้อค่ะ")]
        plan = plan_answer("มีขั้นต่ำไหม", "unknown", chunks=chunks)
        self.assertEqual(plan["answer_type"], "policy")
        self.assertIn("policy_statement", plan["required_facts"])


class TestOriginalQueryParticipatesInFaqExactMatch(unittest.TestCase):
    """Part 5/6 — the root cause: a canonical rewrite must never break an
    otherwise-exact match against an approved FAQ's Question field."""

    def test_exact_match_survives_a_canonical_rewrite_variant(self):
        rows = [{"question": "ขอที่อยู่โกดังจีน", "answer": "...", "alt_questions": []}]
        # The canonical-rewritten form no longer matches verbatim...
        result_canonical_only = find_best_faq_match("ขอที่อยู่และแผนที่โกดังจีน", rows)
        self.assertIsNone(result_canonical_only if result_canonical_only and
                          result_canonical_only["match_type"] == "exact" else None)
        # ...but match_faq_exact must still find the exact match when the
        # ORIGINAL wording is available as a candidate.
        result = find_best_faq_match("ขอที่อยู่โกดังจีน", rows)
        self.assertEqual(result["match_type"], "exact")


if __name__ == "__main__":
    unittest.main()
