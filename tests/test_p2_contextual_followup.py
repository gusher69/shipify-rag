"""P2A — contextual follow-up ("correct answer + ONE useful next question").

Deterministic. No LLM / no network. Covers the follow-up POLICY
(decide_followup), the FAQ-direct purpose renderer, the prompt render,
and the plan wiring. The 3 real synthesis phrasings are checked
separately/live, not here.
"""
import unittest

from rag.query_resolution import decompose_request
from rag.query_understanding import classify_actionable_intent
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


class BlockerSingleEligibility(unittest.TestCase):
    def test_single_product_gets_enriched_query(self):
        from rag.query_resolution import single_eligibility_component
        s = decompose_request("น้ำหอมนำเข้าได้ไหม", raw_question="น้ำหอมนำเข้าได้ไหม")
        c = single_eligibility_component(s)
        self.assertIsNotNone(c)
        self.assertEqual(c[0], "น้ำหอม / eligibility")
        self.assertIn("ของเหลว", c[1])
        self.assertIn("สินค้าต้องห้าม", c[1])

    def test_unknown_product_also_enriched(self):
        from rag.query_resolution import single_eligibility_component
        s = decompose_request("แก้วน้ำ นำเข้าได้ไหม", raw_question="แก้วน้ำ นำเข้าได้ไหม")
        self.assertEqual(single_eligibility_component(s)[0], "แก้วน้ำ / eligibility")

    def test_multi_entity_returns_none(self):
        from rag.query_resolution import single_eligibility_component
        s = decompose_request("แบตเตอรี่ น้ำหอม นำเข้าได้ไหม", raw_question="แบตเตอรี่ น้ำหอม นำเข้าได้ไหม")
        self.assertIsNone(single_eligibility_component(s))

    def test_non_eligibility_returns_none(self):
        from rag.query_resolution import single_eligibility_component
        s = decompose_request("ค่าส่งทางรถเท่าไหร่", raw_question="ค่าส่งทางรถเท่าไหร่")
        self.assertIsNone(single_eligibility_component(s))


class BlockerConsumeFollowupAnswer(unittest.TestCase):
    H = [{"role": "user", "content": "อยากนำเข้าสินค้าจากจีน"},
         {"role": "assistant", "content": "ยินดีค่ะ คุณลูกค้าต้องการนำเข้าสินค้าประเภทไหนคะ"}]

    def _resolve(self, msg, history=None):
        from rag.clarification_state import resolve_clarification_answer
        return resolve_clarification_answer(msg, history or self.H)

    def test_F_perfume_reply_becomes_eligibility_question(self):
        self.assertEqual(self._resolve("น้ำหอมครับ")["resolved_question"], "น้ำหอมนำเข้าได้ไหม")

    def test_G_shampoo_reply(self):
        self.assertEqual(self._resolve("แชมพูครับ")["resolved_question"], "แชมพูนำเข้าได้ไหม")

    def test_H_unknown_product_reply(self):
        self.assertEqual(self._resolve("แก้วน้ำครับ")["resolved_question"], "แก้วน้ำนำเข้าได้ไหม")

    def test_I_no_elicitation_no_reconstruction(self):
        hist = [{"role": "user", "content": "สวัสดีครับ"},
                {"role": "assistant", "content": "สวัสดีค่ะ ยินดีให้บริการค่ะ"}]
        self.assertIsNone(self._resolve("น้ำหอมครับ", hist))

    def test_warehouse_clarification_still_works(self):
        hist = [{"role": "user", "content": "ขอเบอร์ติดต่อ"},
                {"role": "assistant", "content": "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"}]
        self.assertIn("ไทย", self._resolve("ไทย", hist)["resolved_question"])

    def test_laew_shampoo_untouched_by_composer(self):
        # No elicit-marker question in history -> composer's P2 branch must
        # not fire; the existing P1.2A "แล้ว…ล่ะ" path owns this.
        hist = [{"role": "user", "content": "แบตเตอรี่นำเข้าได้ไหม"},
                {"role": "assistant", "content": "ไม่ได้ครับ เป็นสินค้าต้องห้าม"}]
        self.assertIsNone(self._resolve("แล้วแชมพูล่ะ", hist))


