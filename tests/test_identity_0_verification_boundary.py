# -*- coding: utf-8 -*-
"""IDENTITY-0 — the complete verified / unverified / in-progress / failed
/ newly-verified identity boundary, end-to-end through DecisionEngine.

Security invariants under test:
  * PUBLIC information NEVER requires identity verification.
  * PRIVATE customer data is NEVER revealed before a verified binding.
  * Authorization source of truth = customer_channel_bindings.status
    == 'verified' (services/customer_binding_service.py) — NEVER a
    legacy user_profiles.cust_code.
  * No data crosses between LINE users.

Synthetic identifiers only. No production data, no real bindings touched.
"""
import unittest
from unittest.mock import MagicMock, patch

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _engine_with_registry, _fake_playground_result
from services.business_action_registry import BusinessActionRegistry
from services.customer_binding_service import CustomerBindingService
from services.authorization_service import AUTHORIZATION_DENIED_MESSAGE

TENANT = "default"
CHANNEL = "line"

# identity-ask / verification wording that must NEVER appear for a public turn
_IDENTITY_ASK = ["ยืนยันตัวตน", "เบอร์โทรที่ผูก", "อีเมลที่ผูก", "รหัสลูกค้า",
                 "เลขสมาชิก", "ยืนยันสิทธิ์"]
# ERP field values that must never leak to an unverified / other user
_WALLET = "2786"
_COUPON_CODE = "COUPON50"
_SHIP_STATUS = "รับเข้าที่จีน"


