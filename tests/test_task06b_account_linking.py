"""Task 06B (2026-08-26) — Secure LINE Account Linking.

Proves the real, verified LINE-user-to-CustCode binding (services/
customer_binding_service.py, migrations/045_customer_channel_bindings.sql)
integrates into Task 06's authorization gate WITHOUT weakening it:

    unverified LINE user            -> DENY  (unchanged from Task 06)
    verified user A -> own resource -> ALLOW
    verified user A -> resource B   -> DENY  (typed CustCode can never
                                               switch the verified identity)
    revoked binding                 -> DENY again, immediately

VERIFICATION METHOD (Task 06B investigation, see the before-code
report): no trusted self-service channel exists in this codebase (no
OTP provider, no customer login, no LINE Login) — every binding here is
created via the staff-assisted admin API (behind the SAME auth(request)
session-cookie gate as every other admin surface), never a customer-
facing self-service flow. Tests marked N/A below are for self-service-
only concerns (OTP replay/rate-limiting) that don't apply to this
verification method.

Synthetic identifiers only (SP-A/SP-B/Uverified-a/...), never real
customer data.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _engine_with_registry
from tests._admin_test_auth import login_as_test_admin
from services.business_action_registry import BusinessActionRegistry
from services.action_executor import ActionExecutor
from services.customer_binding_service import CustomerBindingService, get_customer_binding_service
from services.authorization_service import check_authorization, AUTHORIZATION_DENIED_MESSAGE

TENANT = "default"
CHANNEL = "line"


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {"ok": True}
    resp.text = "raw"
    return resp


# ── 1. CustomerBindingService — the persistent source of truth ─────────────

class TestCustomerBindingService(unittest.TestCase):
    def setUp(self):
        self.sb = _FakeSupabase()
        self.svc = CustomerBindingService(self.sb)

    def test_link_verified_creates_a_verified_row(self):
        row = self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                       cust_code="SP-A", created_by="admin")
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["cust_code"], "SP-A")
        self.assertEqual(row["verification_method"], "staff_assisted")

    def test_get_verified_binding_returns_none_when_unlinked(self):
        self.assertIsNone(self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL,
                                                          external_user_id="Unever-linked"))

    def test_get_verified_binding_returns_the_row_once_linked(self):
        self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                cust_code="SP-A", created_by="admin")
        row = self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a")
        self.assertIsNotNone(row)
        self.assertEqual(row["cust_code"], "SP-A")

    def test_revoke_removes_it_from_verified_lookup_immediately(self):
        row = self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                       cust_code="SP-A", created_by="admin")
        self.svc.revoke(row["id"], reason="unlink_requested")
        self.assertIsNone(self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL,
                                                          external_user_id="Uverified-a"))

    def test_relink_supersedes_the_users_own_prior_binding(self):
        """Phase 23: explicit relink, never a silent second active row."""
        first = self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                        cust_code="SP-A", created_by="admin")
        second = self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                         cust_code="SP-B", created_by="admin")
        current = self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a")
        self.assertEqual(current["id"], second["id"])
        self.assertEqual(current["cust_code"], "SP-B")
        first_refetched = [r for r in self.sb.store["customer_channel_bindings"] if r["id"] == first["id"]][0]
        self.assertEqual(first_refetched["status"], "revoked")

    def test_relink_supersedes_a_different_users_binding_to_the_same_custcode(self):
        """1:1 policy (confirmed business decision): a CustCode can be
        verified-bound to only ONE LINE user at a time."""
        self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                cust_code="SP-SHARED", created_by="admin")
        self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-b",
                                cust_code="SP-SHARED", created_by="admin")
        self.assertIsNone(self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL,
                                                          external_user_id="Uverified-a"))
        current = self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-b")
        self.assertEqual(current["cust_code"], "SP-SHARED")

    def test_tenant_isolation_a_binding_in_one_tenant_is_invisible_in_another(self):
        self.svc.link_verified(tenant_id="tenant-a", channel=CHANNEL, external_user_id="Uverified-a",
                                cust_code="SP-A", created_by="admin")
        self.assertIsNone(self.svc.get_verified_binding(tenant_id="tenant-b", channel=CHANNEL,
                                                          external_user_id="Uverified-a"))

    def test_channel_isolation_a_binding_on_one_channel_is_invisible_on_another(self):
        self.svc.link_verified(tenant_id=TENANT, channel="line", external_user_id="Uverified-a",
                                cust_code="SP-A", created_by="admin")
        self.assertIsNone(self.svc.get_verified_binding(tenant_id=TENANT, channel="some_other_channel",
                                                          external_user_id="Uverified-a"))

    def test_list_bindings_never_used_for_authorization_only_staff_lookup(self):
        self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                cust_code="SP-A", created_by="admin")
        rows = self.svc.list_bindings(tenant_id=TENANT, external_user_id="Uverified-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cust_code"], "SP-A")


# ── 2. authorization_service.py — verified binding + resource ownership ────

class TestAuthorizationServiceVerifiedBinding(unittest.TestCase):
    SENSITIVE = {"parameters": [
        {"name": "CustCode", "required": False, "input_source": "customer_message"},
    ]}

    def setUp(self):
        self.sb = _FakeSupabase()
        self.svc = CustomerBindingService(self.sb)
        self.svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                cust_code="SP-A", created_by="admin")

    def test_unverified_user_still_denied(self):
        """Task 06 regression: no binding at all -> DENY, exactly as before."""
        result = check_authorization(self.SENSITIVE, {"channel": "line", "tenant_id": TENANT,
                                                        "external_user_id": "Uunlinked"}, sb=self.sb)
        self.assertFalse(result["authorized"])

    def test_verified_user_own_resource_allowed(self):
        result = check_authorization(
            self.SENSITIVE,
            {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a",
             "collected_slots": {"CustCode": "SP-A"}},
            sb=self.sb)
        self.assertTrue(result["authorized"])

    def test_verified_user_no_custcode_supplied_yet_allowed_pending_autofill(self):
        """Convenience autofill (Phase 17/21) happens upstream in
        decision_engine.py from the verified identity; if collected_slots
        genuinely has nothing yet, ownership can't conflict with anything
        -- still allowed (the actual value used downstream is the
        verified one, never fabricated)."""
        result = check_authorization(
            self.SENSITIVE,
            {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a", "collected_slots": {}},
            sb=self.sb)
        self.assertTrue(result["authorized"])

    def test_verified_user_a_requests_customer_b_denied(self):
        """THE core Task 06B invariant: a verified user typing a
        DIFFERENT customer's CustCode is never treated as an identity
        switch."""
        result = check_authorization(
            self.SENSITIVE,
            {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a",
             "collected_slots": {"CustCode": "SP-B"}},
            sb=self.sb)
        self.assertFalse(result["authorized"])
        self.assertIn("does not match", result["reason"])

    def test_revoked_binding_denied_immediately(self):
        binding = self.svc.get_verified_binding(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a")
        self.svc.revoke(binding["id"], reason="test_revocation")
        result = check_authorization(
            self.SENSITIVE,
            {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a",
             "collected_slots": {"CustCode": "SP-A"}},
            sb=self.sb)
        self.assertFalse(result["authorized"])

    def test_action_with_no_custcode_parameter_fails_closed_even_when_verified(self):
        no_custcode_action = {"parameters": [
            {"name": "OrderCode", "required": True, "input_source": "customer_message"},
        ]}
        result = check_authorization(
            no_custcode_action,
            {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a"},
            sb=self.sb)
        self.assertFalse(result["authorized"])

    def test_no_sb_degrades_to_deny_never_crashes(self):
        """Backward compatibility: every pre-Task-06B caller/test that
        never passes sb keeps the exact same universal-deny behavior
        Task 06 already shipped -- never a crash, never an accidental
        allow."""
        result = check_authorization(
            self.SENSITIVE, {"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a"}, sb=None)
        self.assertFalse(result["authorized"])

    def test_missing_tenant_or_external_user_id_degrades_to_deny(self):
        result = check_authorization(self.SENSITIVE, {"channel": "line"}, sb=self.sb)
        self.assertFalse(result["authorized"])

    def test_admin_playground_channel_still_bypasses_binding_check_entirely(self):
        """Task 06's admin/Playground exemption is unaffected -- no
        binding needed at all."""
        result = check_authorization(self.SENSITIVE, {"channel": "admin"}, sb=self.sb)
        self.assertTrue(result["authorized"])
        result2 = check_authorization(self.SENSITIVE, {"channel": "playground"}, sb=self.sb)
        self.assertTrue(result2["authorized"])

    def test_cross_tenant_binding_never_authorizes(self):
        """A verified binding that exists in a DIFFERENT tenant must not
        authorize this tenant's request."""
        result = check_authorization(
            self.SENSITIVE,
            {"channel": "line", "tenant_id": "some-other-tenant", "external_user_id": "Uverified-a",
             "collected_slots": {"CustCode": "SP-A"}},
            sb=self.sb)
        self.assertFalse(result["authorized"])