class OfferAlternativeVerdictGate(unittest.TestCase):
    FU = {"needed": True, "purpose": "offer_alternative_product", "question_goal": "x"}

    def _apply(self, text):
        from services.playground_orchestrator import _apply_p2_followup
        return _apply_p2_followup(text, self.FU)

    def test_appended_after_prohibited(self):
        out, note = self._apply("น้ำหอมจัดเป็นของเหลว ไม่สามารถนำเข้าได้ค่ะ")
        self.assertEqual(note, "appended:offer_alternative_product")
        self.assertIn("อย่างอื่น", out)

    def test_suppressed_when_unconfirmed(self):
        _, note = self._apply("ตอนนี้ยังไม่มีข้อมูลยืนยันว่าแก้วน้ำนำเข้าได้หรือไม่ค่ะ")
        self.assertEqual(note, "suppressed-verdict-not-prohibited")

    def test_suppressed_when_unconfirmed_phrased_as_not_prohibited(self):
        _, note = self._apply("แก้วน้ำไม่ได้อยู่ในรายการ ยังไม่มีการยืนยันว่าห้ามนำเข้าค่ะ")
        self.assertEqual(note, "suppressed-verdict-not-prohibited")

    def test_suppressed_when_plain_positive(self):
        _, note = self._apply("แก้วน้ำสามารถนำเข้าได้ค่ะ")
        self.assertEqual(note, "suppressed-verdict-not-prohibited")

    def test_not_duplicated(self):
        _, note = self._apply("ไม่สามารถนำเข้า มีสินค้าอย่างอื่นที่ต้องการให้ช่วยเช็กไหมคะ")
        self.assertEqual(note, "already-served")


class FaqDifferentProductSynthesizes(unittest.TestCase):
    def test_shampoo_row_named_other_product_not_faq_direct(self):
        # A FAQ-exact chunk whose Question names ครีมอาบน้ำ, matched for a
        # "แชมพู" eligibility turn -> plan must NOT be faq_direct.
        faq_chunk = {"text": "Question: ครีมอาบน้ำนำเข้าได้ไหม\nAnswer: ครีมอาบน้ำจัดเป็นของเหลว…",
                      "is_faq_exact": True}
        plan = plan_answer("แชมพูนำเข้าได้ไหม", "prohibited_goods", ["prohibited_list"], {},
                            chunks=[faq_chunk], raw_question="แชมพูนำเข้าได้ไหม",
                            requested_components=["แชมพู / eligibility"])
        self.assertNotEqual(plan["response_shape"], "faq_direct")

    def test_same_product_faq_still_direct(self):
        faq_chunk = {"text": "Question: แบตเตอรี่นำเข้าได้ไหม\nAnswer: แบตเตอรี่เป็นสินค้าต้องห้าม",
                      "is_faq_exact": True}
        plan = plan_answer("แบตเตอรี่นำเข้าได้ไหม", "prohibited_goods", ["prohibited_list"], {},
                            chunks=[faq_chunk], raw_question="แบตเตอรี่นำเข้าได้ไหม",
                            requested_components=["แบตเตอรี่ / eligibility"])
        self.assertEqual(plan["response_shape"], "faq_direct")


class CategoryPolicyHotfix(unittest.TestCase):
    def test_colloquial_eligibility_marker(self):
        from rag.query_resolution import _ELIGIBILITY_INTENT_RE
        from rag.query_understanding import classify_actionable_intent
        self.assertTrue(_ELIGIBILITY_INTENT_RE.search("น้ำจิ้ม น้ำปลา เข้าได้ไหม"))
        self.assertFalse(_ELIGIBILITY_INTENT_RE.search("ฝากนำเข้าได้ไหมคะ"))
        self.assertEqual(classify_actionable_intent("น้ำจิ้ม น้ำปลาเข้าได้ไหม")["actionable_intent"],
                          "prohibited_goods")

    def test_colloquial_multi_entity_decomposes(self):
        s = decompose_request("น้ำจิ้ม น้ำปลา เข้าได้ไหม", raw_question="น้ำจิ้ม น้ำปลา เข้าได้ไหม")
        self.assertEqual(s.entities, ["น้ำจิ้ม", "น้ำปลา"])

    def test_enrichment_covers_category_vocab(self):
        from rag.query_resolution import single_eligibility_component
        s = decompose_request("น้ำยาซักผ้า นำเข้าได้ไหม", raw_question="น้ำยาซักผ้า นำเข้าได้ไหม")
        q = single_eligibility_component(s)[1]
        for term in ("ของเหลว", "อาหาร", "เครื่องดื่ม", "เครื่องสำอาง"):
            self.assertIn(term, q)

    def test_prompt_two_step_category_instruction(self):
        block = _build_answer_plan_block({
            "answer_goal": "g", "response_shape": "answer_then_details",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "requested_components": ["น้ำปลา / eligibility"],
        })
        self.assertIn("run this checklist", block)
        self.assertIn("UNCONFIRMED", block)
        self.assertIn("prohibited-goods LIST only tells you what is NOT allowed", block)