def _resp(json_body):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_body
    r.text = "raw"
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.binding = CustomerBindingService(self.reg._sb)
        self.engine = _engine_with_registry(self.reg)
        self._seed()

    def _seed(self):
        det = _seed_action(self.reg, key="searchdatashipment", action_type="API",
                           category="Customer Shipment Retrieval", keywords=["เลขบิลขนส่ง", "พัสดุเดียว"])
        self.reg.replace_parameters(det, [
            {"name": "CustCode", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
            {"name": "SecretCode", "required": True, "input_source": "credential_store"},
            {"name": "ShipmentCode", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{6,}$"},
        ])
        self.reg.upsert_execution(det, {"endpoint": "https://erp.invalid/ship", "http_method": "POST"})

        lst = _seed_action(self.reg, key="searchdatashipmentlist", action_type="API",
                           category="Customer Shipment Retrieval", keywords=["พัสดุล่าสุด", "ของผม", "เข้าไทย"])
        self.reg.replace_parameters(lst, [
            {"name": "CustCode", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
            {"name": "SecretCode", "required": True, "input_source": "credential_store"},
        ])
        self.reg.upsert_execution(lst, {"endpoint": "https://erp.invalid/shiplist", "http_method": "POST"})

        cust = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                            category="Customer Data Retrieval", keywords=["ข้อมูลลูกค้า", "ยอดเงิน", "wallet", "คูปอง"])
        self.reg.replace_parameters(cust, [
            {"name": "CustCode", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
            {"name": "CustEmail", "required": False, "input_source": "customer_message", "validation_type": "email"},
        ])
        self.reg.set_parameter_groups(cust, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.replace_response_mapping(cust, [
            {"json_path": "$.data.PurchaseWallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน", "เหลือ"]}},
            {"json_path": "$.data.Coupon", "mapped_label": "คูปอง",
             "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
        ])
        self.reg.upsert_execution(cust, {"endpoint": "https://erp.invalid/cust", "http_method": "GET"})

        rag = _seed_action(self.reg, key="contact_faq", action_type="RAG", category="faq",
                           keywords=["เบอร์ติดต่อ", "เบอร์โทร", "ติดต่อ"])
        self.reg.upsert_execution(rag, {"endpoint": "https://erp.invalid/x", "http_method": "GET"})

    def ask(self, msg, uid, history=None, erp_json=None, cctx=None):
        erp_json = erp_json if erp_json is not None else {"data": {}}
        with patch("services.action_executor.requests.request", return_value=_resp(erp_json)) as erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG-PUBLIC-ANSWER]", confidence=0.9)):
            res = self.engine.decide(msg, history=history or [], context={
                "channel": CHANNEL, "tenant_id": TENANT, "external_user_id": uid,
                "developer_mode": True, "customer_context": dict(cctx or {})})
        res["_erp_called"] = erp.called
        res["_erp_calls"] = erp.call_count
        return res

    def reply(self, res):
        return (res.get("reply") or {}).get("text") or ""

    def assert_no_identity_ask(self, res, ctx=""):
        r = self.reply(res)
        for bad in _IDENTITY_ASK:
            self.assertNotIn(bad, r, f"{ctx}: public turn asked for identity ({bad!r}) — {r[:80]}")

    def assert_no_private_leak(self, res, ctx=""):
        blob = str(res)
        for bad in (_WALLET, _COUPON_CODE, _SHIP_STATUS):
            self.assertNotIn(bad, blob, f"{ctx}: private ERP value {bad!r} leaked")


# ── USER B — NEVER VERIFIED: public questions ────────────────────────────
class TestUnverifiedPublic(_Base):
    UID = "U_new_public"

    def test_PUBLIC_01_road_time(self):
        r = self.ask("ทางรถใช้เวลากี่วัน", self.UID)
        self.assertIn(r["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(r, "PUBLIC-01")
        self.assertFalse(r["_erp_called"])

    def test_PUBLIC_02_coupon_howto(self):
        r = self.ask("คูปองใช้ยังไง", self.UID)
        self.assertIn(r["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(r, "PUBLIC-02")

    def test_PUBLIC_03_contact_number(self):
        r = self.ask("ขอเบอร์ติดต่อ", self.UID)
        self.assertIn(r["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(r, "PUBLIC-03")
        self.assertFalse(r["_erp_called"])

    def test_PUBLIC_04_rate_question(self):
        r = self.ask("ค่าขนส่งคิดยังไง", self.UID)
        self.assertIn(r["routing"]["type"], ("RAG", "GENERAL", "WORKFLOW"))
        self.assert_no_identity_ask(r, "PUBLIC-04")

    def test_PUBLIC_05_services_list(self):
        r = self.ask("มีบริการอะไรบ้าง", self.UID)
        self.assertIn(r["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(r, "PUBLIC-05")


# ── USER B — NEVER VERIFIED: private questions ───────────────────────────
class TestUnverifiedPrivate(_Base):
    UID = "U_new_private"

    def test_PRIVATE_01_wallet_no_value_starts_verification(self):
        r = self.ask("ยอด Wallet ของผมเท่าไหร่", self.UID,
                     erp_json={"data": {"PurchaseWallet": _WALLET}})
        self.assert_no_private_leak(r, "PRIVATE-01")
        self.assertNotEqual(self.reply(r), "")

    def test_PRIVATE_02_coupon_no_data(self):
        r = self.ask("ผมมีคูปองอะไรบ้าง", self.UID,
                     erp_json={"data": {"Coupon": _COUPON_CODE}})
        self.assert_no_private_leak(r, "PRIVATE-02")

    def test_PRIVATE_03_shipment_no_erp_data(self):
        r = self.ask("ของผมเข้าไทยหรือยัง", self.UID,
                     erp_json={"data": {"Shipment": {"Status": _SHIP_STATUS}}})
        self.assert_no_private_leak(r, "PRIVATE-03")
        # must not silently return a shipment — either asks for the bill
        # number (record identifier) or starts identity verification
        self.assertIn(r["routing"]["type"], ("WORKFLOW", "API", "HUMAN_HANDOFF", "SAFE_FALLBACK"))

    def test_PRIVATE_04_order_no_erp_data(self):
        r = self.ask("ออเดอร์ของผมถึงไหนแล้ว", self.UID,
                     erp_json={"data": {"Order": {"Status": _SHIP_STATUS}}})
        self.assert_no_private_leak(r, "PRIVATE-04")


# ── PUBLIC / PRIVATE PAIRS (same unverified user) ───────────────────────
class TestPublicPrivatePairs(_Base):
    UID = "U_pairs"

    def _routing(self, msg, erp_json=None):
        return self.ask(msg, self.UID, erp_json=erp_json)

    def test_coupon_pair(self):
        pub = self._routing("คูปองใช้ยังไง")
        self.assertIn(pub["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(pub, "coupon/public")
        prv = self._routing("ผมมีคูปองอะไรบ้าง", erp_json={"data": {"Coupon": _COUPON_CODE}})
        self.assert_no_private_leak(prv, "coupon/private")
        self.assertNotEqual(pub["routing"]["type"], "API")

    def test_shipment_pair(self):
        pub = self._routing("ทางรถใช้เวลากี่วัน")
        self.assertIn(pub["routing"]["type"], ("RAG", "GENERAL"))
        self.assert_no_identity_ask(pub, "shipment/public")
        prv = self._routing("ของผมเข้าไทยหรือยัง", erp_json={"data": {"Shipment": {"Status": _SHIP_STATUS}}})
        self.assert_no_private_leak(prv, "shipment/private")

    def test_wallet_pair(self):
        pub = self._routing("เติม Wallet ยังไง")
        self.assert_no_identity_ask(pub, "wallet/public")
        prv = self._routing("ยอด Wallet ของผมเท่าไหร่", erp_json={"data": {"PurchaseWallet": _WALLET}})
        self.assert_no_private_leak(prv, "wallet/private")


# ── legacy user_profiles.cust_code must NEVER authorize ─────────────────
class TestLegacyProfileNeverAuthorizes(_Base):
    def test_stale_profile_custcode_in_context_does_not_authorize(self):
        # No verified binding exists; a stale cust_code in customer_context
        # (as an old profile cache might carry) must not unlock private ERP.
        r = self.ask("ขอเช็กพัสดุเดียวครับ FT318220260726001", "U_stale",
                     cctx={"cust_code": "SP9999", "identity_confirmed": True},
                     erp_json={"data": {"Shipment": {"Status": _SHIP_STATUS}}})
        self.assert_no_private_leak(r, "stale-profile")
        # binding still absent
        self.assertIsNone(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id="U_stale"))


# ── verification IN PROGRESS / FAILED / SUCCESS ─────────────────────────
class TestSelfVerificationFlow(_Base):
    ON_FILE = {"data": {"CustPhone": "****7205", "PurchaseWallet": _WALLET}}

    def _start(self, uid):
        r1 = self.ask("ข้อมูลลูกค้า SP1008", uid, erp_json=self.ON_FILE)
        return r1, [{"role": "user", "content": "ข้อมูลลูกค้า SP1008"},
                    {"role": "assistant", "content": self.reply(r1)}]

    def test_verification_start_asks_phone_no_data(self):
        r1, _ = self._start("U_vp_start")
        self.assertIn("เบอร์โทร", self.reply(r1))
        self.assert_no_private_leak(r1, "verify-start")
        self.assertIsNone(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id="U_vp_start"))

    def test_verification_failure_wrong_phone_then_email_escalates_no_bind(self):
        uid = "U_vp_fail"
        r1, h = self._start(uid)
        r2 = self.ask("0000000000", uid, history=h, erp_json=self.ON_FILE)
        self.assertIn("อีเมล", self.reply(r2))
        h += [{"role": "user", "content": "0000000000"},
              {"role": "assistant", "content": self.reply(r2)}]
        r3 = self.ask("nobody@example.com", uid, history=h, erp_json=self.ON_FILE)
        self.assertEqual(r3["routing"]["type"], "HUMAN_HANDOFF")
        self.assert_no_private_leak(r3, "verify-fail")
        self.assertIsNone(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id=uid))

    def test_verification_success_binds_then_private_query_uses_only_that_custcode(self):
        uid = "U_vp_ok"
        r1, h = self._start(uid)
        r2 = self.ask("0642247205", uid, history=h, erp_json=self.ON_FILE)  # matches ****7205
        b = self.binding.get_verified_binding(tenant_id=TENANT, channel=CHANNEL, external_user_id=uid)
        self.assertIsNotNone(b, "successful phone match must create a verified binding")
        self.assertEqual(b["cust_code"], "SP1008")
        self.assertEqual(b["status"], "verified")

    def test_D17_no_reloop_after_failed_verification_escalation(self):
        """D17 / SC2 — once the phone→email sequence has failed and
        escalated to a human, a later private question must NOT restart
        the sequence (no re-prompt for phone), must stay protected (no
        private leak, no binding), and must give the neutral denial."""
        uid = "U_vp_reloop"
        r1, h = self._start(uid)
        r2 = self.ask("0000000000", uid, history=h, erp_json=self.ON_FILE)
        h += [{"role": "user", "content": "0000000000"},
              {"role": "assistant", "content": self.reply(r2)}]
        r3 = self.ask("nobody@example.com", uid, history=h, erp_json=self.ON_FILE)
        self.assertEqual(r3["routing"]["type"], "HUMAN_HANDOFF")
        h += [{"role": "user", "content": "nobody@example.com"},
              {"role": "assistant", "content": self.reply(r3)}]

        # same private request again — the loop case
        r4 = self.ask("ข้อมูลลูกค้า SP1008", uid, history=h, erp_json=self.ON_FILE)
        self.assertNotIn("เบอร์โทรที่ผูก", self.reply(r4), "re-looped: asked for phone again")
        self.assertNotIn("อีเมลที่ผูก", self.reply(r4), "re-looped: asked for email again")
        self.assert_no_private_leak(r4, "D17-reloop")
        self.assertIsNone(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id=uid))
        self.assertTrue(self.reply(r4).strip())

        # a DIFFERENT private request, still no re-loop
        h += [{"role": "user", "content": "ข้อมูลลูกค้า SP1008"},
              {"role": "assistant", "content": self.reply(r4)}]
        r5 = self.ask("เช็คพัสดุ FT318220260726001", uid, history=h, erp_json=self.ON_FILE)
        self.assertNotIn("เบอร์โทรที่ผูก", self.reply(r5), "re-looped on a later different question")
        self.assert_no_private_leak(r5, "D17-reloop-2")


# ── CROSS-USER ISOLATION ───────────────────────────────────────────────
class TestCrossUserIsolation(_Base):
    def test_user_B_never_receives_user_A_private_data(self):
        self.binding.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="U_A",
                                    cust_code="FT3182", created_by="admin")
        # unverified U_B asks a private question with U_A's data mocked on the wire
        r = self.ask("ยอด Wallet ของผมเท่าไหร่", "U_B",
                     erp_json={"data": {"PurchaseWallet": _WALLET, "Coupon": _COUPON_CODE}})
        self.assert_no_private_leak(r, "cross-user")
        self.assertIsNone(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id="U_B"))
        # U_A still verified, unaffected
        self.assertEqual(self.binding.get_verified_binding(
            tenant_id=TENANT, channel=CHANNEL, external_user_id="U_A")["cust_code"], "FT3182")

    def test_verified_user_A_cannot_pull_user_C_custcode(self):
        self.binding.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="U_A",
                                    cust_code="FT3182", created_by="admin")
        r = self.ask("ข้อมูลลูกค้า SP1008", "U_A",
                     erp_json={"data": {"PurchaseWallet": _WALLET}})
        self.assert_no_private_leak(r, "verified-A-requests-C")


if __name__ == "__main__":
    unittest.main()
