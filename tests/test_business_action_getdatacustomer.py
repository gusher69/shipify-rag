"""Regression tests for the GetDataCustomer Business Action
(customer_data_lookup) and the underlying platform capabilities it
required: Parameter Groups, Secret References, and response/log
sanitization. Uses the same mocked Supabase client as
tests/test_business_action_registry.py — never a real DB, never a real
secret value, never a real network call except where explicitly noted.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import (
    BusinessActionRegistry, generate_action_key, resolve_secret_ref,
    validate_parameter_groups, sanitize_response_body, sanitize_for_preview, mask_secret,
)

SECRET_REF = "FASTTRADE_AI_CHAT_SECRET_CODE"


def _seed_customer_lookup(reg):
    action = reg.create({
        "action_key": "customer_data_lookup", "name": "GetDataCustomer",
        "display_name": "ค้นหาข้อมูลลูกค้า", "description": "ค้นหาข้อมูลลูกค้าจากรหัสลูกค้า อีเมล ชื่อ หรือเบอร์โทร",
        "action_type": "API", "category": "customer", "priority": 10, "enabled": True,
        "ai_description": "ใช้ Action นี้เมื่อลูกค้าหรือเจ้าหน้าที่ต้องการค้นหาข้อมูลเฉพาะของลูกค้า",
        "search_keywords": ["ลูกค้า", "customer", "wallet", "coupon"],
    })
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "SecretCode", "display_name": "Secret Code", "required": True,
         "input_source": "secret_configuration", "secret_ref": SECRET_REF, "send_as": "form",
         "visible_to_customer": False, "visible_in_developer_mode": False, "loggable": False},
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "example_value": "C00001"},
        {"name": "CustEmail", "display_name": "อีเมลลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "validation_type": "email"},
        {"name": "CustName", "display_name": "ชื่อ-นามสกุลลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form"},
        {"name": "CustPhone", "display_name": "เบอร์โทรลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "validation_type": "phone_number"},
    ])
    reg.set_parameter_groups(action_id, [
        {"name": "customer_search_identifier", "rule": "AT_LEAST_ONE",
         "members": ["CustCode", "CustEmail", "CustName", "CustPhone"]},
    ])
    reg.upsert_execution(action_id, {
        "execution_target": "FastTrade AI Chat API: GetDataCustomer",
        "base_url": "https://fasttrade.in.th", "endpoint_path": "/web-service/ai-chat/GetDataCustomer",
        "http_method": "POST", "content_type": "application/x-www-form-urlencoded",
        "headers": {"Accept": "application/json"}, "auth_type": "none", "timeout_seconds": 10,
    })
    return action_id


class TestActionIdGeneration(unittest.TestCase):
    def test_action_id_generates_from_name(self):
        self.assertEqual(generate_action_key("GetDataCustomer"), "getdatacustomer")

    def test_action_id_generation_handles_thai_display_name_gracefully(self):
        # Thai text has no ASCII letters — falls back to a safe non-empty slug.
        key = generate_action_key("ค้นหาข้อมูลลูกค้า")
        self.assertTrue(key)
        self.assertNotIn(" ", key)


class TestGetDataCustomerActionExists(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.action_id = _seed_customer_lookup(self.reg)

    def test_action_exists_with_expected_configuration(self):
        action = self.reg.get(self.action_id)
        self.assertEqual(action["action_key"], "customer_data_lookup")
        self.assertEqual(action["name"], "GetDataCustomer")
        self.assertEqual(action["display_name"], "ค้นหาข้อมูลลูกค้า")
        self.assertEqual(action["action_type"], "API")
        self.assertEqual(action["category"], "customer")
        self.assertTrue(action["enabled"])

    def test_final_url_resolves_correctly(self):
        execution = self.reg.get_execution(self.action_id, mask=False)
        self.assertEqual(execution["endpoint"], "https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer")

    def test_post_method_configured(self):
        execution = self.reg.get_execution(self.action_id, mask=False)
        self.assertEqual(execution["http_method"], "POST")

    def test_accept_json_header_configured(self):
        execution = self.reg.get_execution(self.action_id, mask=False)
        self.assertEqual(execution["headers"].get("Accept"), "application/json")

    def test_request_body_uses_form_urlencoded(self):
        execution = self.reg.get_execution(self.action_id, mask=False)
        self.assertEqual(execution["content_type"], "application/x-www-form-urlencoded")

    def test_secret_code_uses_secret_reference_not_value(self):
        params = self.reg.get_parameters(self.action_id)
        secret_param = next(p for p in params if p["name"] == "SecretCode")
        self.assertEqual(secret_param["secret_ref"], SECRET_REF)
        self.assertEqual(secret_param["input_source"], "secret_configuration")
        # The parameter row itself has no value column at all — only a reference name.
        self.assertNotIn("value", secret_param)

    def test_secret_code_never_requested_from_customer(self):
        params = self.reg.get_parameters(self.action_id)
        secret_param = next(p for p in params if p["name"] == "SecretCode")
        self.assertFalse(secret_param["visible_to_customer"])
        self.assertFalse(secret_param["visible_in_developer_mode"])
        self.assertFalse(secret_param["loggable"])


class TestSecretNeverAppearsAnywhere(unittest.TestCase):
    """A real-looking secret value is used ONLY as an in-memory env var
    for these tests — it is never written to any file this test suite
    touches (no fixture, no snapshot, no DB row)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.action_id = _seed_customer_lookup(self.reg)
        self.fake_secret = "SUPER-SECRET-VALUE-abc123"

    def test_real_secret_not_in_db_config_output(self):
        full = self.reg.get_full(self.action_id)
        dumped = str(full)
        self.assertNotIn(self.fake_secret, dumped)

    def test_real_secret_not_in_export(self):
        exported = self.reg.export_action(self.action_id)
        self.assertNotIn(self.fake_secret, str(exported))

    def test_resolve_secret_ref_reads_env_var_only(self):
        with patch.dict(os.environ, {SECRET_REF: self.fake_secret}):
            self.assertEqual(resolve_secret_ref(SECRET_REF), self.fake_secret)
        # No lingering env var after the patch context — never leaks between tests.
        self.assertNotEqual(os.environ.get(SECRET_REF), self.fake_secret)

    def test_missing_secret_env_var_resolves_to_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SECRET_REF, None)
            self.assertIsNone(resolve_secret_ref(SECRET_REF))


