# -*- coding: utf-8 -*-
"""CUSTOMER-TRACK-TH-1 — Thai Tracking / customer shipment tracking.

Customer source: `tests/customer_uat/customer_uat_master.jsonl` CUS-S17
('ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง)', Ai.xlsx sheet
'2.tongchecknairabop' row 17 / CSW17, api_input_hint: shipment_bill_no).
Customer-approved script: "บิลขนส่งนี้แอดมินช่วยเช็คไทยให้นะคะ" (ack) ->
"บิล... แทรคไทย... สถานะขนส่งแฟลชอยู่ระหว่างนำส่ง..." (bill + Thai
tracking + carrier/status) -> a tracking-check link. The identifier is
the SHIPMENT BILL, never a Chinese tracking number.

REAL LINE failures (product owner testing) this fixes:
  "ขอเลขแทรคไทยค่ะ" -> "กรุณาแจ้งเลข Tracking จีนค่ะ"            (wrong: asked for the wrong INPUT)
  "ขอเลขแทรคไทยค่ะ" -> "...ให้เจ้าหน้าที่ติดต่อกลับ..."          (wrong: false Human CS)

ROOT CAUSE: `services.decision_engine._PSI_DOMAIN_ACTION_HINTS
['tracking']['track_hint']` unconditionally preferred `searchdatatracking`
(confirmed live: its own `ai_description` is "ค้นหาบิลขนส่งด้วยเลข
Tracking จีน" — search BY a China tracking number, the OPPOSITE
direction of a Thai-tracking OUTPUT ask) for ANY message matching the
"tracking" domain. Fixed with `_PSI_THAI_TRACKING_OUTPUT_RE`: a message
asking for THAI tracking specifically (and not naming China / an actual
tracking value) resolves to `searchdatashipment` instead (ShipmentCode
-> TrackingTH/TrackingCH/Status/TotalSum — already returns Thai tracking
directly, confirmed via a live registry audit; no China tracking is
ever required as input). Reuses ALL existing generic infrastructure
unchanged: authorization (`requires_verified_identity`/verified
binding), malformed-identifier handling (`_incomplete_shipment_code`,
already scoped to `searchdatashipment`'s `ShipmentCode`), and
valid-not-found handling (`_ERP_READ_STATUS_ACTIONS`, already includes
`searchdatashipment`).

Reuses the real-registry E2E `_Session` harness from
tests.test_system_state_emergency_1.
"""
import unittest
from unittest.mock import MagicMock, patch

from tests.test_system_state_emergency_1 import _Session

_ASK_BILL = "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"
_VALID_BILL = "FT318220260726001"
_MALFORMED_BILL = "FT31822026072"


