"""Tests for the Generic Action Executor (services/action_executor.py).

Covers each supported executor (REST/RAG/Tool/Notification/Human
Handoff/Webhook/Workflow), retry/timeout/secret-resolution/masking
behavior, the common Execution Result shape, and a regression check
that the Business Action Registry itself is untouched by this task.
Never a real DB, never a real network call, never a real secret value.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.action_executor import ActionExecutor, TOOL_REGISTRY, run_rest_call

SECRET_REF = "TEST_ACTION_EXECUTOR_SECRET"


def _make_rest_action(reg, *, with_secret=False, with_group=False):
    action = reg.create({"action_key": "rest_action", "name": "Rest Action", "action_type": "API",
                          "category": "test", "enabled": True})
    action_id = action["id"]
    params = [{"name": "CustCode", "display_name": "Code", "required": False, "input_source": "customer_message"}]
    if with_secret:
        params.append({"name": "SecretCode", "display_name": "Secret", "required": True,
                        "input_source": "secret_configuration", "secret_ref": SECRET_REF,
                        "visible_to_customer": False, "visible_in_developer_mode": False, "loggable": False})
    reg.replace_parameters(action_id, params)
    if with_group:
        reg.set_parameter_groups(action_id, [{"name": "id_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]}])
    reg.upsert_execution(action_id, {
        "execution_target": "Test REST", "endpoint": "https://example.test/api", "http_method": "POST",
        "content_type": "application/json", "headers": {"Accept": "application/json"}, "auth_type": "none",
        "timeout_seconds": 5,
    })
    return action_id


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {"ok": True}
    resp.text = "raw"
    return resp


class TestRestExecutor(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def test_successful_rest_call(self):
        action_id = _make_rest_action(self.reg)
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"status": "ok"})):
            # channel="admin" (Task 06 Authorization Gate) -- this test
            # exercises REST executor mechanics, not customer authorization;
            # CustCode here is arbitrary test data, not a real identifier claim.
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C1"}, "channel": "admin"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["status_code"], 200)
        self.assertIn("latency_ms", result)

    def test_missing_endpoint_returns_structured_error(self):
        action = self.reg.create({"action_key": "no_endpoint", "name": "No Endpoint", "action_type": "API"})
        result = self.executor.execute(action["id"], {})
        self.assertEqual(result["status"], "error")
        self.assertIn("Endpoint", result["error"])

    def test_group_validation_blocks_call_without_network(self):
        action_id = _make_rest_action(self.reg, with_group=True)
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")

    def test_disabled_action_never_executes(self):
        action_id = _make_rest_action(self.reg)
        self.reg.set_enabled(action_id, False)
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertIn("disabled", result["error"])

    def test_unknown_action_id(self):
        result = self.executor.execute("does-not-exist", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "Action not found")


class TestSecretResolutionAndMasking(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def test_missing_secret_env_var_blocks_call(self):
        action_id = _make_rest_action(self.reg, with_secret=True)
        os.environ.pop(SECRET_REF, None)
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C1"}, "channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertIn("SecretCode", result["error"])

    def test_secret_value_never_appears_in_result(self):
        action_id = _make_rest_action(self.reg, with_secret=True)
        fake_secret = "REAL-SECRET-abc123"
        with patch.dict(os.environ, {SECRET_REF: fake_secret}):
            with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})):
                result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C1"}, "channel": "admin"})
        dumped = str(result)
        self.assertNotIn(fake_secret, dumped)
        self.assertIn("[MASKED]", dumped)

    def test_secret_masked_in_connection_error_text(self):
        action_id = _make_rest_action(self.reg, with_secret=True)
        fake_secret = "REAL-SECRET-xyz789"
        import requests as _requests
        with patch.dict(os.environ, {SECRET_REF: fake_secret}):
            with patch("services.action_executor.requests.request",
                       side_effect=_requests.exceptions.ConnectionError(f"failed with {fake_secret}")):
                result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C1"}})
        self.assertNotIn(fake_secret, str(result))


class TestRetryAndTimeout(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def test_retries_on_5xx_up_to_max_retries(self):
        action_id = self.reg.create({"action_key": "retry_action", "name": "Retry", "action_type": "API"})["id"]
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/api", "http_method": "GET",
                                               "retry_policy": {"max_retries": 2, "backoff_seconds": 0}})
        execution = self.reg.get_execution(action_id, mask=False)
        with patch("services.action_executor.requests.request", return_value=_fake_response(500)) as mock_req:
            with patch("services.action_executor.time.sleep"):
                outcome = run_rest_call(execution, {}, {})
        self.assertEqual(mock_req.call_count, 3)  # initial + 2 retries
        self.assertIsNotNone(outcome["error"])

    def test_timeout_produces_friendly_error(self):
        import requests as _requests
        with patch("services.action_executor.requests.request", side_effect=_requests.exceptions.Timeout("timed out")):
            outcome = run_rest_call({"endpoint": "https://example.test/api", "http_method": "GET"}, {}, {})
        self.assertIn("Timeout", outcome["error"])


class TestRagExecutor(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def test_rag_executor_returns_answer_citations_confidence(self):
        action_id = self.reg.create({"action_key": "kb_search", "name": "KB Search", "action_type": "RAG"})["id"]
        fake_chunks = [{"filename": "warehouse.pdf", "public_url": "https://x/warehouse.pdf", "score": 0.9, "text": "Warehouse info"}]
        fake_rag = MagicMock()
        fake_rag.retrieve.return_value = fake_chunks
        fake_rag.build_context.return_value = "Warehouse info"
        with patch("services.rag_service.get_rag_service", return_value=fake_rag):
            result = self.executor.execute(action_id, {"question": "Where is the warehouse?"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["answer"], "Warehouse info")
        self.assertEqual(result["result"]["confidence"], 0.9)
        self.assertEqual(len(result["result"]["citations"]), 1)

    def test_rag_executor_requires_question(self):
        action_id = self.reg.create({"action_key": "kb_search2", "name": "KB Search 2", "action_type": "RAG"})["id"]
        result = self.executor.execute(action_id, {})
        self.assertEqual(result["status"], "error")


class TestToolExecutor(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def test_url_converter_tool(self):
        action_id = self.reg.create({"action_key": "url_converter", "name": "URL Converter", "action_type": "TOOL"})["id"]
        self.reg.upsert_execution(action_id, {"execution_target": "url_converter"})
        url = "https://drive.google.com/file/d/ABC123/view?usp=sharing"
        result = self.executor.execute(action_id, {"url": url})
        self.assertEqual(result["status"], "success")
        self.assertIn("uc?export=download&id=ABC123", result["result"]["converted_url"])

    def test_calculator_tool_no_match_returns_structured_error(self):
        action_id = self.reg.create({"action_key": "calculator", "name": "Calculator", "action_type": "TOOL"})["id"]
        self.reg.upsert_execution(action_id, {"execution_target": "calculator"})
        with patch("rag.calculator.answer_calculation_question", return_value=None):
            result = self.executor.execute(action_id, {"question": "unrelated question"})
        self.assertEqual(result["status"], "error")

    def test_unknown_tool_name_is_structured_error_not_crash(self):
        action_id = self.reg.create({"action_key": "mystery_tool", "name": "Mystery", "action_type": "TOOL"})["id"]
        self.reg.upsert_execution(action_id, {"execution_target": "does_not_exist"})
        result = self.executor.execute(action_id, {})
        self.assertEqual(result["status"], "error")
        self.assertIn("Unknown internal tool", result["error"])

    def test_tool_registry_is_generic_and_extensible(self):
        self.assertIn("calculator", TOOL_REGISTRY)
        self.assertIn("url_converter", TOOL_REGISTRY)


class TestNotificationExecutor(unittest.TestCase):
    def test_notification_is_interface_only(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "notify_customer", "name": "Notify", "action_type": "NOTIFICATION"})["id"]
        result = executor.execute(action_id, {"question": "hello"})
        self.assertEqual(result["status"], "not_implemented")
        self.assertIn("payload", result["result"])


class TestHumanHandoffExecutor(unittest.TestCase):
    def test_handoff_prepares_payload_without_notifying(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "escalate_human", "name": "Escalate", "action_type": "HUMAN_HANDOFF"})["id"]
        result = executor.execute(action_id, {"intent": "complaint", "collected_slots": {"order_number": "123"}})
        self.assertEqual(result["status"], "handoff_prepared")
        self.assertEqual(result["result"]["handoff_payload"]["collected_slots"]["order_number"], "123")


class TestWebhookExecutor(unittest.TestCase):
    def test_webhook_posts_to_configured_endpoint(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "outgoing_hook", "name": "Hook", "action_type": "WEBHOOK"})["id"]
        reg.upsert_execution(action_id, {"endpoint": "https://example.test/hook", "http_method": "POST"})
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"received": True})) as mock_req:
            result = executor.execute(action_id, {"collected_slots": {"note": "hi"}})
        self.assertEqual(result["status"], "success")
        mock_req.assert_called_once()


class TestWorkflowExecutor(unittest.TestCase):
    def test_workflow_is_placeholder(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "multi_step", "name": "Workflow", "action_type": "WORKFLOW"})["id"]
        result = executor.execute(action_id, {})
        self.assertEqual(result["status"], "not_implemented")


class TestExecutionResultShape(unittest.TestCase):
    def test_every_result_has_common_shape(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "shape_check", "name": "Shape", "action_type": "WORKFLOW"})["id"]
        result = executor.execute(action_id, {})
        for key in ("status", "result", "metadata", "latency_ms", "error", "logs"):
            self.assertIn(key, result)
        self.assertEqual(result["metadata"]["executor"], "WORKFLOW")
        self.assertEqual(result["metadata"]["action_id"], action_id)

    def test_developer_mode_adds_debug_metadata(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action_id = reg.create({"action_key": "dev_mode_check", "name": "Dev", "action_type": "WORKFLOW"})["id"]
        result = executor.execute(action_id, {"developer_mode": True, "collected_slots": {"a": "1"}})
        self.assertIn("developer_mode", result["metadata"])
        self.assertEqual(result["metadata"]["developer_mode"]["input_parameters"], {"a": "1"})


class TestUnsupportedActionType(unittest.TestCase):
    def test_unsupported_action_type_returns_error_not_crash(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action = reg.create({"action_key": "bad_type", "name": "Bad", "action_type": "WORKFLOW"})
        reg._sb.store["business_actions"][0]["action_type"] = "NOT_A_REAL_TYPE"
        result = executor.execute(action["id"], {})
        self.assertEqual(result["status"], "error")
        self.assertIn("Unsupported action_type", result["error"])


class TestCustomerMessageFallbackDefault(unittest.TestCase):
    """Regression for the confirmed Latest=5 bug: a customer_message
    parameter with example_value="5" must use the customer's explicit
    value when given, but still fall back to that configured default on
    turns where the customer said nothing — without being
    fixed_configuration (which can never be overridden)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.executor = ActionExecutor(self.reg._sb)
        self.executor.registry = self.reg

    def _make_action_with_latest(self):
        action = self.reg.create({"action_key": "list_action", "name": "List", "action_type": "API"})
        action_id = action["id"]
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": True, "input_source": "customer_message"},
            {"name": "Latest", "required": False, "input_source": "customer_message", "example_value": "5"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/api", "http_method": "POST"})
        return action_id

    def test_explicit_customer_value_overrides_default(self):
        action_id = self._make_action_with_latest()
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})) as mock_req:
            self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP1014", "Latest": "3"},
                                               "channel": "admin"})
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("Latest"), "3")

    def test_falls_back_to_example_value_when_not_specified(self):
        action_id = self._make_action_with_latest()
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})) as mock_req:
            self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP1014"}, "channel": "admin"})
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("Latest"), "5")

    def test_grouped_member_never_falls_back_to_example_value(self):
        """Final Conversational Correctness (2026-08-15) — a parameter
        that is a member of a parameter GROUP (e.g. GetDataCustomer's
        customer_identifier AT_LEAST_ONE: CustCode/CustEmail/CustName/
        CustPhone) must NEVER receive its example_value fallback, even
        though it's individually non-required. example_value holds
        documentation/Test-Action placeholder data (e.g.
        "customer@example.com") — sending it to the real ERP whenever the
        customer only supplied ONE sibling identifier (e.g. CustCode)
        would fabricate identity data the customer never gave. Confirmed
        live: a request with only CustCode collected was still sending a
        fake CustEmail/CustName/CustPhone alongside it."""
        action = self.reg.create({"action_key": "customer_lookup", "name": "C", "action_type": "API"})
        action_id = action["id"]
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": False, "input_source": "customer_message", "example_value": "C00001"},
            {"name": "CustEmail", "required": False, "input_source": "customer_message",
             "example_value": "customer@example.com"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/api", "http_method": "POST"})
        with patch("services.action_executor.requests.request", return_value=_fake_response(200, {"ok": True})) as mock_req:
            self.executor.execute(action_id, {"collected_slots": {"CustCode": "SP1014"}, "channel": "admin"})
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("CustCode"), "SP1014")
        self.assertNotIn("CustEmail", sent_body)

    def test_required_customer_message_param_has_no_silent_fallback(self):
        # A REQUIRED customer_message parameter must not silently pull in
        # its example_value — that would defeat "required" validation by
        # making a missing required slot look satisfied.
        action = self.reg.create({"action_key": "required_no_fallback", "name": "R", "action_type": "API"})
        action_id = action["id"]
        self.reg.replace_parameters(action_id, [
            {"name": "OrderCode", "required": True, "input_source": "customer_message", "example_value": "PO000"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/api", "http_method": "POST"})
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {}, "channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")


class TestRegistryUnaffectedByExecutor(unittest.TestCase):
    """Regression: this task must not have modified the Registry's own
    behavior — only added a new consumer module."""

    def test_registry_crud_still_works(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "still_works", "name": "Still Works", "action_type": "TOOL"})
        self.assertTrue(reg.get(action["id"])["enabled"])


if __name__ == "__main__":
    unittest.main()
