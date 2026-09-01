"""P5 — Stage-aware conversation.

Lead stage (P4) + sentiment (P4.1) tune the DEPTH/PURPOSE of the existing
P2 contextual follow-up. They never change facts, verdicts, retrieval or
ERP behavior. Deterministic, no LLM.
"""
import types
import unittest

from services.answer_planner import decide_followup, plan_answer


def _spec(entities=None, transport_modes=None, comparison=None):
    return types.SimpleNamespace(
        entities=list(entities or []),
        transport_modes=list(transport_modes or []),
        comparison=comparison,
        facets=[], sub_questions=[], corrections={},
    )


def _fu(intent, q, *, spec=None, history=None, lead_stage=None, sentiment_status=None,
        entities=None, answerability="answerable", clarification_required=False,
        conflicting=None):
    return decide_followup(intent, spec or _spec(), q, history or [], answerability,
                           conflicting, clarification_required,
                           lead_stage=lead_stage, sentiment_status=sentiment_status,
                           entities=entities or {})


class ColdBehavior(unittest.TestCase):
    # A
    def test_cold_import_interest_asks_product_type(self):
        r = _fu("unknown", "อยากนำเข้าสินค้าจากจีน", lead_stage="COLD")
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "elicit_product_type")

    # B
    def test_cold_closed_fact_stops(self):
        r = _fu("warehouse_location", "โกดังเปิดกี่โมง", lead_stage="COLD")
        self.assertFalse(r["needed"])

    def test_cold_does_not_get_transport_mode_question(self):
        # rate question, product known, transport missing — but COLD is too early
        r = _fu("shipping_rate", "ค่าส่งเท่าไหร่", lead_stage="COLD",
                entities={"topic": "รองเท้า"})
        self.assertFalse(r["needed"])


class WarmBehavior(unittest.TestCase):
    # C
    def test_warm_known_product_and_transport_no_followup(self):
        r = _fu("shipping_duration", "ทางรถกี่วัน", lead_stage="WARM",
                entities={"topic": "รองเท้า", "transport": "รถ"})
        self.assertFalse(r["needed"])          # product + transport already known

    # D
    def test_warm_evaluation_asks_one_transport_mode(self):
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่", lead_stage="WARM",
                entities={"topic": "รองเท้า"})
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "elicit_transport_mode")

    def test_warm_transport_mode_not_asked_when_product_unknown(self):
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่", lead_stage="WARM", entities={})
        self.assertFalse(r["needed"])

    def test_warm_transport_mode_not_asked_when_transport_known(self):
        r = _fu("shipping_rate", "ค่าขนส่งทางเรือเท่าไหร่", lead_stage="WARM",
                spec=_spec(transport_modes=["เรือ"]), entities={"topic": "รองเท้า"})
        self.assertFalse(r["needed"])

    # E
    def test_warm_prohibited_offers_one_alternative_only(self):
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", lead_stage="WARM",
                spec=_spec(entities=["น้ำหอม"]), entities={"topic": "น้ำหอม"})
        self.assertTrue(r["needed"])
        self.assertEqual(r["purpose"], "offer_alternative_product")


class HotBehavior(unittest.TestCase):
    # F — an exploratory purpose that would fire for COLD/WARM is suppressed for HOT
    def test_hot_suppresses_exploratory_product_question(self):
        r = _fu("unknown", "อยากนำเข้าสินค้าจากจีน", lead_stage="HOT")
        self.assertFalse(r["needed"])

    # G — clarification/required-slot turns never get a P5 question, any stage
    def test_hot_missing_slot_clarification_wins(self):
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", lead_stage="HOT",
                clarification_required=True)
        self.assertFalse(r["needed"])

    # H
    def test_hot_closed_faq_stops(self):
        r = _fu("warehouse_contact", "ขอเบอร์ติดต่อ", lead_stage="HOT")
        self.assertFalse(r["needed"])

    def test_hot_prohibited_no_alternative_offer(self):
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", lead_stage="HOT",
                spec=_spec(entities=["น้ำหอม"]))
        self.assertFalse(r["needed"])

    def test_hot_rate_question_no_transport_prompt(self):
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่", lead_stage="HOT",
                entities={"topic": "รองเท้า"})
        self.assertFalse(r["needed"])


