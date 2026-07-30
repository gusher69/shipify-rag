"""Part 14 — run_erp_test() wired to the Conversation Strategy Engine.

FIXTURE-ONLY: reuses _shipment_search_action() from
tests/test_conversation_form_generator.py (no real DB action).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_conversation_form_generator import _shipment_search_action
from services import erp_test_harness as harness
from services.conversation_analytics_store import get_analytics_store, reset_analytics_store_for_tests


class TestRunErpTestStrategyWiring(unittest.TestCase):
    def setUp(self):
        reset_analytics_store_for_tests()

    def test_conversation_strategy_key_present_and_additive(self):
        reg, action_id, sb = _shipment_search_action()
        result = harness.run_erp_test(sb=sb, action_id=action_id, message="ขอดูของที่ออกจากจีนล่าสุด",
                                       mode="intent_param",
                                       collected_params={"CustCode": "C00001"})
        self.assertIn("conversation_strategy", result)
        self.assertIn("conversation_form", result)
        self.assertIn("conversation_state", result)
        strategy = result["conversation_strategy"]
        self.assertIsNotNone(strategy)
        self.assertIn("BillStatus", strategy["auto_detected_fields"])
        self.assertIn("Latest", strategy["auto_detected_fields"])

    def test_pre_existing_return_keys_unchanged(self):
        """Existing callers must keep working — spot-check some
        pre-existing top-level keys are still present/typed as before."""
        reg, action_id, sb = _shipment_search_action()
        result = harness.run_erp_test(sb=sb, action_id=action_id, message="hi", mode="intent_param",
                                       collected_params={"CustCode": "C00001"})
        for key in ("ok", "summary", "trace", "conversation_state", "collected_params", "missing_parameters"):
            self.assertIn(key, result)

    def test_analytics_turn_recorded_best_effort(self):
        reg, action_id, sb = _shipment_search_action()
        harness.run_erp_test(sb=sb, action_id=action_id, message="ขอดูของที่ออกจากจีนล่าสุด",
                              mode="intent_param", collected_params={"CustCode": "C00001"})
        rows = get_analytics_store().list_conversations(action_id=action_id)
        self.assertEqual(len(rows), 1)
        self.assertIn("Latest", rows[0]["auto_filled_fields"])


if __name__ == "__main__":
    unittest.main()
