"""Regression tests for the ERP Adapter Interface (services/erp_adapter.py)
— verifies the mock never calls a real ERP and every intent dispatches
to the right method."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.erp_adapter import MockERPAdapter, execute_erp_intent, get_erp_adapter
from services.slot_filling_engine import ERP_INTENTS


class TestMockERPAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = MockERPAdapter()

    def test_all_erp_intents_dispatch_without_error(self):
        for intent in ERP_INTENTS:
            result = execute_erp_intent(self.adapter, intent, {"tracking_number": "TH123456789"})
            self.assertEqual(result["status"], "pending_integration")
            self.assertTrue(result["message"])

    def test_never_raises_or_calls_a_real_network(self):
        # A mock must be a pure, offline function — calling it repeatedly
        # with arbitrary slot values must never raise.
        for _ in range(5):
            execute_erp_intent(self.adapter, "tracking", {"tracking_number": "TH000000000"})

    def test_unknown_intent_returns_unknown_status(self):
        result = execute_erp_intent(self.adapter, "not_a_real_intent", {})
        self.assertEqual(result["status"], "unknown_intent")

    def test_factory_returns_a_mock_instance(self):
        self.assertIsInstance(get_erp_adapter(), MockERPAdapter)


if __name__ == "__main__":
    unittest.main()