class ProductListContinuation(unittest.TestCase):
    ELIG_HISTORY = [{"role": "user", "content": "น้ำปลา นำเข้าได้ไหม"},
                    {"role": "assistant", "content": "น้ำปลาเป็นของเหลว ไม่สามารถนำเข้าได้ค่ะ"}]

    def _r(self, msg, history):
        from rag.query_resolution import reconstruct_product_list_continuation
        return reconstruct_product_list_continuation(msg, history)

    def test_bare_list_inherits_eligibility(self):
        self.assertEqual(self._r("น้ำเปล่า ละ น้ำมัน น้ำมันงา", self.ELIG_HISTORY),
                          "น้ำเปล่า น้ำมัน น้ำมันงา นำเข้าได้ไหม")

    def test_single_item_not_reconstructed(self):
        self.assertIsNone(self._r("น้ำเปล่า", self.ELIG_HISTORY))

    def test_no_prior_eligibility_context(self):
        hist = [{"role": "user", "content": "สวัสดีครับ"},
                {"role": "assistant", "content": "สวัสดีค่ะ"}]
        self.assertIsNone(self._r("น้ำมัน น้ำเปล่า", hist))

    def test_standalone_no_history(self):
        self.assertIsNone(self._r("น้ำมัน น้ำเปล่า", None))

    def test_message_with_its_own_question_not_reconstructed(self):
        self.assertIsNone(self._r("น้ำมัน ราคาเท่าไหร่", self.ELIG_HISTORY))


if __name__ == "__main__":
    unittest.main()


class FirmCategoryHedge(unittest.TestCase):
    CTX = ("Question: สินค้าที่ห้ามนำเข้ามีอะไรบ้าง\nAnswer: ทางเราไม่รับนำเข้า ... "
           "ของกิน อาหาร ของเหลว เครื่องดื่ม ... "
           "Question: ครีมอาบน้ำนำเข้าได้ไหม\nAnswer: จัดเป็นของเหลว ไม่สามารถนำเข้าได้")

    def _f(self, text):
        from services.playground_orchestrator import _firm_prohibited_category_hedge
        return _firm_prohibited_category_hedge(text, self.CTX)

    def test_firms_classified_liquid_hedge(self):
        out = self._f("- **น้ำยาซักผ้า**: จัดเป็นของเหลว ซึ่งอาจเข้าข่ายสินค้าต้องห้าม")
        self.assertIn("ไม่สามารถนำเข้าได้", out)
        self.assertNotIn("อาจเข้าข่าย", out)

    def test_leaves_conditional_untouched(self):
        # "หากมีของเหลวอยู่ภายใน" is a conditional, not a classification.
        s = "แก้วน้ำอาจเข้าข่ายสินค้าต้องห้ามหากมีของเหลวอยู่ภายใน"
        self.assertEqual(self._f(s), s)

    def test_leaves_unclassified_hedge_untouched(self):
        s = "สำหรับ **น้ำยาซักผ้า** ตอนนี้ยังไม่มีข้อมูลยืนยันในระบบค่ะ"
        self.assertEqual(self._f(s), s)

    def test_no_effect_when_context_has_no_prohibition(self):
        from services.playground_orchestrator import _firm_prohibited_category_hedge
        s = "- **x**: จัดเป็นของเหลว ซึ่งอาจเข้าข่ายสินค้าต้องห้าม"
        self.assertEqual(_firm_prohibited_category_hedge(s, "no policy here"), s)


class VerbFirstEligibility(unittest.TestCase):
    """'(เรา)สามารถนำเข้า <goods> ได้ไหม' — product AFTER the verb."""
    def _spec(self, q):
        return decompose_request(q, raw_question=q)

    def test_battery_verb_first_is_eligibility(self):
        from rag.query_resolution import single_eligibility_component
        s = self._spec("สามารถนำเข้า แบตเตอรี่ได้ไหม")
        self.assertEqual(s.entities, ["แบตเตอรี่"])
        self.assertIsNotNone(single_eligibility_component(s))
        self.assertEqual(classify_actionable_intent("สามารถนำเข้า แบตเตอรี่ได้ไหม")["actionable_intent"],
                          "prohibited_goods")

    def test_verb_first_liquid(self):
        self.assertEqual(self._spec("สามารถนำเข้า น้ำหอมได้ไหม").entities, ["น้ำหอม"])

    def test_samarth_is_not_transport(self):
        from rag.query_resolution import requested_transport_modes
        self.assertEqual(requested_transport_modes("สามารถนำเข้า แบตเตอรี่ได้ไหม"), [])
        self.assertEqual(self._spec("สามารถนำเข้า แบตเตอรี่ได้ไหม").transport_modes, [])

    def test_fak_service_still_excluded(self):
        self.assertEqual(self._spec("ฝากนำเข้าแบตเตอรี่ได้ไหม").entities, [])

    def test_generic_import_from_china_not_eligibility(self):
        self.assertEqual(self._spec("นำเข้าจากจีนได้ไหม").entities, [])
        self.assertEqual(self._spec("นำเข้าสินค้าจากจีนได้ไหม").entities, [])

    def test_product_first_unchanged(self):
        self.assertEqual(self._spec("แบตเตอรี่ นำเข้าได้ไหม").entities, ["แบตเตอรี่"])