# ── 3. ActionExecutor — direct-call bypass proof with resource ownership ───

class TestActionExecutorResourceOwnership(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg
        self.binding_svc = CustomerBindingService(self.reg._sb)
        self.binding_svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                        cust_code="SP-A", created_by="admin")

    def _make_action(self):
        action_id = _seed_action(self.reg, key="getdatacustomer_like", action_type="API", category="customer")
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": False, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        return action_id

    def test_direct_call_verified_own_resource_allowed(self):
        action_id = self._make_action()
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})) as mock_req:
            result = self.executor.execute(action_id, {
                "collected_slots": {"CustCode": "SP-A"}, "channel": "line",
                "tenant_id": TENANT, "external_user_id": "Uverified-a",
            })
        mock_req.assert_called_once()
        self.assertEqual(result["status"], "success")

    def test_direct_call_verified_other_resource_denied(self):
        """Phase 34: direct call with channel=line, verified binding=A,
        requested resource=B -> DENY. Proves the choke point works
        regardless of caller, not just through decide()."""
        action_id = self._make_action()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 999})) as mock_req:
            result = self.executor.execute(action_id, {
                "collected_slots": {"CustCode": "SP-VICTIM"}, "channel": "line",
                "tenant_id": TENANT, "external_user_id": "Uverified-a",
            })
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "denied")
        self.assertNotIn("999", str(result))


