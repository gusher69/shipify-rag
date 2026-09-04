# -*- coding: utf-8 -*-
"""REGRESSION-GATE-1 — Cross-Flow Matrix (CF-01..CF-14).

Every case here opens a PENDING flow with a real turn 1, then sends a
different flow's triggering message as turn 2 in the SAME accumulated
session (`self.say()`, growing `self.h` — never an isolated one-shot
message), and asserts which flow wins per the SYSTEM-STATE-EMERGENCY-1
central arbitration rule (`services.decision_engine.
_current_intent_breaks_pending_flow`). This is the one shared
arbitration function under test throughout — no per-case regex, no
second engine.

Reuses the `_Session` real-registry E2E harness from
`tests.test_system_state_emergency_1` (same fixtures, same verified
customer FT3182).
"""
import unittest
from unittest.mock import patch

from tests.test_decision_engine import _fake_playground_result
from tests.test_system_state_emergency_1 import _Session

_CHARTER_OPEN = [
    {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
    {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
     "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]


class _CF(_Session):
    def say(self, msg, **kw):
        """`_Session.say()` does not stub RAG (the SYSTEM-STATE-EMERGENCY-1
        journey never asserts an exact routing type on a RAG-track turn).
        The Cross-Flow Matrix DOES need a deterministic routing type on
        RAG-track destinations (coupon usage, invoice, transit time, ...),
        so every CF turn stubs `run_playground_turn` the same way
        `test_calculator_regression_2._E2E._say` already does — with no
        real OPENAI_API_KEY in this test env, an unstubbed RAG call
        degrades unpredictably (and slowly: 4 retries x growing backoff)
        into the no-info/Human-CS path, which is a test-harness artifact,
        not a real routing regression."""
        with patch("services.playground_orchestrator.run_playground_turn",
                    return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            return super().say(msg, **kw)

    def _open_shipment_pending(self):
        self.say("ของผมถึงไหนแล้ว")   # -> "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"

    def _open_order_pending(self):
        self.say("ร้านส่งหรือยังคะ")   # -> "กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ" (order-lookup style)

    def _open_calculator_pending(self):
        self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")   # -> asks road/sea

    def _open_charter_pending(self):
        self.h += list(_CHARTER_OPEN)

    def _open_invoice_answered(self):
        self.say("ใบกำกับค่าสินค้าออกได้ไหม")

    def _open_coupon_usage_answered(self):
        self.say("คูปองใช้ยังไงครับ")


# CF-01 — Shipment pending -> Calculator (Calc wins)
class TestCF01ShipmentPendingThenCalculator(_CF):
    def test_calculator_wins(self):
        self._open_shipment_pending()
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertEqual(o["src"], "shipping_estimate_flow")
        self.assertNotIn("เลขที่บิลขนส่ง", o["reply"])


# CF-02 — Calculator pending -> Coupon Usage (public wins)
class TestCF02CalculatorPendingThenCouponUsage(_CF):
    def test_coupon_usage_wins(self):
        self._open_calculator_pending()
        o = self.say("คูปองใช้ยังไงครับ")
        self.assertNotEqual(o["src"], "shipping_estimate_flow")
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")


# CF-03 — Calculator pending -> My Coupons (private wins)
class TestCF03CalculatorPendingThenMyCoupons(_CF):
    def test_my_coupons_wins(self):
        self._open_calculator_pending()
        o = self.say("ผมมีคูปองอะไรบ้าง")
        self.assertNotEqual(o["src"], "shipping_estimate_flow")
        self.assertIn(o["routing"], ("API", "WORKFLOW"))


# CF-04 — Calculator pending -> TC19 (TC19 wins)
class TestCF04CalculatorPendingThenTC19(_CF):
    def test_tc19_wins(self):
        self._open_calculator_pending()
        o = self.say("มีบริการเหมารถไหมคะ")
        self.assertNotEqual(o["src"], "shipping_estimate_flow")


# CF-05 — Charter pending -> Calculator (Calc wins)
class TestCF05CharterPendingThenCalculator(_CF):
    def test_calculator_wins(self):
        self._open_charter_pending()
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertEqual(o["src"], "shipping_estimate_flow")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# CF-06 — Charter pending -> Coupon Usage (wins)
class TestCF06CharterPendingThenCouponUsage(_CF):
    def test_coupon_usage_wins(self):
        self._open_charter_pending()
        o = self.say("คูปองใช้ยังไงครับ")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# CF-07 — Charter pending -> Invoice (wins)
class TestCF07CharterPendingThenInvoice(_CF):
    def test_invoice_wins(self):
        self._open_charter_pending()
        o = self.say("ใบกำกับค่าสินค้าออกได้ไหม")
        self.assertNotEqual(o["src"], "charter_truck_collection")
        self.assertNotIn("รบกวนแจ้งเลขบิล", o["reply"])


# CF-08 — Charter pending -> Shipment Status (wins)
class TestCF08CharterPendingThenShipmentStatus(_CF):
    def test_shipment_status_wins(self):
        self._open_charter_pending()
        o = self.say("ร้านส่งหรือยังคะ")
        self.assertEqual(o["src"], "private_state_inquiry")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# CF-09 — Charter pending -> Transit Time (wins)
class TestCF09CharterPendingThenTransitTime(_CF):
    def test_transit_time_wins(self):
        self._open_charter_pending()
        o = self.say("ทางเรือกี่วันครับ")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# CF-10 — Charter pending -> Address Change (Operational Action wins)
class TestCF10CharterPendingThenAddressChange(_CF):
    def test_address_change_wins(self):
        self._open_charter_pending()
        o = self.say("บิลนี้อยากเปลี่ยนที่อยู่จัดส่งครับ")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# CF-11 — Private (shipment) pending -> Public Transit (Public wins)
class TestCF11PrivatePendingThenPublicTransit(_CF):
    def test_public_transit_wins(self):
        self._open_shipment_pending()
        o = self.say("ทางเรือกี่วันครับ")
        self.assertNotIn("เลขที่บิลขนส่ง", o["reply"])


# CF-12 — Invoice answered -> Shipment Status (wins)
class TestCF12InvoiceThenShipmentStatus(_CF):
    def test_shipment_status_wins(self):
        self._open_invoice_answered()
        o = self.say("ร้านส่งหรือยังคะ")
        self.assertEqual(o["src"], "private_state_inquiry")


# CF-13 — Coupon (usage) answered -> Calculator (wins)
class TestCF13CouponThenCalculator(_CF):
    def test_calculator_wins(self):
        self._open_coupon_usage_answered()
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertEqual(o["src"], "shipping_estimate_flow")


# CF-14 — TC19 complete -> New Calculator (new episode, no stale TC19 slots)
class TestCF14TC19CompleteThenNewCalculator(_CF):
    def test_new_calculator_episode_has_no_charter_leakage(self):
        self._open_charter_pending()
        o = self.say("เลขบิล FT318220260726001 ปลายทางบางนา")
        self.assertEqual(o["src"], "charter_truck_collection")
        o = self.say("ชื่อผู้รับสมชาย เบอร์ 0812345678")
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")   # charter complete
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 5 โล ขนาด 30x30x30")
        self.assertEqual(o["src"], "shipping_estimate_flow")
        self.assertNotIn("FT318220260726001", o["reply"])
        self.assertNotIn("บางนา", o["reply"])


if __name__ == "__main__":
    unittest.main()