class TestIdentifierGroupValidation(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.action_id = _seed_customer_lookup(self.reg)

    def test_custcode_only_passes(self):
        result = self.reg.validate_can_execute(self.action_id, {"CustCode": "C00001"})
        self.assertTrue(result["ok"])

    def test_custemail_only_passes(self):
        result = self.reg.validate_can_execute(self.action_id, {"CustEmail": "a@b.com"})
        self.assertTrue(result["ok"])

    def test_custname_only_passes(self):
        result = self.reg.validate_can_execute(self.action_id, {"CustName": "สมชาย ใจดี"})
        self.assertTrue(result["ok"])

    def test_custphone_only_passes(self):
        result = self.reg.validate_can_execute(self.action_id, {"CustPhone": "0812345678"})
        self.assertTrue(result["ok"])

    def test_no_identifier_fails(self):
        result = self.reg.validate_can_execute(self.action_id, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_groups"][0]["name"], "customer_search_identifier")

    def test_multiple_identifiers_allowed(self):
        result = self.reg.validate_can_execute(self.action_id, {"CustCode": "C00001", "CustEmail": "a@b.com"})
        self.assertTrue(result["ok"])


class TestParameterGroupRulesAreGeneric(unittest.TestCase):
    """The group mechanism must be reusable by ANY future action, never
    hardcoded to customer lookup — verifies ALL/EXACTLY_ONE/OPTIONAL too."""

    def test_all_rule(self):
        action = {"parameter_groups": [{"name": "g", "rule": "ALL", "members": ["a", "b"]}]}
        self.assertFalse(validate_parameter_groups(action, [], {"a": "1"})["ok"])
        self.assertTrue(validate_parameter_groups(action, [], {"a": "1", "b": "2"})["ok"])

    def test_exactly_one_rule(self):
        action = {"parameter_groups": [{"name": "g", "rule": "EXACTLY_ONE", "members": ["a", "b"]}]}
        self.assertTrue(validate_parameter_groups(action, [], {"a": "1"})["ok"])
        self.assertFalse(validate_parameter_groups(action, [], {"a": "1", "b": "2"})["ok"])
        self.assertFalse(validate_parameter_groups(action, [], {})["ok"])

    def test_optional_rule_always_passes(self):
        action = {"parameter_groups": [{"name": "g", "rule": "OPTIONAL", "members": ["a"]}]}
        self.assertTrue(validate_parameter_groups(action, [], {})["ok"])

    def test_order_api_style_group_reuses_same_mechanism(self):
        """Example from the spec: an Order API requiring order_number OR
        tracking_number — same AT_LEAST_ONE mechanism, no new code."""
        action = {"parameter_groups": [{"name": "order_identifier", "rule": "AT_LEAST_ONE",
                                         "members": ["order_number", "tracking_number"]}]}
        self.assertTrue(validate_parameter_groups(action, [], {"tracking_number": "TH123"})["ok"])
        self.assertFalse(validate_parameter_groups(action, [], {})["ok"])


class TestResponseSanitization(unittest.TestCase):
    def test_sanitize_response_body_masks_pii_shaped_fields(self):
        body = {"CustCode": "C00001", "CustEmail": "somchai@example.com", "CustPhone": "0812345678",
                "wallet": {"balance": 100}}
        sanitized = sanitize_response_body(body)
        self.assertNotEqual(sanitized["CustEmail"], "somchai@example.com")
        self.assertTrue(sanitized["CustEmail"].startswith("****"))
        self.assertNotEqual(sanitized["CustPhone"], "0812345678")
        self.assertEqual(sanitized["wallet"]["balance"], 100)  # non-PII fields untouched

    def test_sanitize_for_preview_masks_email_and_phone_in_free_text(self):
        text = sanitize_for_preview("ติดต่อ somchai@example.com หรือ 0812345678")
        self.assertNotIn("somchai@example.com", text)
        self.assertNotIn("0812345678", text)

    def test_mask_secret_never_shows_full_value(self):
        self.assertNotEqual(mask_secret("SUPER-SECRET-VALUE"), "SUPER-SECRET-VALUE")


class TestExistingBusinessActionTestsUnaffected(unittest.TestCase):
    """Sanity check that seeding/validating a real action didn't disturb
    the generic registry behavior other tests rely on."""

    def test_generic_action_without_groups_still_works(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "generic_tool", "name": "Generic Tool", "action_type": "TOOL"})
        result = reg.validate_can_execute(action["id"], {})
        self.assertTrue(result["ok"])  # no groups, no required params -> executable


if __name__ == "__main__":
    unittest.main()
