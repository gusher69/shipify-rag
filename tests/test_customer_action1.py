# -*- coding: utf-8 -*-
"""CUSTOMER-ACTION-1 — operational change / verify requests with NO
executable Business Action (S02 / S03 / S04 / S11 / S13 / S15 / G18).

Customer-approved behaviour: acknowledge + ask for the ONE required
identifier, then hand to Human CS — never a no-info dead-end, never a
fake success, never an unrelated ERP read. Semantic-First understands
the request; the deterministic workflow decides what happens.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.operational_change_flow import (
    classify_operational_request, derive_operational_state,
    extract_operational_fields, OperationalState, operational_handoff_summary,
)
from services.decision_engine import DecisionEngine

LINE_UID = "Uc5f5717bc090934f9eaa067513388178"
CUST = "FT3182"
_BIND = {"cust_code": CUST, "status": "verified", "channel": "line",
         "external_user_id": LINE_UID, "tenant_id": "default"}


class TestRecognizer(unittest.TestCase):
    def test_each_case_maps_to_its_kind(self):
        cases = {
            "modify_bill_qty": ["ต้องการแก้จำนวนสินค้าในบิล", "ขอแก้จำนวนสินค้าที่สั่งในบิลนี้"],
            "change_shipping_method": ["สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ",
                                       "ขอเปลี่ยนบิล PO318220260806008 เป็นจัดส่งทางเรือ",
                                       "อยากเปลี่ยนวิธีส่งเป็นทางรถ"],
            "change_carrier_or_selfpickup": ["บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง",
                                             "ขอเปลี่ยนบิลเป็นส่งเอกชนได้ไหมคะ"],
            "add_vat": ["ลืมเลือก VAT ไปค่ะ ต้องการVATด้วยค่ะ", "อยากได้ VAT เพิ่มในบิลค่ะ"],
            "duplicate_bill": ["บิลซ้ำค่ะ", "มีบิลซ้ำในระบบช่วยลบให้หน่อย"],
            "verify_warehouse_address": ["ใส่ที่อยู่โกดังจีนถูกไหมคะ", "ช่วยเช็คที่อยู่โกดังจีนที่กรอกไปหน่อย"],
            "topup_not_credited": ["ยอดเงินไม่เข้า เติมเงินแล้วรอตรวจสอบ", "เติมเงินแล้วยอดยังไม่เข้าเลยค่ะ"],
        }
        for kind, msgs in cases.items():
            for m in msgs:
                r = classify_operational_request(m)
                self.assertIsNotNone(r, m)
                self.assertEqual(r["kind"], kind, m)

    def test_does_not_steal_neighbouring_intents(self):
        for m in ["รับสินค้าเองได้ไหมคะ",             # SELF_PICKUP FAQ
                  "ขอที่อยู่โกดังจีน",                  # warehouse FAQ
                  "เปลี่ยนที่อยู่จัดส่งบิลนี้",        # S09 — has its own BA
                  "ขอใบกำกับภาษีครับ",                  # invoice issuance
                  "ใบกำกับค่าสินค้าออกได้ไหม",          # invoice issuance
                  "โหลดใบกำกับยังไง",                   # invoice download
                  "ต้องการใบกำกับภาษี",                 # invoice issuance
                  "ทางเรือ", "รถครับ", "ถ้าเป็นทางเรือล่ะ",   # calculator route answers
                  "ยอดเงินผมเหลือเท่าไหร่",             # wallet read
                  "ยกเลิกบิลสั่งซื้อได้ไหม"]:           # G21 — policy RAG answer
            self.assertIsNone(classify_operational_request(m), m)


class TestStateAndSummary(unittest.TestCase):
    def test_open_ask_then_collect_bill_then_handoff(self):
        h = [{"role": "user", "content": "ต้องการแก้จำนวนสินค้าในบิล"},
             {"role": "assistant", "content": "แอดมินขอเลขบิลสั่งซื้อของรายการนี้หน่อยนะคะ"}]
        st = derive_operational_state(h, "บิล PO318220260806008")
        extract_operational_fields("บิล PO318220260806008", st)
        self.assertEqual(st.kind, "modify_bill_qty")
        self.assertEqual(st.bill, "PO318220260806008")
        self.assertTrue(st.has_input())

    def test_cn_tracking_all_digits_for_duplicate_bill(self):
        st = OperationalState(kind="duplicate_bill")
        extract_operational_fields("แทรคจีน 9822950447648", st)
        self.assertEqual(st.bill, "9822950447648")

    def test_summary_names_the_request_and_identifier_no_credential(self):
        st = OperationalState(kind="change_shipping_method", bill="PO318220260806008")
        s = operational_handoff_summary(st)
        self.assertIn("เปลี่ยนวิธีจัดส่ง", s)
        self.assertIn("PO318220260806008", s)
        self.assertNotIn("Secret", s)

    def test_closed_after_done_marker(self):
        h = [{"role": "user", "content": "บิลซ้ำค่ะ"},
             {"role": "assistant", "content": "แอดมินเช็คบิลซ้ำและลบบิลให้นะคะ รบกวนขอเลขแทรคจีนหน่อยนะคะ"},
             {"role": "user", "content": "9822950447648"},
             {"role": "assistant", "content": "รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ เดี๋ยวเจ้าหน้าที่จะติดต่อดำเนินการให้นะคะ"},
             {"role": "user", "content": "ขอบคุณค่ะ"}]
        self.assertIsNone(derive_operational_state(h, "ขอบคุณค่ะ"))


class TestRoutingE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _run(self, msg, history=None):
        bsvc = MagicMock()
        bsvc.get_verified_binding.return_value = _BIND
        bsvc.get_verified_binding_for_custcode.return_value = _BIND
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": LINE_UID,
               "developer_mode": True, "customer_context": {"cust_code": CUST}}
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=MagicMock(answer="[RAG]", chunks=[], confidence=0.9,
                                          input_tokens=0, output_tokens=0, general_chat_used=False,
                                          policy=MagicMock(escalate=False, escalation_message=None),
                                          unsupported_company_fact=False)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                "summary": (r.get("handoff_payload") or {}).get("summary"),
                "reply": (r.get("reply") or {}).get("text") or ""}

    def test_turn1_acknowledges_and_asks_never_noinfo_never_handoff(self):
        for msg, ack_frag in [
            ("ต้องการแก้จำนวนสินค้าในบิล", "แอดมินขอเลขบิลสั่งซื้อ"),
            ("สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ", "สามารถเปลี่ยนได้ค่ะ"),
            ("บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง", "แอดมินรบกวนขอเลขบิลขนส่ง"),
            ("ลืมเลือก VAT ไปค่ะ ต้องการVATด้วยค่ะ", "ต้องการ VAT"),
            ("บิลซ้ำค่ะ", "เช็คบิลซ้ำและลบบิลให้"),
            ("ใส่ที่อยู่โกดังจีนถูกไหมคะ", "ตรวจสอบความถูกต้อง"),
            ("ยอดเงินไม่เข้า เติมเงินแล้วรอตรวจสอบ", "ขอสลิปการโอนเงิน"),
        ]:
            r = self._run(msg)
            self.assertEqual(r["routing"], "WORKFLOW", msg)
            self.assertEqual(r["src"], "operational_change_collection", msg)
            self.assertIn(ack_frag, r["reply"], msg)
            self.assertIsNone(r["handoff"], msg)
            self.assertNotIn("ยังไม่มีข้อมูล", r["reply"])
            self.assertNotIn("ดำเนินการเรียบร้อยค่ะ", r["reply"])   # no fake success

    def test_turn2_with_identifier_hands_to_human_cs(self):
        h = [{"role": "user", "content": "ต้องการแก้จำนวนสินค้าในบิล"},
             {"role": "assistant", "content": "แอดมินขอเลขบิลสั่งซื้อของรายการนี้หน่อยนะคะ"}]
        r = self._run("บิล PO318220260806008", history=h)
        self.assertEqual(r["routing"], "HUMAN_HANDOFF")
        self.assertEqual(r["handoff"], "operational_change_request: modify_bill_qty")
        self.assertIn("PO318220260806008", r["summary"])
        self.assertNotIn("ดำเนินการเรียบร้อยแล้ว", r["reply"])

    def test_all_in_one_message_hands_off_directly(self):
        r = self._run("ขอเปลี่ยนบิล PO318220260806008 เป็นจัดส่งทางเรือ")
        self.assertEqual(r["routing"], "HUMAN_HANDOFF")
        self.assertEqual(r["handoff"], "operational_change_request: change_shipping_method")

    def test_address_change_S09_keeps_its_own_business_action(self):
        r = self._run("บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่จัดส่ง")
        self.assertNotEqual(r["src"], "operational_change_collection")

    def test_g21_cancel_policy_stays_rag(self):
        r = self._run("ยกเลิกบิลสั่งซื้อได้ไหม")
        self.assertNotEqual(r["src"], "operational_change_collection")
        self.assertIsNone(r["handoff"])

    def test_calculator_route_reply_not_stolen(self):
        r = self._run("ทางเรือ", history=[
            {"role": "user", "content": "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"},
            {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) ต้องการประเมินทางรถหรือทางเรือคะ"}])
        self.assertNotEqual(r["src"], "operational_change_collection")


if __name__ == "__main__":
    unittest.main()
