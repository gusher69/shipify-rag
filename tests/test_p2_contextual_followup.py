"""P2A — contextual follow-up ("correct answer + ONE useful next question").

Deterministic. No LLM / no network. Covers the follow-up POLICY
(decide_followup), the FAQ-direct purpose renderer, the prompt render,
and the plan wiring. The 3 real synthesis phrasings are checked
separately/live, not here.
"""
import unittest

from rag.query_resolution import decompose_request
from services.answer_planner import (
    decide_followup, render_followup_question, plan_answer,
)
from services.prompt_builder import _build_answer_plan_block


def _fu(raw, intent="service_information", history=None, answerability="direct_answer",
        conflicting=None):
    spec = decompose_request(raw, raw_question=raw)
    return decide_followup(intent, spec, raw, history, answerability, conflicting)


class Triggers(unittest.TestCase):
    def test_A_import_interest_no_product(self):
        r = _fu("อยากนำเข้าสินค้าจากจีน")
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "elicit_product_type")

    def test_A2_import_interest_bare(self):
        self.assertTrue(_fu("สนใจใช้บริการนำเข้าครับ")["needed"])

    def test_B_import_interest_with_known_product_no_reask(self):
        r = _fu("อยากนำเข้าน้ำหอมจากจีน")
        self.assertFalse(r["needed"])

    def test_C_air_unavailable_product_unknown(self):
        r = _fu("มีขนส่งทางเครื่องบินไหม", intent="unknown")
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "elicit_product_type")

    def test_C2_air_with_named_product_no_followup(self):
        self.assertFalse(_fu("ส่งเครื่องสำอางทางเครื่องบินได้ไหม", intent="unknown")["needed"])

    def test_D_prohibited_goods_offers_alternative(self):
        r = _fu("น้ำหอมนำเข้าได้ไหม", intent="prohibited_goods")
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "offer_alternative_product")


class NoFollowupClasses(unittest.TestCase):
    def test_E_closed_factual_hours(self):
        self.assertFalse(_fu("โกดังอ่อนนุชเปิดกี่โมง", intent="warehouse_location")["needed"])

    def test_F_company_contact(self):
        self.assertFalse(_fu("ขอเบอร์ติดต่อ", intent="warehouse_contact")["needed"])

    def test_G_invoice_policy(self):
        self.assertFalse(_fu("ใบกำกับค่าสินค้าออกได้ไหม", intent="invoice_policy")["needed"])

    def test_H_erp_private_coupon(self):
        self.assertFalse(_fu("ผมมีคูปองอะไรบ้าง", intent="coupon_policy")["needed"])

    def test_I_no_information_gated(self):
        self.assertFalse(_fu("อยากนำเข้าสินค้าจากจีน", answerability="no_information")["needed"])

    def test_J_conflict_gated(self):
        self.assertFalse(_fu("น้ำหอมนำเข้าได้ไหม", intent="prohibited_goods",
                              conflicting=["ค่าขนส่งทางรถ (บาท/กก.)"])["needed"])

    def test_plain_rate_question_no_followup(self):
        self.assertFalse(_fu("ทางรถเรทเท่าไหร่", intent="shipping_rate")["needed"])


class RepetitionGuard(unittest.TestCase):
    def test_K_purpose_recently_served(self):
        hist = [
            {"role": "user", "content": "อยากนำเข้าสินค้าจากจีน"},
            {"role": "assistant", "content": "ยินดีให้บริการค่ะ คุณลูกค้าต้องการนำเข้าสินค้าประเภทไหนคะ"},
        ]
        self.assertFalse(_fu("อยากนำเข้าของจากจีน", history=hist)["needed"])

    def test_K2_different_purpose_not_suppressed(self):
        hist = [{"role": "assistant", "content": "มีสินค้าอย่างอื่นที่ต้องการให้ช่วยเช็กไหมคะ"}]
        # elicit_product_type is a different purpose -> still allowed
        self.assertTrue(_fu("อยากนำเข้าสินค้าจากจีน", history=hist)["needed"])

    def test_L_user_supplied_answer_no_trigger(self):
        # "น้ำหอมครับ" after the assistant asked product type — it is not an
        # import-interest / air / prohibited trigger at all, so P2 never
        # re-asks.
        hist = [
            {"role": "user", "content": "อยากนำเข้าสินค้าจากจีน"},
            {"role": "assistant", "content": "คุณลูกค้าต้องการนำเข้าสินค้าประเภทไหนคะ"},
        ]
        self.assertFalse(_fu("น้ำหอมครับ", intent="unknown", history=hist)["needed"])


class Renderer(unittest.TestCase):
    def test_faq_direct_purpose_questions(self):
        self.assertIn("ประเภทไหน", render_followup_question("elicit_product_type"))
        self.assertIn("อย่างอื่น", render_followup_question("offer_alternative_product"))

    def test_unknown_purpose_empty(self):
        self.assertEqual(render_followup_question(None), "")
        self.assertEqual(render_followup_question("nope"), "")


class PromptRender(unittest.TestCase):
    def _block(self, followup):
        return _build_answer_plan_block({
            "answer_goal": "g", "response_shape": "answer_then_details",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "followup": followup,
        })

    def test_instruction_present_when_needed(self):
        b = self._block({"needed": True, "purpose": "elicit_product_type",
                          "question_goal": "ask what type of product the customer wants to import"})
        self.assertIn("CONTEXTUAL FOLLOW-UP", b)
        self.assertIn("EXACTLY ONE", b)
        self.assertIn("มีอะไรให้ช่วยอีกไหม", b)  # named only to forbid it

    def test_absent_when_not_needed(self):
        b = self._block({"needed": False, "purpose": None, "question_goal": None})
        self.assertNotIn("CONTEXTUAL FOLLOW-UP", b)


class PlanWiring(unittest.TestCase):
    def test_plan_attaches_followup_import_interest(self):
        spec = decompose_request("อยากนำเข้าสินค้าจากจีน", raw_question="อยากนำเข้าสินค้าจากจีน")
        plan = plan_answer("อยากนำเข้าสินค้าจากจีน", "service_information", ["general_info"], {},
                            chunks=[], raw_question="อยากนำเข้าสินค้าจากจีน",
                            request_spec=spec, answerability="direct_answer")
        self.assertTrue(plan["followup"]["needed"])
        self.assertEqual(plan["followup"]["purpose"], "elicit_product_type")

    def test_plan_followup_off_for_contact(self):
        spec = decompose_request("ขอเบอร์ติดต่อ", raw_question="ขอเบอร์ติดต่อ")
        plan = plan_answer("ขอเบอร์ติดต่อ", "warehouse_contact", ["phone"], {},
                            chunks=[], raw_question="ขอเบอร์ติดต่อ",
                            request_spec=spec, answerability="direct_answer")
        self.assertFalse(plan["followup"]["needed"])

    def test_plan_followup_off_on_conflict(self):
        spec = decompose_request("น้ำหอมนำเข้าได้ไหม", raw_question="น้ำหอมนำเข้าได้ไหม")
        plan = plan_answer("น้ำหอมนำเข้าได้ไหม", "prohibited_goods", ["prohibited_list"], {},
                            chunks=[], raw_question="น้ำหอมนำเข้าได้ไหม",
                            request_spec=spec, answerability="direct_answer",
                            conflicting_components=["ค่าขนส่งทางรถ (บาท/กก.)"])
        self.assertFalse(plan["followup"]["needed"])


if __name__ == "__main__":
    unittest.main()
