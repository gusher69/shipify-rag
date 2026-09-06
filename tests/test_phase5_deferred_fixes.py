# -*- coding: utf-8 -*-
"""PHASE-5 — canonical focused coverage for the Deferred Backlog bugs
fixed in phase 5, promoted from the Phase-1/5 scratchpad harnesses.

Covered:
  D2  — CUS-S18 / CSW18: a successful but EMPTY customer-scoped LIST
        ERP read must say "no shipments for your account", never the
        generic write-ack "ดำเนินการเรียบร้อยค่ะ".
  D5  — CUS-G12: a warehouse-arrival question whose ERP status is in
        NEITHER trusted set must report the real status and say it
        cannot be confirmed — never auto-conclude "ยังไม่ถึงโกดังจีน".
  D10 — CUS-S07 / CSW7: "ขอใบกำกับของ PO12345" stays the invoice how-to
        (RAG), never a searchdataorder READ off the incidental PO.
  D13 — CUS-G21: "ขอยกเลิก PO12345" stays the cancellation policy (RAG),
        never a searchdataorder READ / fake cancel off the incidental PO.
  D15 — CUS-F05: "สั่งแบตเตอรี่จำนวนเยอะได้ไหม" is a PRODUCT_POLICY
        (prohibited-goods) question regardless of the "large quantity"
        framing and the "สั่ง" (order) verb.

Plus a small protected smoke that the fixes did not disturb adjacent
behaviour (a coupon how-to, a Thai-tracking ask, one ERP read, one
operational-workflow request).

Real production registry (reset_real_registry) + mocked binding /
credential / HTTP / playground — same harness the committed
test_customer_action1 / test_identity_0_verification_boundary use.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid")

from services.decision_engine import DecisionEngine
from services.conversation_semantics import _compose as _compose_family
from tests.test_business_action_registry import reset_real_registry
from tests.test_decision_engine import _fake_playground_result

_LU = "Uc5f5717bc090934f9eaa067513388178"
_CUST = "FT3182"


def _t(role, content):
    return {"role": role, "content": content}


class _EngineHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def run_turn(self, msg, history=None, *, cust_code=_CUST, erp=None,
                 erp_status=200, erp_exc=None, rag="[RAG]"):
        binding = ({"cust_code": cust_code, "status": "verified", "channel": "line",
                    "external_user_id": _LU, "tenant_id": "default"} if cust_code else None)
        b = MagicMock()
        b.get_verified_binding.return_value = binding
        b.get_verified_binding_for_custcode.return_value = binding
        cc = {"cust_code": cust_code} if cust_code else {}
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": _LU,
               "developer_mode": True, "customer_context": cc}
        payload = erp if erp is not None else {"data": {}}
        req = (MagicMock(side_effect=erp_exc) if erp_exc
               else MagicMock(return_value=MagicMock(
                   status_code=erp_status, json=lambda: payload, text=json.dumps(payload))))
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=b), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request", req), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer=rag, confidence=0.9)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        d = r.get("developer") or {}
        return {
            "routing": (r.get("routing") or {}).get("type"),
            "src": d.get("selection_source"),
            "action": d.get("selected_business_action"),
            "reply": (r.get("reply") or {}).get("text") or "",
            "handoff": (r.get("handoff_payload") or {}).get("reason"),
            "erp_called": bool(req.call_args),
        }


def _shp(status, code="FT318220260726001", th=""):
    return {"data": {"Shipment": {"Code": code, "Status": status,
                                  "TrackingCH": "768999", "TrackingTH": th, "TotalSum": "0"}}}


class TestD5WarehouseAmbiguousStatus(_EngineHarness):
    _BILL = "FT318220260726001"

    def _followup(self, status):
        # split flow: warehouse question -> engine asks for the bill ->
        # bare bill reply executes the ERP read and answers the question.
        r0 = self.run_turn("สินค้าถึงโกดังหรือยัง", [])
        hist = [_t("user", "สินค้าถึงโกดังหรือยัง"), _t("assistant", r0["reply"])]
        return self.run_turn(self._BILL, hist, erp=_shp(status))

    def test_arrived_status_says_arrived(self):
        r = self._followup("รับเข้าที่จีน")
        self.assertIn("ถึงโกดังจีนแล้ว", r["reply"])
        self.assertIn("รับเข้าที่จีน", r["reply"])

    def test_undefined_status_reports_and_does_not_conclude(self):
        r = self._followup("รับเข้าที่ต้นทางจีน")
        self.assertIn("รับเข้าที่ต้นทางจีน", r["reply"])
        self.assertNotIn("ยังไม่ถึงโกดังจีนค่ะ", r["reply"])
        self.assertIn("ไม่สามารถยืนยันการรับเข้าโกดัง", r["reply"])

    def test_explicit_not_arrived_status_still_negative(self):
        r = self._followup("ยังไม่เข้าโกดังจีน")
        self.assertIn("ยังไม่ถึงโกดังจีนค่ะ", r["reply"])
        self.assertIn("ยังไม่เข้าโกดังจีน", r["reply"])


class TestD2ListEmptyResult(_EngineHarness):
    def test_empty_list_read_is_explicit_not_generic_ack(self):
        r = self.run_turn("วันนี้มีของเข้าไทยไหมคะ", [], erp={"data": []})
        self.assertTrue(r["reply"].strip())
        self.assertNotIn("ดำเนินการเรียบร้อยค่ะ", r["reply"])
        self.assertNotIn("ระบบขัดข้อง", r["reply"])
        self.assertTrue("ยังไม่พบรายการขนส่ง" in r["reply"] or "ไม่พบรายการ" in r["reply"])
        # no fabricated "today" claim beyond the data
        self.assertNotIn("พรุ่งนี้", r["reply"])


class TestD10InvoiceIncidentalPO(_EngineHarness):
    def test_invoice_request_with_po_stays_howto(self):
        r = self.run_turn("ขอใบกำกับของ PO12345", [],
                          rag="ดาวน์โหลดใบกำกับได้จากเมนูรายการสั่งซื้อ > สรุปบัญชี ที่ shipify.co.th")
        self.assertEqual(r["routing"], "RAG")
        self.assertIsNone(r["action"])
        self.assertFalse(r["erp_called"])
        self.assertNotIn("ไม่พบข้อมูลรายการสำหรับเลขที่ PO12345", r["reply"])


class TestD13CancelIncidentalPO(_EngineHarness):
    def test_cancel_request_with_po_stays_policy(self):
        r = self.run_turn("ขอยกเลิก PO12345", [],
                          rag="หากยังไม่ได้ชำระเงินสามารถยกเลิกก่อนได้ค่ะ กรณีชำระแล้วแอดมินจะสอบถามร้านก่อน")
        self.assertEqual(r["routing"], "RAG")
        self.assertIsNone(r["action"])
        self.assertFalse(r["erp_called"])
        self.assertNotIn("ไม่พบข้อมูลรายการสำหรับเลขที่ PO12345", r["reply"])
        for fake in ("ยกเลิกเรียบร้อย", "ยกเลิกสำเร็จ", "ยกเลิกบิลเรียบร้อยแล้ว"):
            self.assertNotIn(fake, r["reply"])


class TestD15ProhibitedGoodsLargeQuantity(unittest.TestCase):
    def test_order_verb_large_quantity_is_product_policy(self):
        for m in ("สั่งแบตเตอรี่จำนวนเยอะได้ไหม", "สั่งแบตเตอรี่ได้ไหม",
                  "ซื้อน้ำหอมได้ไหม", "สั่งซื้อครีมอาบน้ำได้ไหม"):
            fam, conf, _ = _compose_family(m)
            self.assertEqual(fam, "PRODUCT_POLICY", m)

    def test_bare_order_verb_without_goods_is_not_product_policy(self):
        for m in ("สั่งได้ไหม", "ซื้อได้ไหม", "สั่งเยอะได้ไหม"):
            fam, _, _ = _compose_family(m)
            self.assertNotEqual(fam, "PRODUCT_POLICY", m)

    def test_shipping_verb_cases_unchanged(self):
        for m in ("นำเข้าแบตเตอรี่ได้ไหม", "ครีมอาบน้ำนำเข้าได้ไหม",
                  "กล่องพลาสติกนำเข้าได้ไหมครับ"):
            fam, _, _ = _compose_family(m)
            self.assertEqual(fam, "PRODUCT_POLICY", m)


class TestPhase5AdjacentSmoke(_EngineHarness):
    def test_coupon_howto_still_rag(self):
        r = self.run_turn("คูปองใช้ยังไง", [], rag="ใส่โค้ดคูปองตอนชำระเงินค่ะ")
        self.assertEqual(r["routing"], "RAG")

    def test_one_erp_read_still_works(self):
        r = self.run_turn("เช็คสถานะบิล FT318220260726001", [],
                          erp=_shp("รับเข้าที่จีน"))
        self.assertTrue(r["erp_called"])
        self.assertIn("รับเข้าที่จีน", r["reply"])

    def test_one_operational_workflow_still_hands_off(self):
        r = self.run_turn("บิลซ้ำค่ะ", [])
        self.assertTrue(
            (r["handoff"] or "").startswith("operational_change_request")
            or "แทรคจีน" in r["reply"])

    def test_thai_tracking_ask_not_broken(self):
        hist = [_t("user", "เช็คสถานะบิล FT318220260726001"),
                _t("assistant", "สถานะบิลขนส่ง: รับเข้าที่จีนค่ะ")]
        r = self.run_turn("ขอเลขแทร็กไทยค่ะ", hist, erp=_shp("ส่งออกจากจีน", th="TH123456"))
        self.assertNotIn("ดำเนินการเรียบร้อยค่ะ", r["reply"])
        self.assertTrue(r["reply"].strip())


if __name__ == "__main__":
    unittest.main()
