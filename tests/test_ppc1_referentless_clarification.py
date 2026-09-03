# -*- coding: utf-8 -*-
"""PPC-1 — a referent-less underspecified question must be CLARIFIED,
never answered by a fresh RAG search whose closest lexical neighbour can
be a stale / adjacent FAQ chunk.

REAL LINE failure (2026-09-03 ~15:49 ICT, session 6c9b9026):
    ... shipment status -> public sea-shipping duration ->
    user: "สั่งเยอะได้ไหม"
    bot: "สามารถสั่งสินค้าได้เยอะ... หากเป็นสินค้าประเภทแบตเตอรี่ ทางเรา
          ไม่รับนำเข้า..."
The battery clause came from a retrieval chunk ("สั่งแบตจำนวนเยอะได้ไหม"),
the near-exact lexical neighbour of the bare query — not from the
conversation. The message named no product / quantity / service and the
immediate turns established none, so the correct move is one natural
clarifying question.

Invariant: current explicit intent > stale history. An immediate product
referent ("สนใจนำเข้ารองเท้า" the turn before) is inherited; an old,
unrelated topic several turns back is not resurrected.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import (
    _is_referentless_underspecified,
    _recent_product_referent,
)
from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _engine_with_registry, _seed_action, _fake_playground_result


def _hist(*pairs):
    out = []
    for u, a in pairs:
        out.append({"role": "user", "content": u})
        if a is not None:
            out.append({"role": "assistant", "content": a})
    return out


_A_HIST = _hist(
    ("ของผมเข้าไทยหรือยัง", "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"),
    ("ทางเรือกี่วัน", "ระยะเวลาขนส่งทางเรือประมาณ 14–20 วันค่ะ"),
)
_SHOE_HIST = _hist(("สนใจนำเข้ารองเท้า", "ยินดีให้บริการนำเข้ารองเท้าค่ะ"))
_BATT_HIST = _hist(("สนใจนำเข้าแบตเตอรี่", "แบตเตอรี่เป็นสินค้าต้องห้ามค่ะ"))
_STALE_BATT_HIST = _hist(
    ("สนใจนำเข้าแบตเตอรี่", "แบตเตอรี่เป็นสินค้าต้องห้ามค่ะ"),
    ("คูปองใช้ยังไง", "ใช้ลดค่านำเข้าและค่าส่งในไทยค่ะ"),
    ("ทางเรือกี่วัน", "ประมาณ 14–20 วันค่ะ"),
)


class TestReferentlessRecognizer(unittest.TestCase):
    def test_bare_capability_and_attribute_questions_are_referentless(self):
        for m in ["สั่งเยอะได้ไหม", "ราคาเท่าไหร่", "มีไหม", "อันนี้ได้ไหม",
                  "เอาเยอะได้ไหม", "ทำได้ไหม", "เยอะได้ไหม"]:
            self.assertTrue(_is_referentless_underspecified(m), m)

    def test_interrogative_marker_is_required(self):
        # a bare request verb, no question phrasing -> NOT forced to clarify here
        for m in ["ขอข้อมูล", "ขอรายละเอียด", "เช็กให้หน่อย", "ดูให้หน่อย"]:
            self.assertFalse(_is_referentless_underspecified(m), m)

    def test_a_named_topic_survives_the_strip(self):
        for m in ["สั่งรองเท้าเยอะได้ไหม", "ค่าส่งเท่าไหร่", "คูปองใช้ยังไง",
                  "มีขั้นต่ำในการสั่งไหม", "ขอเบอร์ติดต่อ", "โกดังอยู่ที่ไหน",
                  "นำเข้าจากจีนยังไง", "ทางเรือกี่วัน"]:
            self.assertFalse(_is_referentless_underspecified(m), m)

    def test_long_message_is_never_treated_as_bare(self):
        self.assertFalse(_is_referentless_underspecified(
            "อยากทราบว่าถ้าจะสั่งของจำนวนมากจากจีนมีขั้นตอนยังไงบ้างคะ"))

    def test_recent_referent_needs_intent_marker_plus_noun(self):
        self.assertTrue(_recent_product_referent(_SHOE_HIST))
        self.assertTrue(_recent_product_referent(_BATT_HIST))
        # a prior status question / logistics fact is NOT a product referent
        self.assertFalse(_recent_product_referent(_A_HIST))
        self.assertFalse(_recent_product_referent(_STALE_BATT_HIST))
        self.assertFalse(_recent_product_referent([]))


class TestReferentlessRouting(unittest.TestCase):
    """REAL DecisionEngine over a seeded fake registry; RAG + ERP faked.
    A seeded (not live-DB) registry keeps this immune to the known
    _CONFIG_CACHE cross-test leak."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_action(self.reg, key="faq_generic", action_type="RAG", category="faq",
                     keywords=["ค่าส่ง", "ราคาส่ง", "คูปอง", "ขั้นต่ำ", "โกดัง", "ทางเรือ", "ทางรถ",
                               "เบอร์ติดต่อ", "นำเข้า", "รองเท้า"])
        det = _seed_action(self.reg, key="searchdatashipment", action_type="API",
                           category="Customer Shipment Retrieval", keywords=["เลขบิลขนส่ง", "พัสดุเดียว"])
        self.reg.replace_parameters(det, [
            {"name": "CustCode", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
            {"name": "ShipmentCode", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{6,}$"},
        ])
        self.reg.upsert_execution(det, {"endpoint": "https://erp.invalid/ship", "http_method": "POST"})
        self.eng = _engine_with_registry(self.reg)

    def _route(self, msg, history):
        ctx = {"channel": "line", "tenant_id": "default",
               "external_user_id": "U_ppc1_test", "developer_mode": True,
               "customer_context": {}}
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG-STUB]", confidence=0.9)), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})):
            r = self.eng.decide(msg, history=history, context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "reply": (r.get("reply") or {}).get("text") or "",
                "handoff": (r.get("handoff_payload") or {}).get("reason")}

    def _assert_clarified(self, res, msg):
        self.assertEqual(res["routing"], "WORKFLOW", msg)
        self.assertEqual(res["src"], "clarification_referentless_underspecified", msg)
        self.assertNotIn("แบตเตอรี่", res["reply"], msg)
        self.assertIsNone(res["handoff"], msg)
        self.assertTrue(res["reply"].strip(), msg)

    def _assert_rag(self, res, msg):
        self.assertIn(res["routing"], ("RAG", "GENERAL"), msg)
        self.assertNotEqual(res["src"], "clarification_referentless_underspecified", msg)

    # A — the exact REAL failure
    def test_A_real_failure_shipment_then_sea_then_bare_order_qty(self):
        self._assert_clarified(self._route("สั่งเยอะได้ไหม", _A_HIST), "A")

    # B — immediate product context is inherited
    def test_B_immediate_product_context_goes_to_rag(self):
        self._assert_rag(self._route("สั่งเยอะได้ไหม", _SHOE_HIST), "B")

    # C — immediate prohibited-product context is inherited (battery answer OK)
    def test_C_immediate_prohibited_product_context_goes_to_rag(self):
        self._assert_rag(self._route("สั่งเยอะได้ไหม", _BATT_HIST), "C")

    # D — no context at all
    def test_D_no_context_clarifies(self):
        self._assert_clarified(self._route("สั่งเยอะได้ไหม", []), "D")

    # D2 — stale battery several turns back must NOT be resurrected
    def test_D2_stale_battery_context_does_not_leak(self):
        self._assert_clarified(self._route("สั่งเยอะได้ไหม", _STALE_BATT_HIST), "D2")

    # E — bare price question, no referent
    def test_E_bare_price_question_clarifies(self):
        self._assert_clarified(self._route("ราคาเท่าไหร่", _A_HIST), "E")

    # F — bare existence question WITH an immediate referent
    def test_F_bare_existence_with_referent_goes_to_rag(self):
        hist = _hist(("สนใจนำเข้ากระเป๋า", "ยินดีให้บริการค่ะ"))
        self._assert_rag(self._route("มีไหม", hist), "F")

    # G — public / clarification regressions: a NAMED public question is untouched
    def test_G_named_public_questions_still_reach_rag(self):
        for m in ["คูปองใช้ยังไง", "ค่าส่งเท่าไหร่", "สั่งรองเท้าเยอะได้ไหม",
                  "ทางเรือกี่วัน", "มีขั้นต่ำในการสั่งไหม", "โกดังรับสินค้าอยู่ที่ไหน"]:
            self._assert_rag(self._route(m, []), m)

    # H — Fix-2 handoff regression: a clearly-understood company question
    # with genuinely no info still escalates, it does NOT get swallowed as
    # a clarification.
    def test_H_fix2_handoff_still_escalates(self):
        res = self._route("Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม", [])
        self.assertNotEqual(res["src"], "clarification_referentless_underspecified")


if __name__ == "__main__":
    unittest.main()
