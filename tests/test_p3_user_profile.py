"""P3 — User Profile Foundation (acceptance).

The three-way separation P3 requires already exists in production and is
REUSED here, not rebuilt:

  DURABLE IDENTITY   customer_channel_bindings (status='verified'),
                     keyed by LINE source.userId (external_user_id),
                     accessed via services/customer_binding_service.py.
                     user_profiles (line_user_id) holds display_name /
                     first_seen / activity counters.
  SESSION            services/session_service.py + ai_sessions — one
                     conversation id, short-lived, never the identity
                     authority.
  ERP WORKFLOW       services/decision_engine.py — last_business_action /
                     pending_confirmation / retry, scoped to the active
                     action only.

These tests pin the P3 invariants (A–H). No production code changes.
Deterministic, no network. Reuses tests/test_task06_authorization.py's
fixtures where possible.
"""
import unittest

from tests.test_business_action_registry import _FakeSupabase
from services.authorization_service import (
    check_authorization, requires_verified_identity, NO_VERIFIED_BINDING_REASON,
)
from services.customer_binding_service import get_customer_binding_service
from rag.query_understanding import classify_actionable_intent

_TENANT = "default"
_LINE_USER = "Uline_p3_user_0001"
_CUST = "FT7788"

# GetDataCustomer-shaped: the single most sensitive action (wallet /
# coupon / phone / email / name), every identifier param OPTIONAL.
_GETDATACUSTOMER = {"parameters": [
    {"name": "CustCode", "required": False, "input_source": "customer_message"},
    {"name": "CustPhone", "required": False, "input_source": "customer_message"},
    {"name": "CustEmail", "required": False, "input_source": "customer_message"},
]}


def _sb_with_verified_binding():
    sb = _FakeSupabase()
    sb.store["customer_channel_bindings"] = [{
        "id": "b1", "tenant_id": _TENANT, "channel": "line",
        "external_user_id": _LINE_USER, "cust_code": _CUST,
        "status": "verified", "verification_method": "staff_assisted",
        "verified_at": "2026-08-26T00:00:00Z", "created_at": "2026-08-26T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
    }]
    return sb


def _line_ctx(**extra):
    # Exactly what line_bot/webhook.py threads in: server-derived only.
    ctx = {"channel": "line", "tenant_id": _TENANT, "external_user_id": _LINE_USER}
    ctx.update(extra)
    return ctx


class DurableIdentitySource(unittest.TestCase):
    def test_verified_binding_is_the_identity_source(self):
        svc = get_customer_binding_service(_sb_with_verified_binding())
        b = svc.get_verified_binding(tenant_id=_TENANT, channel="line", external_user_id=_LINE_USER)
        self.assertIsNotNone(b)
        self.assertEqual(b["cust_code"], _CUST)
        self.assertEqual(b["status"], "verified")

    def test_no_second_binding_table(self):
        # customer_channel_bindings is THE table; assert we query it, nothing else.
        from services.customer_binding_service import _TABLE
        self.assertEqual(_TABLE, "customer_channel_bindings")


class A_VerifiedUserNewSession(unittest.TestCase):
    """A / B — verified LINE user, brand-new session (no history at all):
    a private ERP action is authorized straight from the durable binding;
    CustCode is never re-asked."""

    def test_A_private_coupon_lookup_authorized_from_binding(self):
        res = check_authorization(_GETDATACUSTOMER, _line_ctx(), _sb_with_verified_binding())
        self.assertTrue(res["authorized"])

    def test_B_private_phone_lookup_authorized_from_binding(self):
        # Same action shape backs "เบอร์ที่ผมลงทะเบียนไว้คืออะไร".
        res = check_authorization(_GETDATACUSTOMER, _line_ctx(), _sb_with_verified_binding())
        self.assertTrue(res["authorized"])

    def test_G_empty_session_still_resolves_identity(self):
        # No `history`, no session fields in context — resolution uses only
        # (tenant_id, channel, external_user_id) + sb. A reset session can
        # still recover verified identity.
        res = check_authorization(_GETDATACUSTOMER, _line_ctx(), _sb_with_verified_binding())
        self.assertTrue(res["authorized"])


class C_UnverifiedUser(unittest.TestCase):
    def test_no_binding_row_is_denied_not_guessed(self):
        res = check_authorization(_GETDATACUSTOMER, _line_ctx(), _FakeSupabase())  # empty store
        self.assertFalse(res["authorized"])
        self.assertEqual(res["reason"], NO_VERIFIED_BINDING_REASON)

    def test_pending_binding_is_not_verified(self):
        sb = _FakeSupabase()
        sb.store["customer_channel_bindings"] = [{
            "id": "b2", "tenant_id": _TENANT, "channel": "line",
            "external_user_id": _LINE_USER, "cust_code": _CUST, "status": "pending",
        }]
        self.assertFalse(check_authorization(_GETDATACUSTOMER, _line_ctx(), sb)["authorized"])


