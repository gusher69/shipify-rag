# -*- coding: utf-8 -*-
"""CUSTOMER-RAG-2.1 — charter-truck (เหมารถ / TC19) multi-turn slot
collection.

REAL LINE (session 6c9b9026):
  turn 726 "มีบริการเหมารถไหมคะ" -> turn 727 the TC19 FAQ answer (PASS)
  turn 728 "เลขบิล FT318220260726001 ปลายทางบางนา"
  -> turn 729 routing API, selected_business_action searchdatashipment,
     "เลขที่บิลขนส่ง: FT318220260726001 ค่ะ"  (FAIL)
The bill identifier resurrected the shipment-status flow; the TC19
collection had no state, so destination "บางนา" was discarded.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.charter_truck_flow import (
    CharterState,
    extract_charter_fields,
    derive_charter_state,
)
from services.decision_engine import DecisionEngine
from tests.test_decision_engine import _fake_playground_result

_TC19 = ("สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ คุณลูกค้าแจ้งเลขบิล และโลเคชั่น"
         "ปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ")
_DONE = ("รับข้อมูลการขอใช้บริการเหมารถครบแล้วค่ะ "
         "เดี๋ยวเจ้าหน้าที่จะติดต่อประสานงานเรื่องเหมารถให้นะคะ")


def _h(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


class TestCharterFieldExtraction(unittest.TestCase):
    def test_partial_any_order(self):
        s = extract_charter_fields("เลขบิล FT318220260726001 ปลายทางบางนา")
        self.assertEqual(s.bill, "FT318220260726001")
        self.assertEqual(s.destination, "บางนา")
        self.assertEqual(s.missing(), ["recipient_name", "recipient_phone"])

        s = extract_charter_fields("ชื่อผู้รับสมชาย เบอร์ 0812345678")
        self.assertEqual(s.recipient_name, "สมชาย")
        self.assertEqual(s.recipient_phone, "0812345678")

        s = extract_charter_fields("FT318220260726001 0812345678")
        self.assertEqual(s.bill, "FT318220260726001")
        self.assertEqual(s.recipient_phone, "0812345678")
        self.assertEqual(s.missing(), ["destination", "recipient_name"])

        s = extract_charter_fields("ปลายทางบางนา ชื่อผู้รับสมชาย")
        self.assertEqual(s.missing(), ["bill", "recipient_phone"])

    def test_all_four_in_one(self):
        s = extract_charter_fields(
            "เลขบิล FT318220260726001 ปลายทางบางนา ชื่อผู้รับสมชาย เบอร์ 0812345678")
        self.assertTrue(s.complete())

    def test_merge_across_turns(self):
        s = CharterState(bill="FT318220260726001", destination="บางนา")
        extract_charter_fields("ผู้รับชื่อสมชาย เบอร์ 0812345678", s)
        self.assertTrue(s.complete())

    def test_derive_opens_after_tc19_answer_and_closes_after_done(self):
        h = _h(("user", "มีบริการเหมารถไหมคะ"), ("assistant", _TC19),
                ("user", "เลขบิล FT318220260726001 ปลายทางบางนา"))
        st = derive_charter_state(h)
        self.assertIsNotNone(st)
        self.assertEqual(st.bill, "FT318220260726001")
        self.assertEqual(st.destination, "บางนา")

        h2 = h + _h(("assistant", "รบกวนแจ้งชื่อผู้รับ และ เบอร์โทรผู้รับเพิ่มเติมด้วยนะคะ"),
                    ("user", "สมชาย 0812345678"), ("assistant", _DONE),
                    ("user", "ขอบคุณค่ะ"))
        self.assertIsNone(derive_charter_state(h2))  # done -> closed

    def test_no_open_without_tc19(self):
        self.assertIsNone(derive_charter_state(_h(("user", "เช็กพัสดุ FT318220260726001"))))


class TestCharterRoutingE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _run(self, msg, hist):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_ct21",
               "developer_mode": True, "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=hist, context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "action": dev.get("selected_business_action"),
                "handoff": r.get("handoff_payload"),
                "reply": (r.get("reply") or {}).get("text") or ""}

    _OPEN = _h(("user", "มีบริการเหมารถไหมคะ"), ("assistant", _TC19))

    def test_A_bill_plus_destination_asks_only_name_and_phone(self):
        r = self._run("เลขบิล FT318220260726001 ปลายทางบางนา", self._OPEN)
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["src"], "charter_truck_collection")
        self.assertIsNone(r["action"])           # NOT searchdatashipment
        self.assertIsNone(r["handoff"])
        self.assertIn("ชื่อผู้รับ", r["reply"])
        self.assertIn("เบอร์โทรผู้รับ", r["reply"])
        self.assertNotIn("เลขที่บิลขนส่ง:", r["reply"])   # not the shipment-status reply

    def test_B_final_slots_complete_triggers_charter_handoff(self):
        hist = self._OPEN + _h(
            ("user", "เลขบิล FT318220260726001 ปลายทางบางนา"),
            ("assistant", "รับทราบค่ะ (เลขบิล FT318220260726001 • ปลายทาง บางนา) รบกวนแจ้งชื่อผู้รับ และ เบอร์โทรผู้รับเพิ่มเติมด้วยนะคะ"))
        r = self._run("ชื่อผู้รับสมชาย เบอร์ 0812345678", hist)
        self.assertEqual(r["routing"], "HUMAN_HANDOFF")
        self.assertEqual(r["handoff"]["reason"], "charter_truck_request")
        d = r["handoff"]["details"]
        self.assertEqual(d, {"bill": "FT318220260726001", "destination": "บางนา",
                             "recipient_name": "สมชาย", "recipient_phone": "0812345678"})
        for f in ("FT318220260726001", "บางนา", "สมชาย", "0812345678"):
            self.assertIn(f, r["handoff"]["summary"])
        # neutral reply — no promise (webhook adds it only if notified)
        self.assertNotIn("เจ้าหน้าที่", r["reply"])

    def test_C_all_four_in_one_message_hands_off_directly(self):
        r = self._run("เลขบิล FT318220260726001 ปลายทางบางนา ชื่อผู้รับสมชาย เบอร์ 0812345678", self._OPEN)
        self.assertEqual(r["routing"], "HUMAN_HANDOFF")
        self.assertEqual(r["handoff"]["reason"], "charter_truck_request")

    def test_D_field_order_independent(self):
        r = self._run("ปลายทางบางนา ชื่อผู้รับสมชาย", self._OPEN)
        self.assertEqual(r["src"], "charter_truck_collection")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("เลขบิล", r["reply"])
        self.assertIn("เบอร์โทรผู้รับ", r["reply"])

    def test_E_unrelated_shipment_status_unchanged(self):
        r = self._run("เช็กพัสดุ FT318220260726001", [])
        self.assertNotEqual(r["src"], "charter_truck_collection")
        self.assertIn(r["routing"], ("WORKFLOW", "API"))

    def test_F_tc19_faq_fresh_turn_unchanged(self):
        r = self._run("มีบริการเหมารถไหมคะ", [])
        self.assertNotEqual(r["src"], "charter_truck_collection")
        self.assertIsNone(r["handoff"])


if __name__ == "__main__":
    unittest.main()
