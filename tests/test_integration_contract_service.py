"""Tests for services/integration_contract_service.py — the Integration
Contract Layer (Part 1-13, 17). Uses the same _FakeSupabase mock and the
same fixture builders as tests/test_erp_test_harness.py — never a real
DB/network call."""
import unittest

from tests.test_business_action_registry import _FakeSupabase
from tests.test_erp_test_harness import (
    _customer_lookup_action, _order_lookup_action, _product_lookup_action,
    _invoice_lookup_action, _cancel_order_action,
)
from services.integration_contract_service import (
    describe_integration, describe_integration_cached, invalidate_contract_cache,
    validate_integration_contract, resolve_effective_contract, list_integrations,
    list_capabilities, list_entities, list_operation_types, list_providers,
    list_required_inputs, list_response_fields, get_contract_version,
    CONTRACT_VERSION, RESOLVER_VERSION,
)


class TestDescribeIntegrationShape(unittest.TestCase):
    def test_order_lookup_contract_shape(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        contract = describe_integration(action_id, sb=sb)
        self.assertEqual(contract["contract_version"], CONTRACT_VERSION)
        self.assertEqual(contract["operation_type"], "LOOKUP")
        self.assertEqual(contract["capability"], "order.lookup")
        self.assertIn("order", contract["entities"])
        names = {f["technical_name"] for f in contract["inputs"]}
        self.assertEqual(names, {"OrderNo"})
        out_names = {f["canonical_name"] for f in contract["outputs"]}
        self.assertEqual(out_names, {"order.status", "tracking.identifier", "order.balance"})
        self.assertTrue(contract["conversation"]["ask_requested_information"])
        self.assertFalse(contract["conversation"]["require_confirmation_before_execute"])

    def test_cancel_order_contract_conversation_behavior(self):
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        contract = describe_integration(action_id, sb=sb)
        self.assertEqual(contract["operation_type"], "CANCEL")
        self.assertFalse(contract["conversation"]["ask_requested_information"])
        self.assertTrue(contract["conversation"]["require_confirmation_before_execute"])

    def test_no_secret_value_in_contract(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        contract = describe_integration(action_id, sb=sb)
        blob = str(contract)
        # only a credential reference/pointer, never a raw secret value
        self.assertIn("credential_reference", contract["execution"])
        self.assertNotIn("sk-", blob)


class TestValidation(unittest.TestCase):
    def test_valid_contract_has_no_errors(self):
        sb = _FakeSupabase()
        reg, action_id = _product_lookup_action(sb)
        result = validate_integration_contract(action_id, sb=sb)
        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])

    def test_action_not_found_is_invalid(self):
        sb = _FakeSupabase()
        result = validate_integration_contract("does-not-exist", sb=sb)
        self.assertFalse(result["valid"])
        self.assertIn("action_not_found", result["errors"])

    def test_errors_and_warnings_are_separate_lists(self):
        sb = _FakeSupabase()
        reg, action_id = _invoice_lookup_action(sb)
        result = validate_integration_contract(action_id, sb=sb)
        self.assertIsInstance(result["errors"], list)
        self.assertIsInstance(result["warnings"], list)


class TestResolveEffectiveContract(unittest.TestCase):
    def test_wraps_contract_validation_and_provenance(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        result = resolve_effective_contract(action_id, sb=sb)
        self.assertIsNotNone(result["contract"])
        self.assertIn("valid", result["validation"])
        self.assertIsInstance(result["provenance"], dict)

    def test_missing_action_returns_none_contract(self):
        sb = _FakeSupabase()
        result = resolve_effective_contract("missing", sb=sb)
        self.assertIsNone(result["contract"])
        self.assertFalse(result["validation"]["valid"])


class TestPlatformServiceApis(unittest.TestCase):
    def test_list_integrations_and_capabilities(self):
        sb = _FakeSupabase()
        _order_lookup_action(sb)
        _product_lookup_action(sb)
        contracts = list_integrations(sb=sb)
        self.assertEqual(len(contracts), 2)
        caps = list_capabilities(sb=sb)
        self.assertIn("order.lookup", caps)
        self.assertIn("product.lookup", caps)

    def test_list_entities_and_providers(self):
        sb = _FakeSupabase()
        _order_lookup_action(sb)
        self.assertIn("order", list_entities(sb=sb))
        self.assertTrue(list_providers(sb=sb))

    def test_list_operation_types_is_static_vocabulary(self):
        ops = list_operation_types()
        self.assertIn("LOOKUP", ops)
        self.assertIn("UNKNOWN", ops)

    def test_list_required_inputs_and_response_fields(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        required = list_required_inputs(action_id, sb=sb)
        self.assertEqual([f["technical_name"] for f in required], ["OrderNo"])
        fields = list_response_fields(action_id, sb=sb)
        self.assertEqual(len(fields), 3)

    def test_filters_by_operation_type_and_entity(self):
        sb = _FakeSupabase()
        _order_lookup_action(sb)
        _cancel_order_action(sb)
        lookups = list_integrations({"operation_type": "LOOKUP"}, sb=sb)
        self.assertEqual(len(lookups), 1)
        cancels = list_integrations({"operation_type": "CANCEL"}, sb=sb)
        self.assertEqual(len(cancels), 1)


class TestContractCache(unittest.TestCase):
    def test_cache_returns_same_contract_and_invalidates(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        invalidate_contract_cache()
        c1 = describe_integration_cached(action_id, sb=sb)
        c2 = describe_integration_cached(action_id, sb=sb)
        self.assertEqual(c1, c2)
        reg.set_enabled(action_id, False)
        c3 = describe_integration_cached(action_id, sb=sb)
        self.assertEqual(c3["status"], "disabled")
        self.assertNotEqual(c1["status"], c3["status"])

    def test_invalidate_by_action_id(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        describe_integration_cached(action_id, sb=sb)
        invalidate_contract_cache(action_id)
        # Should not raise and should recompute fine.
        c = describe_integration_cached(action_id, sb=sb)
        self.assertIsNotNone(c)


class TestVersioning(unittest.TestCase):
    def test_get_contract_version_fields(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        version_info = get_contract_version(action_id, sb=sb)
        self.assertEqual(version_info["contract_version"], CONTRACT_VERSION)
        self.assertEqual(version_info["resolver_version"], RESOLVER_VERSION)


if __name__ == "__main__":
    unittest.main()