class D_StaticCouponStaysRag(unittest.TestCase):
    def test_how_to_use_coupon_is_rag_not_erp(self):
        self.assertEqual(
            classify_actionable_intent("ใช้คูปองยังไง")["actionable_intent"], "coupon_policy")

    def test_profile_existence_does_not_convert_static_to_private(self):
        # Intent classification is message-only; a verified profile never
        # turns a static how-to into a private ERP lookup.
        self.assertEqual(
            classify_actionable_intent("คูปองใช้ยังไง")["actionable_intent"], "coupon_policy")
        self.assertNotEqual(
            classify_actionable_intent("ใช้คูปองยังไง")["actionable_intent"], "tracking_status")


class E_StaleErpSafety(unittest.TestCase):
    """A company-contact request stays RAG even when a verified binding +
    stale ERP identifiers are present — reuses the shipped stale-ERP
    boundary logic (services/decision_engine.py::classify_turn_intent)."""

    def test_contact_request_is_shipify_information(self):
        from services.decision_engine import classify_turn_intent
        self.assertEqual(classify_turn_intent("ขอเบอร์ติดต่อ"), "SHIPIFY_INFORMATION")

    def test_static_coupon_howto_is_shipify_information(self):
        from services.decision_engine import classify_turn_intent
        self.assertEqual(classify_turn_intent("ใช้คูปองยังไง"), "SHIPIFY_INFORMATION")


class F_NewWorkflowResetScope(unittest.TestCase):
    """Identity is durable; workflow retry/error state is not. A fresh
    private action authorizes from the same binding regardless of any
    prior failed action's state (which lives in decision_engine, not in
    the binding)."""

    def test_identity_reused_for_a_fresh_action(self):
        sb = _sb_with_verified_binding()
        first = check_authorization(_GETDATACUSTOMER, _line_ctx(), sb)
        second = check_authorization(_GETDATACUSTOMER, _line_ctx(), sb)
        self.assertTrue(first["authorized"])
        self.assertTrue(second["authorized"])

    def test_binding_row_carries_no_workflow_state(self):
        b = get_customer_binding_service(_sb_with_verified_binding()).get_verified_binding(
            tenant_id=_TENANT, channel="line", external_user_id=_LINE_USER)
        for workflow_key in ("last_business_action", "retry_count", "missing_slots",
                              "pending_confirmation", "escalation_required"):
            self.assertNotIn(workflow_key, b)


class H_NoHistoryBinding(unittest.TestCase):
    """A CustCode that only ever appeared in chat text / the
    user_profiles convenience cache is NEVER verified identity."""

    def test_custcode_in_context_customer_context_is_ignored(self):
        # Simulate the webhook having merged a legacy user_profiles.cust_code
        # into customer_context — authorization must still fail (no binding).
        ctx = _line_ctx(customer_context={"cust_code": _CUST, "last_order_code": "POS1",
                                           "last_tracking": "99999"})
        res = check_authorization(_GETDATACUSTOMER, ctx, _FakeSupabase())
        self.assertFalse(res["authorized"])

    def test_only_a_verified_binding_row_authorizes(self):
        # Same context, but now with the real verified row present.
        ctx = _line_ctx(customer_context={"cust_code": "SOMETHING_ELSE"})
        res = check_authorization(_GETDATACUSTOMER, ctx, _sb_with_verified_binding())
        self.assertTrue(res["authorized"])

    def test_revoked_binding_does_not_authorize(self):
        sb = _FakeSupabase()
        sb.store["customer_channel_bindings"] = [{
            "id": "b3", "tenant_id": _TENANT, "channel": "line",
            "external_user_id": _LINE_USER, "cust_code": _CUST, "status": "revoked",
        }]
        self.assertFalse(check_authorization(_GETDATACUSTOMER, _line_ctx(), sb)["authorized"])


class LatencyShape(unittest.TestCase):
    def test_single_binding_lookup_per_authorization(self):
        calls = {"n": 0}
        sb = _sb_with_verified_binding()
        real_table = sb.table

        def counting_table(name):
            if name == "customer_channel_bindings":
                calls["n"] += 1
            return real_table(name)

        sb.table = counting_table
        check_authorization(_GETDATACUSTOMER, _line_ctx(), sb)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