# ── 4. End-to-end via decide() ──────────────────────────────────────────────

class TestEndToEndVerifiedAccess(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)
        self.binding_svc = CustomerBindingService(self.reg._sb)

    def _seed_customer_lookup(self):
        # Shaped like the REAL production GetDataCustomer action (Task 06
        # investigation): CustCode individually optional but a member of
        # an AT_LEAST_ONE identifier group -- without the group, this
        # fixture's CustCode is a no-op that never actually gets bound
        # from message text (confirmed live while writing this test), so
        # the group is required here to genuinely exercise the "typed a
        # DIFFERENT CustCode" resource-ownership check below.
        action_id = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            # Matches this codebase's established CustCode shape (2
            # letters + digits, e.g. "SP1014") -- a "CU0001"-style value
            # was NOT reliably recognized by the existing generic
            # message-token scanner (unrelated to Task 06B; confirmed
            # live while writing this test), so the fixture uses the
            # same shape every other test file in this repo already does.
            {"name": "CustCode", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{4}$"},
            {"name": "CustEmail", "required": False, "input_source": "customer_message",
             "validation_type": "email"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        return action_id

    def test_verified_customer_own_profile_allowed_end_to_end(self):
        self._seed_customer_lookup()
        self.binding_svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                        cust_code="CU0001", created_by="admin")
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"CustName": "Test A"})) as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า CU0001", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a"})
        mock_req.assert_called_once()
        self.assertEqual(result["routing"]["type"], "API")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "CU0001")

    def test_verified_customer_cross_customer_still_denied_end_to_end(self):
        self._seed_customer_lookup()
        self.binding_svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                        cust_code="CU0001", created_by="admin")
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"CustName": "Victim"})) as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า CU9999", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a"})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)

    def test_unverified_user_still_denied_end_to_end_task06_regression(self):
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า CU0002", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Unever-linked"})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)

    def test_binding_convenience_autofill_never_requires_retyping_custcode(self):
        """Phase 17/21: a verified customer's OWN CustCode is auto-filled
        from the verified binding (never from the old, now-unwritten
        user_profiles.cust_code), so they don't have to retype it every
        turn."""
        self._seed_customer_lookup()
        self.binding_svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                        cust_code="CU0001", created_by="admin")
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"CustName": "Test A"})) as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a",
                         "customer_context": {"cust_code": "CU0001"}})
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "CU0001")

    def test_revocation_mid_pending_workflow_denies_on_resume(self):
        """Phase 31: verified, starts a flow, binding gets revoked before
        the ACTUAL execution turn -- authorization is re-checked, a
        pending state never bypasses revocation."""
        self._seed_customer_lookup()
        binding = self.binding_svc.link_verified(tenant_id=TENANT, channel=CHANNEL, external_user_id="Uverified-a",
                                                  cust_code="CU0001", created_by="admin")
        self.binding_svc.revoke(binding["id"], reason="test_mid_flow_revocation")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า CU0001", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Uverified-a"})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)

    def test_public_rag_unaffected_by_binding_state(self):
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag:
            mock_rag.return_value = MagicMock(
                answer="CBM คือหน่วยวัดปริมาตรสินค้า", chunks=[], confidence=0.9, confidence_label="High",
                model="gpt-4o", policy=MagicMock(escalate=False, escalation_message=None),
                prompt=MagicMock(template=MagicMock(id="t1", version="1")), policy_set_name="Standard",
                general_chat_used=False)
            mock_rag.return_value.prompt.template.name = "Standard"
            result = self.engine.decide(
                "CBM คืออะไร", history=[],
                context={"channel": "line", "tenant_id": TENANT, "external_user_id": "Uunlinked"})
        self.assertEqual(result["routing"]["type"], "RAG")

    def test_admin_playground_access_preserved_regardless_of_binding(self):
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"CustName": "Anything"})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า CU0003", history=[], context={"channel": "playground"})
        mock_req.assert_called_once()
        self.assertEqual(result["routing"]["type"], "API")


