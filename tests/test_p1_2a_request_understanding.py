"""P1.2A — real-user RAG understanding: one message can carry several
components (products / facets / sub-questions / a comparison / an
in-message correction) that must all survive query resolution ->
decomposition -> answer planning.

Deterministic, no LLM / no network. Covers acceptance classes A-M plus
the P1-closed regressions most at risk from this change.
"""
import unittest

from rag.query_resolution import (
    decompose_request, build_request_components, resolve_conversation,
)
from rag.query_understanding import classify_actionable_intent
from services.answer_planner import plan_answer
from services.prompt_builder import _build_answer_plan_block


def _labels(q, h=None):
    return [l for l, _ in build_request_components(decompose_request(q, h))]


class MultiEntity(unittest.TestCase):
    def test_A_three_products_all_retained(self):
        s = decompose_request("แบตเตอรี่ น้ำหอม น้ำยาซักผ้า นำเข้าได้ไหม")
        self.assertEqual(s.entities, ["แบตเตอรี่", "น้ำหอม", "น้ำยาซักผ้า"])
        self.assertEqual(len(build_request_components(s)), 3)

    def test_A_comma_and_conjunction_forms(self):
        s = decompose_request("แบตเตอรี่, น้ำหอม แล้วก็แก้วน้ำ นำเข้าได้ไหม")
        self.assertEqual(s.entities, ["แบตเตอรี่", "น้ำหอม", "แก้วน้ำ"])

    def test_B_partial_entities_still_all_components(self):
        # Whether glass has evidence is decided at synthesis; decomposition
        # must still expose all three as requested components.
        self.assertEqual(_labels("แบตเตอรี่ น้ำหอม แล้วก็แก้วน้ำ นำเข้าได้ไหม"),
                         ["แบตเตอรี่ / eligibility", "น้ำหอม / eligibility", "แก้วน้ำ / eligibility"])

    def test_not_an_eligibility_question_has_no_entities(self):
        self.assertEqual(decompose_request("ค่าส่งไปจีนเท่าไหร่").entities, [])

    def test_import_agent_service_question_is_not_prohibited_goods(self):
        self.assertNotEqual(
            classify_actionable_intent("ฝากนำเข้าได้ไหมคะ")["actionable_intent"], "prohibited_goods")

    def test_goods_eligibility_routes_to_prohibited_goods(self):
        self.assertEqual(
            classify_actionable_intent("แบตเตอรี่นำเข้าได้ไหม")["actionable_intent"], "prohibited_goods")


class MultiFacet(unittest.TestCase):
    def test_C_road_sea_rate_duration_four_components(self):
        self.assertEqual(_labels("ทางรถกับทางเรือราคาเท่าไหร่ ใช้กี่วัน"),
                         ["รถ / rate", "รถ / duration", "เรือ / rate", "เรือ / duration"])

    def test_D_three_facets_preserved(self):
        self.assertEqual(_labels("ราคาเท่าไหร่ ใช้กี่วัน มีขั้นต่ำไหม"),
                         ["rate", "duration", "minimum"])

    def test_single_facet_is_not_multi_component(self):
        self.assertEqual(_labels("ค่าส่งทางรถเท่าไหร่"), [])


class MultiTopic(unittest.TestCase):
    def test_E_invoice_two_subquestions(self):
        s = decompose_request("ใบกำกับออกได้ไหม แล้วโหลดจากไหน")
        self.assertEqual(s.sub_questions, ["ใบกำกับออกได้ไหม", "โหลดจากไหน"])

    def test_F_coupon_two_subquestions(self):
        s = decompose_request("คูปองใช้ยังไง แล้วใช้ไม่หมดคืนได้ไหม")
        self.assertEqual(len(s.sub_questions), 2)

    def test_bare_followup_is_not_split_into_subquestions(self):
        self.assertEqual(decompose_request("แล้วเรือล่ะ").sub_questions, [])

    def test_warehouse_contact_and_address_not_forced_multi_topic(self):
        # "ขอเบอร์และที่อยู่โกดังไทย" — existing warehouse aggregation owns
        # this; it must not be split into multi-target sub-questions.
        self.assertEqual(_labels("ขอเบอร์และที่อยู่โกดังไทย"), [])


class Comparison(unittest.TestCase):
    def test_H_cheaper_uses_rate_components(self):
        s = decompose_request("รถกับเรืออันไหนถูกกว่า")
        self.assertEqual(s.comparison, "cheaper")
        self.assertEqual(_labels("รถกับเรืออันไหนถูกกว่า"), ["รถ / rate", "เรือ / rate"])

    def test_I_context_comparison_pulls_modes_from_history(self):
        h = [{"role": "user", "content": "ทางรถเรทเท่าไหร่"},
             {"role": "assistant", "content": "35"},
             {"role": "user", "content": "ทางเรือเรทเท่าไหร่"},
             {"role": "assistant", "content": "19"}]
        s = decompose_request("อันไหนเร็วกว่า", h)
        self.assertEqual(s.comparison, "faster")
        self.assertEqual(_labels("อันไหนเร็วกว่า", h), ["รถ / duration", "เรือ / duration"])


