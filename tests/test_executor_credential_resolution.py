"""Tests for Generic Action Executor <-> Credential Store integration:
credential_store parameter resolution, priority fallback to legacy
secret_configuration, masking in request previews, and structured
missing/disabled/revoked credential errors. Never a real DB/network/
secret value — master key is a per-test generated Fernet key."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.credential_store import CredentialStore
from services.action_executor import ActionExecutor

TEST_KEY = Fernet.generate_key().decode()


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {"ok": True}
    resp.text = "raw"
    return resp


class TestExecutorCredentialResolution(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
        self.patcher.start()
        self.sb = _FakeSupabase()
        self.reg = BusinessActionRegistry(self.sb)
        self.cred_store = CredentialStore(self.sb)
        self.executor = ActionExecutor(self.sb)
        self.executor.registry = self.reg

    def tearDown(self):
        self.patcher.stop()

    def _make_action(self):
        action = self.reg.create({"action_key": "search_data_order", "name": "SearchDataOrder", "action_type": "API"})
        self.reg.replace_parameters(action["id"], [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fasttrade_erp_secret"},
            {"name": "CustCode", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action["id"], {"endpoint": "https://fasttrade.in.th/api", "http_method": "GET"})
        return action["id"]

    def test_resolves_credential_store_parameter_at_execution_time(self):
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        with patch("services.action_executor.requests.request", return_value=_fake_response()) as mock_req:
            # channel="admin" (Task 06 Authorization Gate) -- this test
            # exercises credential-store resolution, not customer
            # authorization; CustCode here is arbitrary test data.
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
        self.assertEqual(result["status"], "success")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("SecretCode"), "16513268151")

    def test_missing_credential_blocks_call_with_structured_error(self):
        action_id = self._make_action()
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertIn("SecretCode", result["error"])

    def test_disabled_credential_blocks_call(self):
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        self.cred_store.set_status("default", "fasttrade_erp_secret", "disabled")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")

    def test_revoked_credential_blocks_call(self):
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        self.cred_store.set_status("default", "fasttrade_erp_secret", "revoked")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
        mock_req.assert_not_called()
        self.assertEqual(result["status"], "error")

    def test_credential_value_never_appears_in_result_or_request_preview(self):
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        with patch("services.action_executor.requests.request", return_value=_fake_response()):
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "developer_mode": True})
        dumped = str(result)
        self.assertNotIn("16513268151", dumped)

    def test_exception_message_masks_credential_value(self):
        import requests as _requests
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        with patch("services.action_executor.requests.request",
                   side_effect=_requests.exceptions.ConnectionError("failed with 16513268151")):
            result = self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}})
        self.assertNotIn("16513268151", str(result))

    def test_rotation_transparent_to_business_action(self):
        """After rotating the credential, the SAME Business Action
        (unmodified) picks up the new value automatically."""
        action_id = self._make_action()
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "old_value")
        self.cred_store.rotate("default", "fasttrade_erp_secret", "new_value")
        with patch("services.action_executor.requests.request", return_value=_fake_response()) as mock_req:
            self.executor.execute(action_id, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("SecretCode"), "new_value")

    def test_priority_fallback_to_legacy_secret_configuration(self):
        """A parameter with BOTH credential_ref (unresolvable) and a
        legacy secret_ref falls back to the environment variable per
        the documented resolution priority."""
        action = self.reg.create({"action_key": "legacy_fallback_action", "name": "Legacy", "action_type": "API"})
        self.reg.replace_parameters(action["id"], [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "does_not_exist_yet", "secret_ref": "FALLBACK_ENV_SECRET"},
        ])
        self.reg.upsert_execution(action["id"], {"endpoint": "https://x.test/api", "http_method": "GET"})
        with patch.dict("os.environ", {"FALLBACK_ENV_SECRET": "env_fallback_value"}):
            with patch("services.action_executor.requests.request", return_value=_fake_response()) as mock_req:
                result = self.executor.execute(action["id"], {})
        self.assertEqual(result["status"], "success")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("SecretCode"), "env_fallback_value")

    def test_shared_credential_across_two_actions(self):
        self.cred_store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
        action_1 = self._make_action()
        action_2 = self.reg.create({"action_key": "customer_data_lookup", "name": "GetDataCustomer", "action_type": "API"})["id"]
        self.reg.replace_parameters(action_2, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fasttrade_erp_secret"},
        ])
        self.reg.upsert_execution(action_2, {"endpoint": "https://fasttrade.in.th/api2", "http_method": "GET"})
        with patch("services.action_executor.requests.request", return_value=_fake_response()) as mock_req:
            self.executor.execute(action_1, {"collected_slots": {"CustCode": "C00001"}, "channel": "admin"})
            self.executor.execute(action_2, {})
        calls = mock_req.call_args_list
        self.assertEqual(calls[0].kwargs["params"].get("SecretCode"), "16513268151")
        self.assertEqual(calls[1].kwargs["params"].get("SecretCode"), "16513268151")


if __name__ == "__main__":
    unittest.main()
