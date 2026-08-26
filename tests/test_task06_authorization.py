"""Task 06 (2026-08-26) — Privacy / Authorization / Cross-Customer Data
Protection. Proves the central invariant this task enforces:
IDENTIFIER != AUTHORIZATION. Knowing/guessing another customer's
CustCode/ShipmentCode/OrderCode/tracking number must never be sufficient
to retrieve or mutate that customer's private data over LINE.

ROOT CAUSE (see services/authorization_service.py's own docstring for
the full investigation writeup): no verified LINE-user-to-customer-
account binding exists anywhere in this codebase. The chosen fix is the
SAFEST MINIMAL behavior given that gap -- fail closed on every
customer/order/shipment-scoped Business Action for any non-admin
channel, never a homemade OTP/verification flow. Every scenario below
uses synthetic, obviously-fake identifiers (SP-A/SP-B/CUST-VICTIM/...),
never real customer data.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _engine_with_registry
from services.business_action_registry import BusinessActionRegistry
from services.action_executor import ActionExecutor
from services.authorization_service import (
    requires_verified_identity, check_authorization, _is_admin_context,
    AUTHORIZATION_DENIED_MESSAGE,
)
from profiles.manager import update_profile_from_turn


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {"ok": True}
    resp.text = "raw"
    return resp


# ── 1. Unit tests: services/authorization_service.py classification ────────

class TestRequiresVerifiedIdentityClassification(unittest.TestCase):
    """Mirrors the real production Business Action shapes confirmed live
    during this task's investigation (GetDataCustomer, SendLineNotiCS,
    RequestShippingAddressChange)."""

    def test_getdatacustomer_shaped_action_with_all_optional_identifiers_requires_identity(self):
        # Confirmed live: the real GetDataCustomer action has EVERY
        # identifier parameter individually OPTIONAL (any one suffices).
        # A required-only check would have missed this -- the single most
        # sensitive action in the registry (wallet/coupon/email/phone/name).
        action = {"parameters": [
            {"name": "CustCode", "required": False, "input_source": "customer_message"},
            {"name": "CustEmail", "required": False, "input_source": "customer_message"},
            {"name": "CustName", "required": False, "input_source": "customer_message"},
            {"name": "CustPhone", "required": False, "input_source": "customer_message"},
        ]}
        self.assertTrue(requires_verified_identity(action))

    def test_sendlinenotics_shaped_action_has_no_identifier_and_is_unaffected(self):
        # Confirmed live: SendLineNotiCS's only parameters are SecretCode
        # (credential_store) and Message (customer_message, no identifier
        # name match) -- correctly never gated.
        action = {"parameters": [
            {"name": "SecretCode", "required": True, "input_source": "credential_store"},
            {"name": "Message", "required": False, "input_source": "customer_message"},
        ]}
        self.assertFalse(requires_verified_identity(action))

    def test_address_change_shaped_action_with_required_identifiers_requires_identity(self):
        action = {"parameters": [
            {"name": "CustCode", "required": True, "input_source": "customer_message"},
            {"name": "ShipmentCode", "required": True, "input_source": "customer_message"},
            {"name": "Address", "required": True, "input_source": "customer_message"},
        ]}
        self.assertTrue(requires_verified_identity(action))

    def test_action_with_no_parameters_at_all_is_unaffected(self):
        self.assertFalse(requires_verified_identity({"parameters": []}))
        self.assertFalse(requires_verified_identity({}))

    def test_non_customer_message_identifier_shaped_param_does_not_count(self):
        """A parameter merely NAMED like an identifier but sourced from
        system_generated/fixed_configuration/credential_store is not a
        customer-supplied claim of ownership -- must not trigger the gate."""
        action = {"parameters": [
            {"name": "CustCode", "required": True, "input_source": "fixed_configuration"},
        ]}
        self.assertFalse(requires_verified_identity(action))


class TestAdminContextRecognition(unittest.TestCase):
    def test_playground_channel_is_admin(self):
        self.assertTrue(_is_admin_context({"channel": "playground"}))

    def test_admin_channel_is_admin(self):
        self.assertTrue(_is_admin_context({"channel": "admin"}))

    def test_line_channel_is_not_admin(self):
        self.assertFalse(_is_admin_context({"channel": "line"}))

    def test_missing_channel_is_not_admin(self):
        """Fail-closed by DEFAULT -- an unrecognized/absent channel is
        never silently treated as trusted, including for any future
        caller someone adds without thinking about authorization."""
        self.assertFalse(_is_admin_context({}))
        self.assertFalse(_is_admin_context(None))

    def test_unknown_future_channel_value_is_not_admin(self):
        self.assertFalse(_is_admin_context({"channel": "some_new_channel_nobody_registered"}))


class TestVerifiedBindingExtensionPoint(unittest.TestCase):
    def test_no_binding_resolves_without_sb_or_identity_context(self):
        """Task 06's finding ("no verified binding exists") was fixed by
        Task 06B (2026-08-26, services/customer_binding_service.py) —
        the extension point this test originally proved is now a real
        lookup. What's still true, and still checked here: with no `sb`
        or no tenant_id/external_user_id in context (every caller/test
        that predates Task 06B), nothing can ever resolve as verified —
        the exact same universal fail-closed behavior as before, never a
        crash. See tests/test_task06b_account_linking.py for the full
        verified-binding test suite."""
        sensitive = {"parameters": [{"name": "CustCode", "required": False, "input_source": "customer_message"}]}
        self.assertFalse(check_authorization(sensitive, {}, sb=None)["authorized"])
        self.assertFalse(check_authorization(
            sensitive, {"customer_context": {"cust_code": "SP-A"}}, sb=None)["authorized"])
        self.assertFalse(check_authorization(
            sensitive, {"confirmed": True, "confirmed_action_id": "x"}, sb=None)["authorized"])


class TestCheckAuthorizationMatrix(unittest.TestCase):
    SENSITIVE = {"parameters": [{"name": "CustCode", "required": True, "input_source": "customer_message"}]}
    PUBLIC = {"parameters": []}

    def test_sensitive_action_denied_on_line_channel(self):
        result = check_authorization(self.SENSITIVE, {"channel": "line"})
        self.assertFalse(result["authorized"])

    def test_sensitive_action_denied_with_no_channel(self):
        result = check_authorization(self.SENSITIVE, {})
        self.assertFalse(result["authorized"])

    def test_sensitive_action_allowed_on_admin_channel(self):
        result = check_authorization(self.SENSITIVE, {"channel": "admin"})
        self.assertTrue(result["authorized"])

    def test_sensitive_action_allowed_on_playground_channel(self):
        result = check_authorization(self.SENSITIVE, {"channel": "playground"})
        self.assertTrue(result["authorized"])

    def test_public_action_allowed_regardless_of_channel(self):
        self.assertTrue(check_authorization(self.PUBLIC, {"channel": "line"})["authorized"])
        self.assertTrue(check_authorization(self.PUBLIC, {})["authorized"])

    def test_denial_message_is_neutral_generic_and_reused_verbatim(self):
        """Enumeration resistance (Phase 14/26): the denial text must
        never confirm or deny whether a specific identifier/account
        exists, and must be the SAME string every time -- never
        interpolating the identifier the customer supplied."""
        self.assertNotIn("SP-", AUTHORIZATION_DENIED_MESSAGE)
        self.assertNotIn("{", AUTHORIZATION_DENIED_MESSAGE)


# ── 2. ActionExecutor gate: the single, unbypassable choke point ───────────

class TestActionExecutorAuthorizationGate(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def _make_sensitive_action(self, key="getdatacustomer_like"):
        action_id = _seed_action(self.reg, key=key, action_type="API", category="customer")
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": False, "input_source": "customer_message"},
            {"name": "CustEmail", "required": False, "input_source": "customer_message"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        return action_id

    def test_direct_executor_call_with_no_channel_is_denied_never_fetch_then_hide(self):
        """Proves the gate lives in ActionExecutor.execute() itself, not
        the conversational layer -- a raw, direct call (bypassing
        decide()/webhook entirely, exactly what services/human_handoff_
        service.py, admin/routes.py, and services/erp_test_harness.py all
        do) is STILL gated. The real HTTP call must never fire at all
        (fail closed, never fetch-then-hide)."""
        action_id = self._make_sensitive_action()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 999, "Coupon": ["SECRET10"]})) as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP-VICTIM"}})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["result"]["message"], AUTHORIZATION_DENIED_MESSAGE)
        # No trace of the ERP response ever reaches the result.
        self.assertNotIn("999", str(result))
        self.assertNotIn("SECRET10", str(result))

    def test_same_call_allowed_on_admin_channel(self):
        action_id = self._make_sensitive_action()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"ok": True})) as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP-A"}, "channel": "admin"})
        mock_req.assert_called_once()
        self.assertEqual(result["status"], "success")

    def test_arbitrary_attacker_supplied_custcode_gets_identical_denial_as_a_real_one(self):
        """Enumeration resistance: a made-up, syntactically-plausible
        CustCode and a genuine one must be denied identically -- no
        different error, no different latency-relevant branch, no
        different message."""
        action_id = self._make_sensitive_action()
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})):
            r1 = self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP-DOES-NOT-EXIST-99999"}})
            r2 = self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP-REAL-LOOKING-0001"}})
        self.assertEqual(r1["status"], r2["status"], "denied")
        self.assertEqual(r1["result"]["message"], r2["result"]["message"])

    def test_non_sensitive_action_still_works_with_no_channel(self):
        """The gate must not become a blanket denial of everything --
        only actions that actually name a customer/order/shipment
        identifier are affected."""
        action_id = _seed_action(self.reg, key="notify_only", action_type="API", category="support")
        self.reg.replace_parameters(action_id, [
            {"name": "Message", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/notify", "http_method": "POST"})
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})) as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"Message": "help please"}})
        mock_req.assert_called_once()
        self.assertEqual(result["status"], "success")


# ── 3. End-to-end via decide(): the real LINE customer conversation path ───

class TestEndToEndCrossCustomerDenialViaDecide(unittest.TestCase):
    """Reproduces Attacks A/B/C/G from the Task 06 threat model through
    the FULL decide() pipeline -- exactly the path a real LINE message
    takes (line_bot/webhook.py -> DecisionEngine.decide(), channel="line"
    propagated, no customer_context binding)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_customer_lookup(self):
        action_id = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}-?[A-Za-z0-9]+$"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        return action_id

    def test_own_profile_lookup_is_denied_no_verified_binding_exists(self):
        """Would be an ALLOW under a real verified-binding system; today,
        since no such binding exists ANYWHERE, even the customer's own
        genuine CustCode gets denied -- the explicit, accepted tradeoff:
        no LINE self-service for sensitive data until real account
        linking is built."""
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 500, "CustName": "Real Customer"})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า CU-0001", history=[], context={"channel": "line"})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)

    def test_other_customers_profile_is_denied_identically(self):
        """Attack A: guessing/knowing a DIFFERENT customer's CustCode
        must not extract their wallet/coupon/PII -- denied exactly like
        the "own" case above, proving no distinguishable behavior leaks
        which CustCode values are real."""
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 99999, "CustName": "Victim Customer"})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า CU-VICTIM", history=[], context={"channel": "line"})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)
        self.assertNotIn("99999", str(result))
        self.assertNotIn("Victim Customer", str(result))

    def test_user_supplied_custcode_is_never_trusted_as_authorization(self):
        """Attack B/C: a customer simply TYPING a CustCode (their own or
        someone else's) must never itself act as proof of ownership --
        the central IDENTIFIER != AUTHORIZATION invariant."""
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า CU-ANYTHING", history=[], context={"channel": "line"})
        mock_req.assert_not_called()
        self.assertEqual(result["routing"]["type"], "API")  # correctly routed; denied at execution, not misrouted

    def test_identifier_memory_no_longer_auto_authorizes_a_later_turn(self):
        """Attack F (Identifier Memory Poisoning) companion: even if a
        customer_context carries a remembered cust_code (as an OLD,
        pre-fix profile row still would, until it naturally goes stale),
        that alone must not grant access -- the Authorization Gate checks
        the CHANNEL, not whether an identifier happens to be known."""
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 1})) as mock_req:
            result = self.engine.decide(
                "ข้อมูลลูกค้า", history=[],
                context={"channel": "line", "customer_context": {"cust_code": "CU-0001"}})
        mock_req.assert_not_called()
        self.assertEqual(result["reply"]["text"], AUTHORIZATION_DENIED_MESSAGE)

    def test_playground_admin_context_still_permitted(self):
        """Admin/Playground tooling must remain usable -- staff are
        session-cookie authenticated separately (admin/routes.py `auth`),
        an orthogonal, already-existing gate."""
        self._seed_customer_lookup()
        with patch("services.action_executor.requests.request",
                   return_value=_fake_response(200, {"Wallet": 1})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า CU-0001", history=[], context={"channel": "playground"})
        mock_req.assert_called_once()
        self.assertEqual(result["routing"]["type"], "API")

    def test_public_rag_question_is_entirely_unaffected(self):
        """Public/FAQ content (no Business Action, no identifier) must
        never be caught by this gate -- it never reaches ActionExecutor
        at all."""
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag:
            mock_rag.return_value = MagicMock(
                answer="CBM คือหน่วยวัดปริมาตรสินค้า", chunks=[], confidence=0.9, confidence_label="High",
                model="gpt-4o", policy=MagicMock(escalate=False, escalation_message=None),
                prompt=MagicMock(template=MagicMock(id="t1", version="1")), policy_set_name="Standard")
            mock_rag.return_value.prompt.template.name = "Standard"
            result = self.engine.decide("CBM คืออะไร", history=[], context={"channel": "line"})
        self.assertEqual(result["routing"]["type"], "RAG")


# ── 4. Identifier Memory persistence removal (profiles/manager.py) ─────────

class TestIdentifierMemoryPoisoningClosed(unittest.TestCase):
    """Attack F's root cause: profiles/manager.py used to permanently
    write a customer-TYPED identifier to user_profiles with zero
    ownership verification, keyed only by line_user_id -- "type it once,
    exploit forever". See tests/test_profiles_manager.py's
    TestIdentifierPersistence for the direct unit proof; this class
    proves the same thing through the public entry point signature."""

    def test_update_profile_from_turn_never_writes_identifier_fields(self):
        import profiles.manager as mgr

        class _FakeSelectQuery:
            def __init__(self, row):
                self._row = row

            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def single(self):
                return self

            def execute(self):
                return MagicMock(data=dict(self._row))

        class _FakeUpdateQuery:
            def __init__(self, row):
                self._row = row
                self._payload = None

            def update(self, payload):
                self._payload = payload
                self._row.update(payload)
                return self

            def eq(self, *a, **k):
                return self

            def execute(self):
                return MagicMock(data=[dict(self._row)])

        class _FakeTable:
            def __init__(self, row):
                self._row = row

            def select(self, *a, **k):
                return _FakeSelectQuery(self._row).select(*a, **k)

            def update(self, payload):
                return _FakeUpdateQuery(self._row).update(payload)

        class _FakeSb:
            def __init__(self, row):
                self._row = row

            def table(self, _name):
                return _FakeTable(self._row)

        row = {"line_user_id": "Uvictim"}
        with patch.object(mgr, "supabase", _FakeSb(row)):
            update_profile_from_turn(
                "Uvictim",
                decide_result={"reply": {"text": "ok"}, "error": None, "alert": None},
                conversation_fields={
                    "collected_parameters": {"CustCode": "SP-SOMEONE-ELSES-CODE"},
                    "actionable_intent": None, "broad_intent": None,
                    "routing_type": "API", "erp_request": {}, "escalated": False,
                },
                is_new_conversation=True,
            )
        self.assertNotIn("cust_code", row)


if __name__ == "__main__":
    unittest.main()