class NegationCorrection(unittest.TestCase):
    def test_J_attribute_correction(self):
        c = decompose_request("ไม่ได้ถามราคา ถามระยะเวลา").corrections
        self.assertIn("rate", c["removed_facets"])
        self.assertIn("duration", c["added_facets"])

    def test_K_transport_correction_road_only(self):
        self.assertEqual(decompose_request("ไม่เอาทางเรือ เอาทางรถ").transport_modes, ["รถ"])

    def test_location_correction(self):
        c = decompose_request("ไม่ใช่โกดังจีน หมายถึงไทย").corrections
        self.assertEqual(c["removed_location"], ["จีน"])
        self.assertEqual(c["added_location"], ["ไทย"])

    def test_L_erp_to_rag_coupon_correction(self):
        self.assertEqual(
            decompose_request("ไม่ได้ถามคูปองของผม ถามวิธีใช้").corrections["interpretation"], "rag")
        self.assertEqual(
            classify_actionable_intent("ไม่ได้ถามคูปองของผม ถามวิธีใช้")["actionable_intent"],
            "coupon_policy")


class ProductFollowUp(unittest.TestCase):
    H = [{"role": "user", "content": "แบตเตอรี่นำเข้าได้ไหม"},
         {"role": "assistant", "content": "ไม่ได้ครับ เป็นสินค้าต้องห้าม"}]

    def test_M_with_laew_wrapper(self):
        self.assertEqual(resolve_conversation("แล้วแชมพูล่ะ", self.H)["resolved_question"],
                         "แชมพูนำเข้าได้ไหม")

    def test_M_without_laew_wrapper(self):
        self.assertEqual(resolve_conversation("น้ำหอมล่ะ", self.H)["resolved_question"],
                         "น้ำหอมนำเข้าได้ไหม")

    def test_bare_suffix_ignored_without_eligibility_context(self):
        h = [{"role": "user", "content": "ทางรถเรทเท่าไหร่"}, {"role": "assistant", "content": "35"}]
        self.assertEqual(resolve_conversation("เข้าใจละ", h)["resolved_question"], "เข้าใจละ")


class P1Regression(unittest.TestCase):
    def test_transport_rate_followup_preserved(self):
        h = [{"role": "user", "content": "ทางรถเรทเท่าไหร่"}, {"role": "assistant", "content": "35"}]
        self.assertEqual(resolve_conversation("แล้วเรือล่ะ", h)["resolved_question"], "ขอเรททางเรือ")

    def test_transport_duration_followup_preserved(self):
        h = [{"role": "user", "content": "ทางรถใช้เวลากี่วัน"}, {"role": "assistant", "content": "7-10 วัน"}]
        self.assertIn("เรือ", resolve_conversation("แล้วเรือล่ะ", h)["resolved_question"])

    def test_warehouse_contact_continuation_preserved(self):
        h = [{"role": "user", "content": "ขอเบอร์โกดัง"},
             {"role": "assistant", "content": "ไทยหรือจีนคะ"},
             {"role": "user", "content": "ไทย"},
             {"role": "assistant", "content": "064-224-7205"}]
        r = resolve_conversation("แล้วจีนล่ะ", h)["resolved_question"]
        self.assertIn("เบอร์", r)
        self.assertIn("จีน", r)

    def test_warehouse_address_continuation_preserved(self):
        h = [{"role": "user", "content": "ขอที่อยู่โกดัง"},
             {"role": "assistant", "content": "ไทยหรือจีนคะ"},
             {"role": "user", "content": "ไทย"},
             {"role": "assistant", "content": "..."}]
        r = resolve_conversation("แล้วจีนล่ะ", h)["resolved_question"]
        self.assertIn("ที่อยู่", r)
        self.assertIn("จีน", r)

    def test_plain_general_question_not_decomposed(self):
        s = decompose_request("จีนอยู่ทวีปอะไร")
        self.assertFalse(s.is_multi_component())


class PlannerIntegration(unittest.TestCase):
    def test_multi_component_goal_and_field(self):
        plan = plan_answer("แบตเตอรี่ น้ำหอม น้ำยาซักผ้า นำเข้าได้ไหม", "prohibited_goods",
                            ["prohibited_list"], {}, chunks=[],
                            requested_components=["แบตเตอรี่ / eligibility", "น้ำหอม / eligibility",
                                                  "น้ำยาซักผ้า / eligibility"])
        self.assertEqual(len(plan["requested_components"]), 3)
        self.assertIn("EVERY", plan["answer_goal"])

    def test_comparison_shape(self):
        plan = plan_answer("รถกับเรืออันไหนถูกกว่า", "unknown", [], {}, chunks=[],
                            requested_components=["รถ / rate", "เรือ / rate"], comparison="cheaper")
        self.assertEqual(plan["response_shape"], "comparison")
        self.assertIn("cheaper", plan["answer_goal"])

    def test_single_component_plan_unchanged(self):
        plan = plan_answer("ค่าส่งทางรถเท่าไหร่", "shipping_rate", ["rate_per_kg", "rate_per_cbm"],
                            {"transport": "รถ"}, chunks=[])
        self.assertEqual(plan["requested_components"], [])
        self.assertNotIn("EVERY", plan["answer_goal"])

    def test_prompt_block_lists_components_and_classification_clause(self):
        block = _build_answer_plan_block({
            "answer_goal": "g", "response_shape": "answer_then_details",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "requested_components": ["แบตเตอรี่ / eligibility", "น้ำหอม / eligibility"],
        })
        self.assertIn("แบตเตอรี่ / eligibility", block)
        self.assertIn("Retrieved Context", block)
        self.assertIn("liquids", block)


if __name__ == "__main__":
    unittest.main()
