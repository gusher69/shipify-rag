"""Tests for Smart Setup's credential-detection-and-linking flow —
local secret detection running before the LLM call, proposal parameters
defaulting to credential_store, and the save-flow's
"create-or-select-credential then link credential_ref" behavior. The
LLM is always mocked; the master key is a per-test generated Fernet
key, never a real secret."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.credential_store import CredentialStore
from services.ai_auto_setup_service import analyze_capability, detect_and_redact_secrets
from services.llm_service import LLMResponse

TEST_KEY = Fernet.generate_key().decode()


def _mock_llm(proposal_dict):
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(text=json.dumps(proposal_dict), model="gpt-4o-mini",
                                              provider="openai", input_tokens=10, output_tokens=20, latency_ms=50.0)
    return fake


def _base_proposal(**overrides):
    proposal = {
        "action_name": "SearchDataOrder", "display_name": "ค้นหา PO", "action_id": "search_data_order",
        "description": "ค้นหา PO รายการเดียว", "category": "order", "action_type": "API",
        "http_method": "POST", "base_url": "https://fasttrade.in.th",
        "endpoint_path": "/web-service/ai-chat/SearchDataOrder", "content_type": "application/x-www-form-urlencoded",
        "headers": {}, "parameters": [
            {"name": "SecretCode", "display_name": "Secret Code", "required": True,
             "input_source": "secret_configuration", "secret_ref": None, "example_value": None,
             "validation_type": None, "validation_pattern": None, "validation_confidence": "high",
             "follow_up_options": []},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "secret_ref": None, "example_value": "C00001",
             "validation_type": "regex", "validation_pattern": r"^C\d{5}$", "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้งรหัสลูกค้าครับ"]},
        ],
        "parameter_groups": [], "keywords": ["PO"], "example_questions": ["เช็ค PO ให้หน่อย"],
        "response_mapping": [], "customer_facing_response_template": "{status}",
    }
    proposal.update(overrides)
    return proposal


class TestSecretDetectionBeforeLlm(unittest.TestCase):
    def test_llm_never_sees_the_raw_secret_value(self):
        fake_llm = _mock_llm(_base_proposal())
        curl = 'curl -X POST https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer -d "SecretCode=16513268151&CustCode=C00001"'
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            analyze_capability(curl, "ค้นหาข้อมูลลูกค้า", [])
        sent_messages = fake_llm.generate.call_args[0][0]
        sent_text = json.dumps(sent_messages, ensure_ascii=False)
        self.assertNotIn("16513268151", sent_text)
        self.assertIn("[REDACTED_SECRET_1]", sent_text)

    def test_analysis_result_includes_detected_credentials_with_masked_preview_only(self):
        fake_llm = _mock_llm(_base_proposal())
        curl = 'SecretCode=16513268151&CustCode=C00001'
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_capability(curl, "ค้นหาข้อมูลลูกค้า", [])
        self.assertTrue(result["ok"])
        creds = result["detected_credentials"]
        self.assertEqual(len(creds), 1)
        self.assertEqual(creds[0]["name"], "SecretCode")
        self.assertEqual(creds[0]["masked_preview"], "********8151")
        self.assertNotIn("16513268151", json.dumps(creds))

    def test_secret_parameter_defaults_to_credential_store_when_detected(self):
        fake_llm = _mock_llm(_base_proposal())
        curl = 'SecretCode=16513268151&CustCode=C00001'
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_capability(curl, "ค้นหาข้อมูลลูกค้า", [])
        secret_param = next(p for p in result["proposal"]["parameters"] if p["name"] == "SecretCode")
        self.assertEqual(secret_param["input_source"], "credential_store")
        self.assertTrue(secret_param["credential_ref"])
        self.assertIsNone(secret_param["secret_ref"])


class TestCredentialLinkingSaveFlow(unittest.TestCase):
    """Simulates the admin route's save flow: create-or-select the
    credential FIRST, replace the raw value with credential_ref, THEN
    save the Business Action — never storing a raw value in the
    Registry itself."""

    def setUp(self):
        self.patcher = patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
        self.patcher.start()
        self.sb = _FakeSupabase()
        self.reg = BusinessActionRegistry(self.sb)
        self.cred_store = CredentialStore(self.sb)

    def tearDown(self):
        self.patcher.stop()

    def _simulate_save(self, proposal, raw_secret_value):
        """Mirrors admin/routes.py::api_ai_auto_setup_save's credential
        handling: for every credential_store parameter, get-or-create
        the credential (idempotent — no duplicate on repeated saves),
        then persist the action with ONLY credential_ref, never the
        raw value."""
        for p in proposal["parameters"]:
            if p.get("input_source") == "credential_store":
                self.cred_store.get_or_create(
                    "default", p["credential_ref"], p.get("_detected_credential", {}).get("suggested_display_name", p["name"]),
                    "secret_code", raw_secret_value, created_by="admin")
        action = self.reg.create({"action_key": proposal["action_id"], "name": proposal["action_name"],
                                   "action_type": "API", "category": proposal["category"]})
        self.reg.replace_parameters(action["id"], proposal["parameters"])
        self.reg.upsert_execution(action["id"], {
            "base_url": proposal["base_url"], "endpoint_path": proposal["endpoint_path"],
            "http_method": proposal["http_method"], "content_type": proposal["content_type"],
        })
        return action["id"]

    def test_save_stores_credential_ref_not_raw_value(self):
        proposal = _base_proposal()
        proposal["parameters"][0]["input_source"] = "credential_store"
        proposal["parameters"][0]["credential_ref"] = "fasttrade_erp_secret"
        action_id = self._simulate_save(proposal, "16513268151")

        params = self.reg.get_parameters(action_id)
        secret_param = next(p for p in params if p["name"] == "SecretCode")
        self.assertEqual(secret_param["credential_ref"], "fasttrade_erp_secret")
        dumped = json.dumps(params)
        self.assertNotIn("16513268151", dumped)

        outcome = self.cred_store.resolve("default", "fasttrade_erp_secret")
        self.assertEqual(outcome["value"], "16513268151")

    def test_repeated_save_does_not_duplicate_credential(self):
        proposal = _base_proposal()
        proposal["parameters"][0]["input_source"] = "credential_store"
        proposal["parameters"][0]["credential_ref"] = "fasttrade_erp_secret"
        self._simulate_save(proposal, "16513268151")
        self._simulate_save(proposal, "16513268151")  # second analyze+save of the same input
        all_creds = self.sb.store.get("integration_credentials", [])
        self.assertEqual(len(all_creds), 1)

    def test_two_business_actions_share_one_credential(self):
        proposal_a = _base_proposal(action_id="search_data_order")
        proposal_a["parameters"][0]["input_source"] = "credential_store"
        proposal_a["parameters"][0]["credential_ref"] = "fasttrade_erp_secret"
        proposal_b = _base_proposal(action_id="customer_data_lookup", action_name="GetDataCustomer")
        proposal_b["parameters"][0]["input_source"] = "credential_store"
        proposal_b["parameters"][0]["credential_ref"] = "fasttrade_erp_secret"

        action_a = self._simulate_save(proposal_a, "16513268151")
        action_b = self._simulate_save(proposal_b, "16513268151")

        self.assertEqual(
            self.reg.get_parameters(action_a)[0]["credential_ref"],
            self.reg.get_parameters(action_b)[0]["credential_ref"],
        )
        all_creds = self.sb.store.get("integration_credentials", [])
        self.assertEqual(len(all_creds), 1)


if __name__ == "__main__":
    unittest.main()
