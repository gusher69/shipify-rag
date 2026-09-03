# -*- coding: utf-8 -*-
"""SEM-1 — private-record status-inquiry semantic routing.

A HUMAN question about the STATE of the customer's OWN record / account
must enter the matching Business Action's collection flow (acknowledge →
ask for the missing identifier), never fall through to RAG and dead-end
on "ไม่มีข้อมูลยืนยัน".

These tests exercise the pure concept recognizer plus the REAL
DecisionEngine against a seeded registry that mirrors the production
Status-Inquiry actions. They deliberately use UNSEEN paraphrases — the
production code must recognize the *concept*, not a phrase list.
"""
import unittest
from unittest.mock import MagicMock, patch

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _engine_with_registry, _seed_action, _fake_playground_result
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import _classify_private_state_inquiry


def _seed_status_registry(reg):
    for key, cat, kws in [
        ("searchdatashipmentlist", "Customer Shipment Retrieval", ["พัสดุล่าสุด", "รายการพัสดุ"]),
        ("searchdatashipment", "Customer Shipment Retrieval", ["เลขบิลขนส่ง", "พัสดุเดียว"]),
        ("searchdataorderlist", "Customer Order Retrieval", ["รายการสั่งซื้อ", "ออเดอร์ล่าสุด"]),
        ("searchdatatracking", "Customer Shipment Retrieval", ["แทร็กจีน", "tracking"]),
        ("getdatacustomer", "Customer Data Retrieval", ["ข้อมูลลูกค้า", "ยอดเงิน"]),
    ]:
        aid = _seed_action(reg, key=key, action_type="API", category=cat, keywords=kws)
        reg.replace_parameters(aid, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        reg.upsert_execution(aid, {"endpoint": f"https://erp.invalid/{key}", "http_method": "POST"})


class TestPrivateStateConcept(unittest.TestCase):
    """The recognizer is compositional, not a phrase table — unseen
    wordings in each domain must resolve to the right domain, and public
    / how-to / operational-write wordings must NOT fire."""

    def _dom(self, msg):
        r = _classify_private_state_inquiry(msg)
        return r["domain"] if r else None

    def test_shipment_paraphrases(self):
        for m in ["ของที่ส่งมาถึงไหนแล้ว", "พัสดุผมเป็นไงบ้าง", "ของผมเข้าไทยยัง",
                  "สินค้าถึงโกดังหรือยัง", "เช็กของผมให้หน่อย", "วันนี้มีของเข้าไทยไหมคะ"]:
            self.assertEqual(self._dom(m), "shipment", m)

    def test_order_paraphrases(self):
        for m in ["ออเดอร์ที่ผมสั่งไปถึงไหน", "ร้านส่งของให้ผมหรือยัง", "เช็กคำสั่งซื้อของผมที",
                  "ร้านส่งหรือยังคะ"]:
            self.assertEqual(self._dom(m), "order", m)

    def test_wallet_and_coupon_paraphrases(self):
        for m in ["เงินในระบบผมเหลือเท่าไหร่", "ยอดของผมมีเท่าไร", "เงินที่เติมเข้าไปมาหรือยัง",
                  "ในบัญชีผมมีคูปองอะไร", "ตอนนี้ผมเหลือคูปองไหม"]:
            self.assertEqual(self._dom(m), "customer_data", m)

    def test_tracking_paraphrases(self):
        for m in ["ขอแทรคไทยค่ะ", "เลขแทร็กจีนของผมสถานะอะไร"]:
            self.assertEqual(self._dom(m), "tracking", m)

    def test_public_and_howto_never_fire(self):
        for m in ["คูปองใช้ยังไง", "ใช้คูปองยังไง", "ค่าขนส่งคิดยังไง", "ขอเบอร์ติดต่อ",
                  "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง", "มีบริการอะไรบ้าง", "ขนส่งเอกชนมีอะไรบ้าง",
                  "คูปองใช้ไม่หมด คืนได้ไหมคะ", "มีขั้นต่ำในการสั่งไหม",
                  "ถอนเงินสั่งซื้อยังไง", "โหลดใบกำกับยังไง", "ขอที่อยู่โกดังจีน",
                  "ระยะเวลาการส่งจากร้านจีน -โกดังจีน"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)

    def test_operational_write_never_fires(self):
        # SEM-1 point 6 — a WRITE / change request is not a status inquiry
        # and must be left for the Human Handoff phase, not this fix.
        for m in ["ต้องการแก้จำนวนสินค้าในบิล", "สามารถเปลี่ยนเป็นจัดส่งทางรถได้ไหมคะ",
                  "ยกเลิกบิลสั่งซื้อได้ไหม", "เปลี่ยนที่อยู่พัสดุให้หน่อย", "บิลซ้ำค่ะ"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)


class TestPrivateStateRouting(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_status_registry(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _route(self, msg):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)) as rag:
            res = self.engine.decide(msg, history=[], context={"channel": "line", "developer_mode": True,
                                                              "customer_context": {}})
        return res, rag

    def test_private_state_inquiry_enters_erp_collection_not_rag(self):
        for m in ["สินค้าถึงโกดังหรือยัง", "ร้านส่งของให้ผมหรือยัง", "ของผมเข้าไทยยัง",
                  "ยอดของผมเหลือเท่าไหร่", "ขอแทรคไทยค่ะ", "วันนี้มีของเข้าไทยไหมคะ"]:
            res, rag = self._route(m)
            self.assertEqual(res["routing"]["type"], "WORKFLOW", m)
            dev = res.get("developer") or {}
            self.assertEqual(dev.get("private_state_inquiry", {}).get("intent"), "status_inquiry", m)
            self.assertEqual(dev.get("selection_source"), "private_state_inquiry", m)
            reply = (res.get("reply") or {}).get("text") or ""
            self.assertNotIn("ยังไม่มีข้อมูล", reply)
            self.assertNotIn("ไม่มีข้อมูลยืนยัน", reply)

    def test_public_question_still_reaches_rag_and_no_identity_prompt(self):
        for m in ["คูปองใช้ยังไง", "ค่าขนส่งคิดยังไง", "ขอเบอร์ติดต่อ"]:
            res, rag = self._route(m)
            self.assertEqual(res["routing"]["type"], "RAG", m)
            reply = (res.get("reply") or {}).get("text") or ""
            for bad in ("ยืนยันตัวตน", "เบอร์โทรที่ผูก", "รหัสลูกค้า"):
                self.assertNotIn(bad, reply, m)

    def test_domain_routes_to_matching_capability(self):
        def _sel(res):
            dev = res["developer"] or {}
            ics = dev.get("information_collection_status") or {}
            return dev.get("selected_business_action") or ics.get("selected_business_action")
        self.assertEqual(_sel(self._route("ยอดของผมเหลือเท่าไหร่")[0]), "getdatacustomer")
        self.assertEqual(_sel(self._route("ออเดอร์ที่ผมสั่งไปถึงไหน")[0]), "searchdataorderlist")
        self.assertEqual(_sel(self._route("ของผมเข้าไทยยัง")[0]), "searchdatashipmentlist")


if __name__ == "__main__":
    unittest.main()
