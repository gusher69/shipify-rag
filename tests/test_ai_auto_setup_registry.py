"""Tests for the Registry/Decision-Engine changes AI Auto Setup needed:
validation_pattern-based multi-value binding (the actual C00001 vs
PO202601001 fix), and validate_can_enable() Draft/Enable gating. Uses
the same _FakeSupabase mock as the rest of the Business Action test
suite — never a real DB, never a real network call."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import DecisionEngine, _bind_message_to_action


def _search_data_order_registry():
    reg = BusinessActionRegistry(_FakeSupabase())
    action = reg.create({"action_key": "search_data_order", "name": "SearchDataOrder",
                          "display_name": "ค้นหาออเดอร์", "action_type": "API", "category": "order",
                          "enabled": True, "search_keywords": ["PO", "เช็ค PO"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "SecretCode", "required": True, "input_source": "secret_configuration",
         "secret_ref": "FASTTRADE_AI_CHAT_SECRET_CODE"},
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message",
         "validation_type": "regex", "validation_pattern": r"^C\d{5}$", "description": "กรุณาแจ้งรหัสลูกค้าครับ"},
        {"name": "OrderCode", "display_name": "เลขคำสั่งซื้อหรือเลข PO", "required": True,
         "input_source": "customer_message", "validation_type": "regex", "validation_pattern": r"^PO\d{6,}$",
         "description": "กรุณาแจ้งเลขคำสั่งซื้อหรือเลข PO ครับ"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/SearchDataOrder",
                                      "http_method": "POST"})
    return reg, action_id


class TestValidationPatternDistinguishesParameters(unittest.TestCase):
    def setUp(self):
        self.reg, self.action_id = _search_data_order_registry()
        self.action = self.reg.get_full(self.action_id, mask_secrets=False)

    def test_ordercode_value_binds_to_ordercode_not_custcode(self):
        outcome = _bind_message_to_action(self.action, self.reg, {}, "PO202601001")
        self.assertEqual(outcome["bound"], ("OrderCode", "PO202601001"))

    def test_custcode_value_binds_to_custcode_not_ordercode(self):
        outcome = _bind_message_to_action(self.action, self.reg, {}, "C00001")
        self.assertEqual(outcome["bound"], ("CustCode", "C00001"))

    def test_both_values_in_one_message_bind_correctly_one_at_a_time(self):
        collected = {}
        outcome1 = _bind_message_to_action(self.action, self.reg, collected, "C00001 PO202601001")
        self.assertIsNotNone(outcome1["bound"])
        collected[outcome1["bound"][0]] = outcome1["bound"][1]
        outcome2 = _bind_message_to_action(self.action, self.reg, collected, "C00001 PO202601001")
        if outcome2["bound"]:
            collected[outcome2["bound"][0]] = outcome2["bound"][1]
        self.assertEqual(collected.get("CustCode"), "C00001")
        self.assertEqual(collected.get("OrderCode"), "PO202601001")


class TestDecisionEngineEitherOrderConversation(unittest.TestCase):
    """Live-verification scenarios A-D from the task spec, exercised
    directly against a mocked registry/executor."""

    def setUp(self):
        self.reg, self.action_id = _search_data_order_registry()
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def test_a_missing_both_asks_one_at_a_time(self):
        r1 = self.engine.decide("เช็ค PO ให้หน่อย", history=[])
        self.assertEqual(r1["routing"]["type"], "WORKFLOW")

    def test_b_ordercode_first_asks_only_custcode(self):
        r = self.engine.decide("เช็ค PO202601001 ให้หน่อย", history=[], context={"developer_mode": True})
        s = r["developer"]["information_collection_status"]
        self.assertEqual(s["collected_parameters"].get("OrderCode"), "PO202601001")
        self.assertIn("CustCode", s["missing_parameters"])

    def test_c_custcode_first_asks_only_ordercode(self):
        r = self.engine.decide("ลูกค้า C00001 ต้องการเช็คออเดอร์", history=[], context={"developer_mode": True})
        s = r["developer"]["information_collection_status"]
        self.assertEqual(s["collected_parameters"].get("CustCode"), "C00001")
        self.assertIn("OrderCode", s["missing_parameters"])

    def test_d_both_values_bind_without_repeated_question(self):
        r = self.engine.decide("เช็ค PO202601001 ของลูกค้า C00001", history=[], context={"developer_mode": True})
        s = r["developer"]["information_collection_status"]
        self.assertEqual(s["collected_parameters"].get("CustCode"), "C00001")
        self.assertEqual(s["collected_parameters"].get("OrderCode"), "PO202601001")
        self.assertEqual(s["missing_parameters"], [])

    def test_executor_blocks_safely_when_secret_missing(self):
        # channel="admin" (Task 06 Authorization Gate) -- this test proves
        # a missing SecretCode blocks the call, not customer authorization.
        r = self.engine.decide("เช็ค PO202601001 ของลูกค้า C00001", history=[],
                                context={"developer_mode": True, "channel": "admin"})
        self.assertEqual(r["routing"]["type"], "API")
        self.assertIn("SecretCode", r["error"])


class TestValidateCanEnable(unittest.TestCase):
    def setUp(self):
        self.reg, self.action_id = _search_data_order_registry()

    def test_complete_action_can_enable(self):
        check = self.reg.validate_can_enable(self.action_id)
        self.assertTrue(check["ok"], check["reasons"])

    def test_missing_endpoint_blocks_enable(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "incomplete", "name": "Incomplete", "action_type": "API"})
        check = reg.validate_can_enable(action["id"])
        self.assertFalse(check["ok"])
        self.assertIn("missing_endpoint", check["reasons"])

    def test_secret_parameter_without_ref_blocks_enable(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "bad_secret", "name": "Bad Secret", "action_type": "API"})
        reg.upsert_execution(action["id"], {"endpoint": "https://x.test/api", "http_method": "GET"})
        reg.replace_parameters(action["id"], [
            {"name": "SecretCode", "required": True, "input_source": "secret_configuration", "secret_ref": None},
        ])
        check = reg.validate_can_enable(action["id"])
        self.assertFalse(check["ok"])
        self.assertTrue(any("secret_ref" in r for r in check["reasons"]))

    def test_required_askable_parameter_without_question_blocks_enable(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "no_question", "name": "No Question", "action_type": "API"})
        reg.upsert_execution(action["id"], {"endpoint": "https://x.test/api", "http_method": "GET"})
        reg.replace_parameters(action["id"], [
            {"name": "Foo", "required": True, "input_source": "customer_message"},
        ])
        check = reg.validate_can_enable(action["id"])
        self.assertFalse(check["ok"])
        self.assertTrue(any("missing_follow_up_question" in r for r in check["reasons"]))

    def test_advanced_editor_toggle_still_works_without_gating(self):
        """Regression: the EXISTING set_enabled() (used by the Advanced
        Editor's Enable/Disable toggle) must remain ungated — only the
        NEW AI Auto Setup save route enforces validate_can_enable()."""
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "bare_action", "name": "Bare", "action_type": "TOOL"})
        reg.set_enabled(action["id"], False)
        reg.set_enabled(action["id"], True)  # must not raise
        self.assertTrue(reg.get(action["id"])["enabled"])


if __name__ == "__main__":
    unittest.main()
