# -*- coding: utf-8 -*-
"""CALCULATOR-REGRESSION-2 — REAL LINE routing regression after
SEMANTIC-FIRST-2 / 2.1 (production session 6c9b9026, turn 774).

REAL failure:
  ... "ร้านส่งหรือยังคะ"           -> "กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ"   (leaves a
                                     searchdataorder collection pending)
  ... "ใบกำกับค่าสินค้าออกได้ไหม"  -> invoice policy answer
  User: "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"
  Bot : "กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ"          <-- FAIL

Two root causes, both STATE/ROUTE AUTHORITY (not semantic mis-labelling —
interpret() correctly returns SHIPPING_ESTIMATE):

  1. decision_engine — the Generic Continuation Intent Guard had no path
     for a decisive NEW actionable intent that carries no question
     particle / "…หน่อย" request marker, so the stale searchdataorder
     continuation won. Fix: a decisive current-turn semantic family
     (`_CURRENT_TURN_AUTHORITY_FAMILIES`, confidence >= 0.8, follow_up_op
     NONE) overrides an unrelated pending Business-Action continuation.

  2. shipping_estimate_flow — the widened SEMANTIC-FIRST-2 LLM gate now
     labels a bare route answer ("เอารถครับ") SHIPPING_ESTIMATE /
     SET_VALUE, which `_is_explicit_new_request` treated as a fresh
     calculation, wiping the retained weight + dimensions. Fix: a bare
     route/method answer (no weight / dims / calc verb of its own) is
     always a CONTINUATION value. `_method_of` also learns the
     comparison-connector shape ("ถ้าเป็นเรือล่ะ").
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from services.shipping_estimate_flow import _method_of, _is_explicit_new_request, derive_estimate_state
from services.conversation_semantics import interpret
from tests.test_decision_engine import _fake_playground_result


def _llm_calc(message, history=None):
    """Simulate the widened SEMANTIC-FIRST-2 gate: any conversational
    Thai turn about shipping cost / a bare route word resolves to
    SHIPPING_ESTIMATE (this is what breaks locally-degraded tests)."""
    import re
    s = message or ""
    if re.search(r"คำนวณ|ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ประเมิน|กี่บาท", s):
        return {"family": "SHIPPING_ESTIMATE"}
    if re.fullmatch(r"\s*(?:เอา|ขอ|ใช้|เป็น)?\s*(?:ทาง|โดย)?\s*(?:รถ|เรือ|บก)\s*(?:ครับ|ค่ะ|คะ|นะ)?\s*", s):
        return {"family": "SHIPPING_ESTIMATE"}
    if re.search(r"คูปอง", s) and re.search(r"ยังไง|ตรงไหน", s):
        return {"family": "COUPON_USAGE"}
    return {"family": "UNKNOWN"}


_PEND_ORDER = [{"role": "user", "content": "ร้านส่งหรือยังคะ"},
               {"role": "assistant", "content": "กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ"}]
# a genuine Business-Action order-lookup collection (resolvable by
# _resolve_continuation_action, unlike the seller-dispatch synth above).
_PEND_ORDER_LOOKUP = [{"role": "user", "content": "เช็คสถานะออเดอร์"},
                      {"role": "assistant", "content": "กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ"}]
_PEND_ORDER_THEN_INVOICE = _PEND_ORDER + [
    {"role": "user", "content": "ใบกำกับค่าสินค้าออกได้ไหม"},
    {"role": "assistant", "content": "ทางเราสามารถออกใบกำกับค่าสินค้าและใบเสร็จค่าขนส่งให้ได้ค่ะ "
                                     "ตามเงื่อนไขของบิลและวิธีชำระเงิน หากต้องการใบกำกับภาษี "
                                     "รบกวนแจ้งข้อมูลผู้เสียภาษีและเลขบิลให้เจ้าหน้าที่ตรวจสอบก่อนชำระเงินนะคะ"}]
_PEND_SHIPMENT = [{"role": "user", "content": "ของผมถึงไหนแล้ว"},
                  {"role": "assistant", "content": "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"}]

_CALC_MSG = "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"


class _E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _say(self, msg, history, *, llm=_llm_calc):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_calcrg2",
               "developer_mode": True, "customer_context": {}}
        with patch("services.conversation_semantics._llm_family", side_effect=llm), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=list(history), context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                "broke": dev.get("pending_flow_broken_by_current_intent"),
                "reply": (r.get("reply") or {}).get("text") or ""}


class TestExactRealFailure(_E2E):
    def test_calc_request_after_pending_order_wins(self):
        r = self._say(_CALC_MSG, _PEND_ORDER_THEN_INVOICE)
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIsNone(r["handoff"])
        self.assertNotIn("เลขที่คำสั่งซื้อ", r["reply"])
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])          # asks only the method
        self.assertIn("54x12x43", r["reply"])                   # weight + dims retained
        self.assertIn("2", r["reply"])

    def test_minimal_repro_pending_order_only(self):
        r = self._say(_CALC_MSG, _PEND_ORDER)
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertNotIn("เลขที่คำสั่งซื้อ", r["reply"])

    def test_pending_shipment_also_yields_to_calculator(self):
        r = self._say(_CALC_MSG, _PEND_SHIPMENT)
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])


class TestCalculatorJourneyWithLlm(_E2E):
    """The required 4-turn journey, threaded through ONE engine with the
    widened LLM gate live (mocked) — the environment the real bug needs."""

    def test_journey(self):
        h = []
        r = self._say(_CALC_MSG, h)
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])
        h += [{"role": "user", "content": _CALC_MSG}, {"role": "assistant", "content": r["reply"]}]

        r = self._say("เอารถครับ", h)                          # ROAD, weight+dims retained
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("192.26", r["reply"])
        h += [{"role": "user", "content": "เอารถครับ"}, {"role": "assistant", "content": r["reply"]}]

        r = self._say("ถ้าเป็นเรือล่ะ", h)                      # SEA, reuse weight/dims
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("125.39", r["reply"])
        h += [{"role": "user", "content": "ถ้าเป็นเรือล่ะ"}, {"role": "assistant", "content": r["reply"]}]

        r = self._say("ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล", h)      # NEW episode, no inheritance
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("5", r["reply"])
        self.assertNotIn("54x12x43", r["reply"])
        self.assertNotIn("192.26", r["reply"])
        self.assertNotIn("125.39", r["reply"])


class TestCrossFlowAuthority(_E2E):
    _PEND_CALC = [{"role": "user", "content": _CALC_MSG},
                  {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) "
                                                   "ต้องการประเมินทางรถหรือทางเรือคะ"}]

    def test_A_pending_order_then_calculator(self):
        self.assertEqual(self._say(_CALC_MSG, _PEND_ORDER)["src"], "shipping_estimate_flow")

    def test_B_pending_calculator_then_public_coupon(self):
        r = self._say("คูปองใช้ยังไง", self._PEND_CALC)
        self.assertNotEqual(r["src"], "shipping_estimate_flow")
        self.assertNotEqual(r["routing"], "HUMAN_HANDOFF")

    def test_D_pending_calculator_then_tc19(self):
        r = self._say("มีบริการเหมารถไหมคะ", self._PEND_CALC)
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

    def test_F_pending_order_then_invoice(self):
        r = self._say("ใบกำกับค่าสินค้าออกได้ไหม", _PEND_ORDER)
        self.assertNotIn("เลขที่คำสั่งซื้อ", r["reply"])


class TestShortReplyControlsStillContinue(_E2E):
    def test_route_answer_continues_the_calculator(self):
        h = [{"role": "user", "content": _CALC_MSG},
             {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) "
                                              "ต้องการประเมินทางรถหรือทางเรือคะ"}]
        r = self._say("เอารถครับ", h)
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("192.26", r["reply"])

    _PEND_CHARTER = [
        {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
        {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
         "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]

    def test_slot_value_still_continues_the_charter_collection(self):
        r = self._say("เลขบิล FT318220260726001 ปลายทางบางนา", self._PEND_CHARTER)
        self.assertEqual(r["src"], "charter_truck_collection")
        self.assertIsNone(r["broke"])

    def test_bare_name_and_phone_still_continue_the_charter_collection(self):
        r = self._say("ชื่อผู้รับสมชาย เบอร์ 0812345678", self._PEND_CHARTER)
        self.assertEqual(r["src"], "charter_truck_collection")
        self.assertIsNone(r["broke"])


class TestUnitLevelGuards(unittest.TestCase):
    def test_method_of_comparison_connectors(self):
        self.assertEqual(_method_of("ถ้าเป็นเรือล่ะ"), "sea")
        self.assertEqual(_method_of("ถ้าเป็นทางเรือล่ะ"), "sea")
        self.assertEqual(_method_of("แล้วรถล่ะ"), "road")
        self.assertEqual(_method_of("งั้นเอาเรือ"), "sea")

    def test_method_of_unrelated_sentences_still_none(self):
        for t in ("รถของผมจอดอยู่ไหน", "เรือสินค้ามาถึงหรือยัง",
                  "สนใจนำเข้ารถมอเตอร์ไซค์", "อยากส่งของขึ้นเรือประมง "):
            self.assertIsNone(_method_of(t), t)

    def test_bare_route_answer_is_not_an_explicit_new_request(self):
        fake = interpret.__globals__  # not used; explicit object below
        class _I:  # minimal Interpretation stand-in
            intent_family = "SHIPPING_ESTIMATE"
            follow_up_op = "SET_VALUE"
            confidence = 0.6
        self.assertFalse(_is_explicit_new_request("เอารถครับ", _I()))
        self.assertFalse(_is_explicit_new_request("เรือค่ะ", _I()))

    def test_real_new_request_with_content_is_still_explicit(self):
        class _I:
            intent_family = "SHIPPING_ESTIMATE"
            follow_up_op = "NONE"
            confidence = 0.85
        self.assertTrue(_is_explicit_new_request("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล", _I()))
        self.assertTrue(_is_explicit_new_request("ค่าส่งเท่าไหร่ น้ำหนัก 5 กิโล", _I()))

    def test_bare_route_answer_continues_thread_in_derive_state(self):
        h = [{"role": "user", "content": _CALC_MSG},
             {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) "
                                              "ต้องการประเมินทางรถหรือทางเรือคะ"}]
        with patch("services.conversation_semantics._llm_family", side_effect=_llm_calc):
            st = derive_estimate_state(h, "เอารถครับ", interpretation=interpret("เอารถครับ", h))
        self.assertIsNotNone(st)
        self.assertEqual(st.weight, 2.0)
        self.assertEqual((st.length, st.width, st.height), (54.0, 12.0, 43.0))
        self.assertEqual(st.method, "road")
        self.assertTrue(st.complete())


if __name__ == "__main__":
    unittest.main()