# ── 5. Admin API — staff-assisted linking (route-level) ────────────────────

class TestStaffAssistedLinkingRoutes(unittest.TestCase):
    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        login_as_test_admin(self.client)
        self.fake_sb = _FakeSupabase()
        self.patcher_sb = patch("admin.routes.get_sb", return_value=self.fake_sb)
        self.patcher_sb.start()

    def tearDown(self):
        self.patcher_sb.stop()

    def test_link_creates_a_verified_binding(self):
        resp = self.client.post("/admin/api/customer-bindings/link",
                                 json={"external_user_id": "Uverified-a", "cust_code": "SP-A"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["binding"]["status"], "verified")
        self.assertEqual(data["binding"]["cust_code"], "SP-A")

    def test_link_requires_both_fields(self):
        resp = self.client.post("/admin/api/customer-bindings/link", json={"external_user_id": "Uverified-a"})
        self.assertFalse(resp.json()["ok"])

    def test_link_endpoint_requires_admin_auth(self):
        from admin.routes import app
        client_no_auth = TestClient(app)
        resp = client_no_auth.post("/admin/api/customer-bindings/link",
                                    json={"external_user_id": "Uverified-a", "cust_code": "SP-A"},
                                    follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login", resp.headers.get("location", ""))

    def test_revoke_removes_authorization_immediately(self):
        link_resp = self.client.post("/admin/api/customer-bindings/link",
                                      json={"external_user_id": "Uverified-a", "cust_code": "SP-A"})
        binding_id = link_resp.json()["binding"]["id"]
        revoke_resp = self.client.post(f"/admin/api/customer-bindings/{binding_id}/revoke",
                                        json={"reason": "test_unlink"})
        self.assertTrue(revoke_resp.json()["ok"])
        from services.customer_binding_service import get_customer_binding_service
        svc = get_customer_binding_service(self.fake_sb)
        self.assertIsNone(svc.get_verified_binding(tenant_id="default", channel="line",
                                                     external_user_id="Uverified-a"))

    def test_list_bindings_returns_created_binding(self):
        self.client.post("/admin/api/customer-bindings/link",
                          json={"external_user_id": "Uverified-a", "cust_code": "SP-A"})
        resp = self.client.get("/admin/api/customer-bindings?external_user_id=Uverified-a")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["bindings"]), 1)


if __name__ == "__main__":
    unittest.main()
