"""Regression tests for the Business Action Provider abstraction
(Business Action Framework + ERP Sync milestone, Phases 3-5):
Business Action Registry -> Action Executor -> Provider, with a mock
implementation per category (customer/order/tracking/finance/product)
and a configurable, category-keyed factory so a future real ERP
connector can be swapped in without touching the Action or Executor.
"""
import os
import unittest
from unittest.mock import patch

from services.business_action_providers import (
    get_provider, reset_provider_cache, normalize_result, normalize_error,
    MockCustomerProvider, MockOrderProvider, MockTrackingProvider,
    MockFinanceProvider, MockProductProvider, PROVIDER_CATEGORIES,
)
from services.action_executor import TOOL_REGISTRY, _execute_tool


class TestNormalizedResultShape(unittest.TestCase):
    def test_success_shape(self):
        r = normalize_result({"x": 1}, source="mock_customer")
        self.assertEqual(r, {"ok": True, "data": {"x": 1}, "source": "mock_customer", "error": None})

    def test_error_shape(self):
        r = normalize_error("missing", source="mock_order", code="missing_order_code")
        self.assertEqual(r, {"ok": False, "data": None, "source": "mock_order",
                              "error": {"code": "missing_order_code", "message": "missing"}})


class TestMockProviders(unittest.TestCase):
    def test_customer_provider_requires_at_least_one_identifier(self):
        result = MockCustomerProvider().lookup({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "missing_identifier")

    def test_customer_provider_returns_normalized_data(self):
        result = MockCustomerProvider().lookup({"CustCode": "C001"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["customer_code"], "C001")
        self.assertEqual(result["source"], "mock_customer")

    def test_order_provider_requires_order_code(self):
        result = MockOrderProvider().lookup({})
        self.assertFalse(result["ok"])
        result_ok = MockOrderProvider().lookup({"OrderCode": "PO123"})
        self.assertTrue(result_ok["ok"])
        self.assertEqual(result_ok["data"]["order_code"], "PO123")

    def test_tracking_provider_requires_tracking_number(self):
        result = MockTrackingProvider().lookup({})
        self.assertFalse(result["ok"])
        result_ok = MockTrackingProvider().lookup({"TrackingNumber": "TH123456789"})
        self.assertTrue(result_ok["ok"])
        self.assertIn("status", result_ok["data"])

    def test_finance_provider_accepts_invoice_or_order_reference(self):
        result = MockFinanceProvider().lookup({})
        self.assertFalse(result["ok"])
        by_invoice = MockFinanceProvider().lookup({"InvoiceNumber": "INV001"})
        self.assertTrue(by_invoice["ok"])
        by_order = MockFinanceProvider().lookup({"OrderCode": "PO123"})
        self.assertTrue(by_order["ok"])
        self.assertEqual(by_order["data"]["invoice_number"], "INV-PO123")

    def test_product_provider_requires_sku(self):
        result = MockProductProvider().lookup({})
        self.assertFalse(result["ok"])
        result_ok = MockProductProvider().lookup({"SKU": "SKU-001"})
        self.assertTrue(result_ok["ok"])
        self.assertTrue(result_ok["data"]["in_stock"])

    def test_alternate_parameter_name_spellings_accepted(self):
        """A Playground manual-test field name ("customer_code") must
        work the same as the REST-style name ("CustCode")."""
        result = MockCustomerProvider().lookup({"customer_code": "C002"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["customer_code"], "C002")


class TestProviderFactory(unittest.TestCase):
    def setUp(self):
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)

    def test_every_category_resolves_to_a_mock_provider_by_default(self):
        for category in PROVIDER_CATEGORIES:
            provider = get_provider(category)
            self.assertTrue(hasattr(provider, "lookup"))

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            get_provider("not_a_real_category")

    def test_unregistered_provider_kind_raises_not_implemented(self):
        with patch.dict(os.environ, {"BUSINESS_ACTION_PROVIDER_CUSTOMER": "erp"}):
            reset_provider_cache()
            with self.assertRaises(NotImplementedError):
                get_provider("customer")

    def test_provider_instances_are_cached(self):
        first = get_provider("order")
        second = get_provider("order")
        self.assertIs(first, second)


class TestActionExecutorToolWiring(unittest.TestCase):
    """The Action must not know which provider is active — verifying the
    TOOL adapter dispatches through the SAME get_provider() seam, not a
    hardcoded provider reference of its own."""

    def test_all_five_lookup_tools_are_registered(self):
        for name in ("customer_lookup", "order_lookup", "tracking_lookup",
                     "finance_lookup", "product_lookup"):
            self.assertIn(name, TOOL_REGISTRY)

    def test_execute_tool_dispatches_customer_lookup_end_to_end(self):
        action = {"execution": {"execution_target": "customer_lookup"}, "action_key": "customer_lookup"}
        context = {"action_params": {"CustCode": "C999"}}
        result = _execute_tool(action, context, registry=None)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["data"]["customer_code"], "C999")

    def test_execute_tool_reports_error_status_for_missing_params(self):
        action = {"execution": {"execution_target": "order_lookup"}, "action_key": "order_lookup"}
        result = _execute_tool(action, {"action_params": {}}, registry=None)
        self.assertEqual(result["status"], "error")

    def test_collected_slots_also_work_not_only_action_params(self):
        """Backward compatible with the existing conversational
        slot-filling context shape."""
        action = {"execution": {"execution_target": "tracking_lookup"}, "action_key": "tracking_lookup"}
        context = {"collected_slots": {"TrackingNumber": "TH000111"}}
        result = _execute_tool(action, context, registry=None)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["data"]["tracking_number"], "TH000111")


if __name__ == "__main__":
    unittest.main()