class NegativeOverride(unittest.TestCase):
    # I
    def test_hot_negative_suppresses_all_followup(self):
        r = _fu("prohibited_goods", "ตอบผิดอีกแล้ว น้ำหอมนำเข้าได้ไหม",
                lead_stage="HOT", sentiment_status="NEGATIVE",
                spec=_spec(entities=["น้ำหอม"]))
        self.assertFalse(r["needed"])

    # J
    def test_warm_negative_suppresses_alternative_offer(self):
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", lead_stage="WARM",
                sentiment_status="NEGATIVE", spec=_spec(entities=["น้ำหอม"]))
        self.assertFalse(r["needed"])

    def test_warm_negative_suppresses_transport_prompt(self):
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่ บริการแย่มาก", lead_stage="WARM",
                sentiment_status="NEGATIVE", entities={"topic": "รองเท้า"})
        self.assertFalse(r["needed"])


class NoRepeat(unittest.TestCase):
    # K
    def test_alternative_offer_not_repeated(self):
        hist = [{"role": "assistant", "content": "…มีสินค้าอย่างอื่นที่ต้องการให้ช่วยเช็กไหมคะ"}]
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", lead_stage="WARM",
                spec=_spec(entities=["น้ำหอม"]), history=hist)
        self.assertFalse(r["needed"])

    def test_transport_mode_not_repeated(self):
        hist = [{"role": "assistant", "content": "รบกวนสอบถามว่าสนใจส่งทางรถหรือทางเรือคะ"}]
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่", lead_stage="WARM",
                entities={"topic": "รองเท้า"}, history=hist)
        self.assertFalse(r["needed"])

    def test_product_type_not_asked_after_customer_named_product(self):
        # "น้ำหอมครับ" carries a named-product hint -> elicit_product_type never fires
        r = _fu("prohibited_goods", "น้ำหอมครับ", lead_stage="COLD",
                spec=_spec(entities=["น้ำหอม"]))
        self.assertNotEqual(r.get("purpose"), "elicit_product_type")


class NoStageIsUnchangedP2(unittest.TestCase):
    """lead_stage=None (AI Playground / not-yet-scored) -> byte-identical
    to pre-P5 P2 behavior."""

    def test_import_interest_still_asks_product(self):
        r = _fu("unknown", "อยากนำเข้าสินค้าจากจีน")
        self.assertEqual(r["purpose"], "elicit_product_type")

    def test_prohibited_still_offers_alternative(self):
        r = _fu("prohibited_goods", "น้ำหอมนำเข้าได้ไหม", spec=_spec(entities=["น้ำหอม"]))
        self.assertEqual(r["purpose"], "offer_alternative_product")

    def test_closed_fact_still_stops(self):
        self.assertFalse(_fu("warehouse_contact", "ขอเบอร์ติดต่อ")["needed"])

    def test_no_transport_prompt_without_stage(self):
        # elicit_transport_mode is a WARM-only P5 purpose — never fires stage-less
        r = _fu("shipping_rate", "ค่าขนส่งเท่าไหร่", entities={"topic": "รองเท้า"})
        self.assertFalse(r["needed"])


class FactualFidelity(unittest.TestCase):
    # L — stage never changes anything except `followup`
    def _plan(self, **kw):
        return plan_answer("น้ำหอมนำเข้าได้ไหม", "prohibited_goods",
                           ["prohibited_list"], {"topic": "น้ำหอม"}, chunks=[],
                           request_spec=_spec(entities=["น้ำหอม"]),
                           answerability="answerable", history=[], **kw)

    def test_only_followup_field_varies_with_stage(self):
        base = self._plan()
        hot = self._plan(lead_stage="HOT")
        warm_neg = self._plan(lead_stage="WARM", sentiment_status="NEGATIVE")
        for other in ("answer_goal", "required_facts", "optional_facts", "excluded_facts",
                      "response_shape", "clarification_required", "clarification_question"):
            self.assertEqual(base[other], hot[other], msg=other)
            self.assertEqual(base[other], warm_neg[other], msg=other)
        # follow-up: base (no stage) offers alternative; HOT and WARM+NEG do not
        self.assertEqual(base["followup"]["purpose"], "offer_alternative_product")
        self.assertFalse(hot["followup"]["needed"])
        self.assertFalse(warm_neg["followup"]["needed"])


class ProfileLookupShape(unittest.TestCase):
    # M — _run_rag_pipeline reads the already-loaded customer_context,
    # it must not introduce a new profile / lead-stage read.
    def test_run_rag_pipeline_does_not_reload_profile(self):
        import inspect
        from services import decision_engine
        src = inspect.getsource(decision_engine.DecisionEngine._run_rag_pipeline)
        self.assertIn('customer_context', src)
        self.assertIn('lead_stage', src)
        self.assertNotIn("get_profile", src)
        self.assertNotIn("update_lead_stage", src)


if __name__ == "__main__":
    unittest.main()
