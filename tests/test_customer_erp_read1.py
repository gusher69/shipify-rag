# -*- coding: utf-8 -*-
"""CUSTOMER-ERP-READ-1 — the private ERP READ error taxonomy.

  MISSING_INPUT       -> ask for the one missing identifier
  MALFORMED_IDENTIFIER-> "the number looks incomplete, send the full one"
  VALID_NOT_FOUND     -> "no matching record, re-check the number"
  UNAUTHORIZED        -> deterministic denial / self-verification
  ERP_FAILURE         -> a customer-safe temporary system-error reply
  SUCCESS             -> the real ERP fields

None of MALFORMED / NOT_FOUND / MISSING may become "ระบบขัดข้อง" or a
Human CS handoff. Authorization stays deterministic (verified binding
only — never a stale user_profiles.cust_code such as SP1008).
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import (
    DecisionEngine, _incomplete_shipment_code, _incomplete_identifier_prompt,
    _ERP_READ_STATUS_ACTIONS,
)
from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _engine_with_registry, _seed_action
from services.business_action_registry import BusinessActionRegistry

LINE_UID = "Uc5f5717bc090934f9eaa067513388178"
CUST = "FT3182"
SHIP_OK = "FT318220260726001"          # 17 chars — complete
SHIP_SHORT = "FT31822026072"           # 13 chars — the customer's incomplete example
SHIP_ABSENT = "FT318220260726999"      # complete shape, no ERP record

_BIND = {"cust_code": CUST, "status": "verified", "channel": "line",
         "external_user_id": LINE_UID, "tenant_id": "default"}


def _seed(reg):
    aid = _seed_action(reg, key="searchdatashipment", action_type="API",
                       category="Customer Shipment Retrieval",
                       keywords=["เลขบิลขนส่ง", "สถานะพัสดุ", "ของถึงไหน"])
    reg.replace_parameters(aid, [
        {"name": "SecretCode", "display_name": "SecretCode", "required": True,
         "input_source": "credential_store", "credential_ref": "sc"},
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
        {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
    ])
    reg.upsert_execution(aid, {"endpoint": "https://erp.invalid/shipment", "http_method": "POST"})
    reg.replace_response_mapping(aid, [
        {"json_path": "$.data.Shipment.Code", "mapped_label": "เลขที่บิลขนส่ง", "field_metadata": {}, "sort_order": 0},
        {"json_path": "$.data.Shipment.Status", "mapped_label": "สถานะบิลขนส่ง", "field_metadata": {}, "sort_order": 1},
    ])
    return aid


class TestIncompleteIdentifierHelper(unittest.TestCase):
    _P = {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง"}

    def test_flags_short_bill_code_only_for_the_read_flow_and_shipmentcode_slot(self):
        self.assertEqual(
            _incomplete_shipment_code(f"เช็ค {SHIP_SHORT}", self._P, "searchdatashipment"), SHIP_SHORT)
        # complete code -> not flagged
        self.assertIsNone(
            _incomplete_shipment_code(f"เช็ค {SHIP_OK}", self._P, "searchdatashipment"))
        # different action / different slot / no param -> never flagged
        self.assertIsNone(
            _incomplete_shipment_code(f"เช็ค {SHIP_SHORT}", self._P, "requestshippingaddresschange"))
        self.assertIsNone(_incomplete_shipment_code(
            f"เช็ค {SHIP_SHORT}", {"name": "CustCode"}, "searchdatashipment"))
        self.assertIsNone(_incomplete_shipment_code(f"เช็ค {SHIP_SHORT}", None, "searchdatashipment"))

    def test_does_not_flag_a_custcode_shaped_token(self):
        # "SP1008" (prefix + 4 digits) must NOT be read as a short bill code
        self.assertIsNone(
            _incomplete_shipment_code("SP1008", self._P, "searchdatashipment"))

    def test_prompt_wording_is_a_recheck_not_an_error_or_handoff(self):
        t = _incomplete_identifier_prompt({"display_name": "เลขที่บิลขนส่ง"}, SHIP_SHORT)
        self.assertIn(SHIP_SHORT, t)
        self.assertIn("ไม่ครบถ้วน", t)
        self.assertNotIn("ระบบขัดข้อง", t)
        self.assertNotIn("เจ้าหน้าที่", t)


class TestErpReadTaxonomyE2E(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.aid = _seed(self.reg)
        self.eng = _engine_with_registry(self.reg)

    def _run(self, msg, *, verified=True, erp=None, erp_status=200, erp_raises=False, history=None):
        binding = _BIND if verified else None
        bsvc = MagicMock()
        bsvc.get_verified_binding.return_value = binding
        bsvc.get_verified_binding_for_custcode.return_value = binding
        erp = erp if erp is not None else {"data": {"Shipment": {"Code": SHIP_OK, "Status": "รับเข้าที่จีน"}}}

        def _req(*a, **k):
            if erp_raises:
                raise Exception("connection reset")
            m = MagicMock(status_code=erp_status, ok=erp_status < 400)
            m.json = lambda: erp
            m.text = "{}"
            m.headers = {"content-type": "application/json"}
            return m

        cc = {"cust_code": CUST} if verified else {}
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": LINE_UID,
               "developer_mode": True, "customer_context": cc}
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request", side_effect=_req), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=MagicMock(answer="[RAG]", chunks=[], confidence=0.9,
                                          input_tokens=0, output_tokens=0, general_chat_used=False,
                                          policy=MagicMock(escalate=False, escalation_message=None),
                                          unsupported_company_fact=False)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                "erp_read": dev.get("erp_read_result"),
                "reply": (r.get("reply") or {}).get("text") or ""}

    # C — MALFORMED
    def test_C_incomplete_identifier_asks_for_full_number_no_erp_no_handoff(self):
        r = self._run(f"ของผมถึงไหนแล้ว บิล {SHIP_SHORT}")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ไม่ครบถ้วน", r["reply"])
        self.assertNotEqual(r["routing"], "HUMAN_HANDOFF")
        self.assertIsNone(r["handoff"])
        self.assertNotIn("ระบบขัดข้อง", r["reply"])

    # D — VALID_NOT_FOUND
    def test_D_valid_format_not_found_says_recheck_not_system_error(self):
        for empty in ({"data": {"Shipment": None}}, {"data": {}}, {"data": None}):
            r = self._run(f"ของผมถึงไหนแล้ว บิล {SHIP_ABSENT}", erp=empty)
            self.assertEqual(r["erp_read"], "valid_not_found", empty)
            self.assertIn("ไม่พบข้อมูลรายการ", r["reply"])
            self.assertNotIn("ระบบขัดข้อง", r["reply"])
            self.assertNotEqual(r["routing"], "HUMAN_HANDOFF")

    # E — ERP_FAILURE
    def test_E_erp_failure_is_a_temporary_system_error_not_not_found(self):
        for kw in (dict(erp={"error": "upstream"}, erp_status=500), dict(erp_raises=True)):
            r = self._run(f"ของผมถึงไหนแล้ว บิล {SHIP_OK}", **kw)
            self.assertNotEqual(r["erp_read"], "valid_not_found")
            self.assertNotIn("ไม่พบข้อมูลรายการ", r["reply"])
            self.assertNotEqual(r["routing"], "HUMAN_HANDOFF")

    # A — SUCCESS
    def test_A_verified_read_returns_real_erp_fields(self):
        r = self._run(f"ของผมถึงไหนแล้ว บิล {SHIP_OK}")
        self.assertIn("รับเข้าที่จีน", r["reply"])

    # B — MISSING_INPUT
    def test_B_missing_identifier_asks_not_no_info_not_handoff(self):
        r = self._run("ของผมถึงไหนแล้ว")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertNotIn("ไม่มีข้อมูล", r["reply"])
        self.assertIsNone(r["handoff"])

    # NO AUTO-LATEST
    def test_no_auto_latest_shipment(self):
        r = self._run("ของผมเข้าไทยหรือยัง")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertNotIn("รับเข้าที่จีน", r["reply"])  # did not silently return a record

    # CONTEXT SWITCH
    def test_context_switch_to_public_transit_time(self):
        r = self._run("ทางเรือกี่วัน", history=[
            {"role": "user", "content": "ของผมถึงไหนแล้ว"},
            {"role": "assistant", "content": "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"}])
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])
        self.assertNotEqual(r["routing"], "HUMAN_HANDOFF")


if __name__ == "__main__":
    unittest.main()