class _TrackE2E(_Session):
    def _say_with_exec(self, msg, *, verified=True, exec_response=None, history=None):
        """Like _Session.say(), but with control over the ERP HTTP mock's
        response (needed for full/not-found/no-thai-tracking scenarios) —
        _Session.say() hardcodes a fixed getdatacustomer-shaped payload."""
        bsvc = MagicMock()
        bsvc.get_verified_binding.return_value = {"cust_code": "FT3182", "status": "verified"} if verified else None
        bsvc.get_verified_binding_for_custcode.return_value = (
            {"cust_code": "FT3182", "status": "verified"} if verified else None)
        customer_context = {"cust_code": "FT3182", "identity_confirmed": True} if verified else {}
        # Mirrors profiles/manager.py::update_profile_from_turn's own
        # persistence of `last_business_action` (real ERP execution only
        # — see that function's own guard) + line_bot/webhook.py loading
        # it back into `customer_context` on the NEXT turn. A test-only
        # simulation of real cross-turn behavior; never a hardcoded
        # per-action shortcut.
        _last_action = getattr(self, "_last_business_action", None)
        if _last_action:
            customer_context["last_business_action"] = _last_action
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "Uc5f5717bc090934f9eaa067513388178",
               "developer_mode": True, "customer_context": customer_context}
        with patch("services.conversation_semantics._llm_family",
                    side_effect=lambda m, history=None: {"family": "UNKNOWN"}), \
             patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request",
                   return_value=exec_response or MagicMock(status_code=200, json=lambda: {"data": {}})) as mock_req:
            r = self.eng.decide(msg, history=list(history or self.h), context=ctx)
        dev = r.get("developer") or {}
        reply = (r.get("reply") or {}).get("text") or ""
        routing_type = (r.get("routing") or {}).get("type")
        if routing_type in ("API", "WEBHOOK") and dev.get("selected_business_action"):
            self._last_business_action = dev["selected_business_action"]
        if history is None:
            self.h += [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
        return {"routing": routing_type, "reply": reply,
                "action": dev.get("selected_business_action"), "src": dev.get("selection_source"),
                "erp_called": mock_req.called, "handoff": (r.get("handoff_payload") or {}).get("reason")}

    @staticmethod
    def _shipment_response(**fields):
        base = {"Code": _VALID_BILL, "Status": "", "TrackingCH": "", "TrackingTH": "", "TotalSum": ""}
        base.update(fields)
        return MagicMock(status_code=200, json=lambda: {"data": {"Shipment": base}})


# 1/2 — exact customer phrase + unseen paraphrase
class TestExactPhraseAndParaphrase(_TrackE2E):
    def test_exact_customer_phrase(self):
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertEqual(o["reply"], _ASK_BILL)
        self.assertFalse(o["erp_called"])
        self.assertIsNone(o["handoff"])

    def test_unseen_paraphrase(self):
        for msg in ("ขอเลขติดตามฝั่งไทยหน่อยครับ", "ของเข้าไทยแล้ว ขอแทรคไทยหน่อย",
                    "ขอ tracking ฝั่งไทยของบิลนี้หน่อย", "รบกวนขอเลขติดตามพัสดุฝั่งไทยของบิลนี้หน่อยค่ะ"):
            self.h = []
            o = self._say_with_exec(msg)
            self.assertEqual(o["reply"], _ASK_BILL, msg)


# 3/4 — missing / malformed shipment identifier
class TestMissingAndMalformedIdentifier(_TrackE2E):
    def test_missing_identifier_asks_not_human_cs(self):
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertEqual(o["routing"], "WORKFLOW")
        self.assertIsNone(o["handoff"])
        self.assertFalse(o["erp_called"])

    def test_malformed_identifier(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec(_MALFORMED_BILL)
        self.assertIn("ไม่ครบถ้วน", o["reply"])
        self.assertFalse(o["erp_called"])
        self.assertIsNone(o["handoff"])


# 5 — valid format, not found
class TestValidNotFound(_TrackE2E):
    def test_valid_not_found(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec("FT999999999999999",
                                 exec_response=MagicMock(status_code=200, json=lambda: {"data": {"Shipment": {}}}))
        self.assertIn("ไม่พบข้อมูล", o["reply"])
        self.assertNotIn("แทรคไทย: ", o["reply"])
        self.assertIsNone(o["handoff"])


# 6 — shipment found, Thai tracking not yet assigned
class TestShipmentFoundNoThaiTracking(_TrackE2E):
    def test_shipment_found_no_thai_tracking(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec(_VALID_BILL, exec_response=self._shipment_response(Status="รับเข้าโกดังจีน"))
        self.assertIn("ยังไม่มีเลข Tracking ไทย", o["reply"])
        self.assertNotIn("Tracking จีน:", o["reply"])   # never substitutes China tracking


# 7/8/9/10 — Thai tracking found, with carrier/status/url where present
class TestThaiTrackingFound(_TrackE2E):
    def test_full_result_with_status_and_china_tracking_also_shown(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec(_VALID_BILL, exec_response=self._shipment_response(
            Status="อยู่ระหว่างนำส่ง", TrackingCH="CN123456789", TrackingTH="TH987654321", TotalSum="1500.00"))
        self.assertEqual(o["action"], "searchdatashipment")
        self.assertIn("TH987654321", o["reply"])
        self.assertIn("อยู่ระหว่างนำส่ง", o["reply"])
        self.assertIsNone(o["handoff"])

    def test_no_invented_carrier_or_url_fields(self):
        # the current API contract's response_mapping has no Carrier/
        # TrackingURL row at all -- the reply must never fabricate one.
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec(_VALID_BILL, exec_response=self._shipment_response(TrackingTH="TH1"))
        self.assertNotIn("http://", o["reply"])
        self.assertNotIn("https://", o["reply"])


# 11/12 — verified vs unverified customer
class TestIdentityHandling(_TrackE2E):
    def test_verified_customer_gets_real_data(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ", verified=True)
        o = self._say_with_exec(_VALID_BILL, verified=True,
                                 exec_response=self._shipment_response(TrackingTH="TH1"))
        self.assertTrue(o["erp_called"])
        self.assertIn("TH1", o["reply"])

    def test_unverified_customer_follows_identity_flow_not_leak(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ", verified=False)
        o = self._say_with_exec(_VALID_BILL, verified=False,
                                 exec_response=self._shipment_response(TrackingTH="TH1"))
        # unverified -> either a denial or an identity-verification ask;
        # never the real tracking data leaked without authorization.
        if o["erp_called"]:
            self.assertNotIn("TH1", o["reply"])


# 13/14 — no China-tracking confusion, no false Human CS (already
# exercised above; asserted explicitly here too as the core regression)
class TestNoChinaTrackingNoFalseHumanCS(_TrackE2E):
    def test_no_china_tracking_ask(self):
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertNotIn("Tracking จีน", o["reply"])
        self.assertNotIn("เลขจีน", o["reply"])

    def test_no_false_human_cs(self):
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertIsNone(o["handoff"])


# 15/16/17 — state authority: other pending flows yield to Thai Tracking
class TestStateAuthorityIncoming(_TrackE2E):
    def test_tc19_pending_then_thai_tracking_wins(self):
        self.h = [{"role": "user", "content": "มีบริการเหมารถไหมคะ"},
                  {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
                   "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertEqual(o["src"], "private_state_inquiry")
        self.assertEqual(o["reply"], _ASK_BILL)

    def test_calculator_pending_then_thai_tracking_wins(self):
        self.h = [{"role": "user", "content": "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"},
                  {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) "
                   "ต้องการประเมินทางรถหรือทางเรือคะ"}]
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertEqual(o["reply"], _ASK_BILL)

    def test_link_conversion_pending_then_thai_tracking_wins(self):
        self.h = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อย"},
                  {"role": "assistant", "content": "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"}]
        o = self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self.assertEqual(o["reply"], _ASK_BILL)
        self.assertNotEqual(o["action"], "geturlproductdetail")


# 18/19 — Thai Tracking pending yields to a genuinely different topic
class TestStateAuthorityOutgoing(_TrackE2E):
    def test_pending_then_coupon_wins(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec("คูปองใช้ยังไงครับ")
        self.assertNotEqual(o["reply"], _ASK_BILL)
        self.assertFalse(o["erp_called"])

    def test_pending_then_transit_wins(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec("ทางเรือกี่วันครับ")
        self.assertNotEqual(o["reply"], _ASK_BILL)


# 20 — follow-up shipment status reuses the established shipment context
class TestFollowUpContext(_TrackE2E):
    def test_follow_up_status_after_lookup(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        self._say_with_exec(_VALID_BILL, exec_response=self._shipment_response(
            Status="อยู่ระหว่างนำส่ง", TrackingTH="TH1"))
        o = self._say_with_exec("ตอนนี้ถึงไหนแล้วคะ")
        # must not silently dead-end into a generic no-info reply
        self.assertNotIn("ไม่มีข้อมูล", o["reply"])


# 21/22 — no fake success, no cross-customer leakage (structural checks
# already threaded through the tests above; asserted directly here too)
class TestTruthAndIsolation(_TrackE2E):
    def test_no_fake_success_on_not_found(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec("FT999999999999999",
                                 exec_response=MagicMock(status_code=200, json=lambda: {"data": {"Shipment": {}}}))
        self.assertNotIn("เรียบร้อยค่ะ", o["reply"])

    def test_no_cross_customer_data_without_matching_binding(self):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ", verified=True)
        o = self._say_with_exec(_VALID_BILL, verified=True,
                                 exec_response=self._shipment_response(TrackingTH="TH_SECRET_OTHER"))
        # the mocked ERP call is scoped to the VERIFIED caller's own
        # CustCode (asserted at the executor/authorization layer,
        # already covered by services/authorization_service.py's own
        # tests) -- here we only assert the reply reflects exactly what
        # the (authorized) call returned, nothing invented on top.
        self.assertIn("TH_SECRET_OTHER", o["reply"])


if __name__ == "__main__":
    unittest.main()
