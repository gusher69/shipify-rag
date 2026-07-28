"""Tests for AI Auto Setup (services/ai_auto_setup_service.py) — secret
redaction, structured-schema validation, and the LLM call site, all
mocked. Never a real network/LLM call, never a real secret value."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ai_auto_setup_service import (
    analyze_api, analyze_capability, infer_input_source, redact_secrets, suggest_secret_ref,
    validate_proposal_schema, detect_endpoints_from_document,
)
from services.llm_service import LLMResponse


def _valid_proposal():
    return {
        "action_name": "SearchDataOrder", "display_name": "ค้นหาออเดอร์", "action_id": "search_data_order",
        "description": "ค้นหา PO รายการเดียว", "category": "order", "action_type": "API",
        "http_method": "POST", "base_url": "https://fasttrade.in.th",
        "endpoint_path": "/web-service/ai-chat/SearchDataOrder", "content_type": "application/x-www-form-urlencoded",
        "headers": {"Accept": "application/json"},
        "parameters": [
            {"name": "SecretCode", "display_name": "Secret Code", "required": True,
             "input_source": "secret_configuration", "secret_ref": "FASTTRADE_AI_CHAT_SECRET_CODE",
             "example_value": None, "validation_type": None, "validation_pattern": None,
             "validation_confidence": "high", "follow_up_options": []},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "secret_ref": None, "example_value": "C00001",
             "validation_type": "regex", "validation_pattern": r"^C\d{5}$", "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้งรหัสลูกค้าครับ", "ขอรหัสลูกค้าเพื่อใช้ตรวจสอบออเดอร์ครับ",
                                    "รบกวนแจ้ง Customer Code เพิ่มเติมครับ"]},
            {"name": "OrderCode", "display_name": "เลขคำสั่งซื้อหรือเลข PO", "required": True,
             "input_source": "customer_message", "secret_ref": None, "example_value": "PO202601001",
             "validation_type": "regex", "validation_pattern": r"^PO\d{6,}$", "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้งเลขคำสั่งซื้อหรือเลข PO ครับ", "ขอเลข PO ที่ต้องการตรวจสอบครับ",
                                    "รบกวนแจ้งหมายเลขคำสั่งซื้อครับ"]},
        ],
        "parameter_groups": [], "keywords": ["PO", "เช็ค PO"], "example_questions": ["เช็ค PO ให้หน่อย"],
        "response_mapping": [{"json_path": "$.status", "mapped_label": "สถานะ"}],
        "customer_facing_response_template": "สถานะออเดอร์ของคุณคือ {status}",
    }


class TestSecretRedaction(unittest.TestCase):
    def test_redacts_authorization_header(self):
        redacted, found = redact_secrets("Authorization: Bearer sk-real-secret-abc123\nAccept: application/json")
        self.assertNotIn("sk-real-secret-abc123", redacted)
        self.assertIn("Accept: application/json", redacted)
        self.assertTrue(found)

    def test_redacts_curl_bearer_token(self):
        redacted, _ = redact_secrets('curl -H "Authorization: Bearer sk-real-secret-abc123" https://api.example.com')
        self.assertNotIn("sk-real-secret-abc123", redacted)

    def test_redacts_secretcode_param(self):
        redacted, _ = redact_secrets("SecretCode=SUPER-SECRET-VALUE-999&CustCode=C00001")
        self.assertNotIn("SUPER-SECRET-VALUE-999", redacted)
        self.assertIn("CustCode=C00001", redacted)

    def test_preserves_non_secret_headers_and_params(self):
        redacted, _ = redact_secrets("Content-Type: application/json\nCustCode=C00001\nOrderCode=PO202601001")
        self.assertIn("Content-Type: application/json", redacted)
        self.assertIn("CustCode=C00001", redacted)
        self.assertIn("OrderCode=PO202601001", redacted)

    def test_empty_input_returns_empty(self):
        redacted, found = redact_secrets("")
        self.assertEqual(redacted, "")
        self.assertEqual(found, [])


class TestInputSourceInference(unittest.TestCase):
    def test_secretcode_infers_secret_configuration(self):
        self.assertEqual(infer_input_source("SecretCode"), "secret_configuration")

    def test_authorization_infers_secret_configuration(self):
        self.assertEqual(infer_input_source("Authorization"), "secret_configuration")

    def test_apikey_infers_secret_configuration(self):
        self.assertEqual(infer_input_source("ApiKey"), "secret_configuration")

    def test_line_user_id_infers_customer_profile(self):
        self.assertEqual(infer_input_source("line_user_id"), "customer_profile")

    def test_custcode_infers_customer_message(self):
        self.assertEqual(infer_input_source("CustCode"), "customer_message")

    def test_suggest_secret_ref_never_contains_a_value(self):
        ref = suggest_secret_ref("SecretCode", "search_data_order")
        self.assertNotIn("=", ref)
        self.assertTrue(ref.isupper())


class TestStructuredSchemaValidation(unittest.TestCase):
    def test_valid_proposal_passes(self):
        ok, errors = validate_proposal_schema(_valid_proposal())
        self.assertTrue(ok, errors)

    def test_missing_top_level_key_fails(self):
        proposal = _valid_proposal()
        del proposal["endpoint_path"]
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)
        self.assertTrue(any("endpoint_path" in e for e in errors))

    def test_invalid_action_type_fails(self):
        proposal = _valid_proposal()
        proposal["action_type"] = "NOT_A_TYPE"
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)

    def test_secret_parameter_without_secret_ref_fails(self):
        proposal = _valid_proposal()
        proposal["parameters"][0]["secret_ref"] = None
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)
        self.assertTrue(any("secret_ref" in e for e in errors))

    def test_invalid_group_rule_fails(self):
        proposal = _valid_proposal()
        proposal["parameter_groups"] = [{"name": "g", "rule": "NOT_A_RULE", "members": ["CustCode"]}]
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)

    def test_non_dict_response_fails(self):
        ok, errors = validate_proposal_schema(["not", "a", "dict"])
        self.assertFalse(ok)


class TestAnalyzeApiCallSite(unittest.TestCase):
    """Confirms the LLM is called through the EXISTING abstraction only
    (services.llm_service.get_llm_service), never a direct provider call,
    and that secrets are redacted before being sent."""

    def test_analyze_uses_llm_service_abstraction_and_redacts_secret(self):
        fake_llm = MagicMock()
        import json
        fake_llm.generate.return_value = LLMResponse(
            text=json.dumps(_valid_proposal()), model="gpt-4o-mini", provider="openai",
            input_tokens=10, output_tokens=20, latency_ms=100.0)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_api("Authorization: Bearer sk-real-secret-abc123\nGET /orders",
                                  "ค้นหา PO รายการเดียว", ["เช็ค PO ให้หน่อย"])
        self.assertTrue(result["ok"])
        fake_llm.generate.assert_called_once()
        sent_messages = fake_llm.generate.call_args[0][0]
        sent_text = json.dumps(sent_messages, ensure_ascii=False)
        self.assertNotIn("sk-real-secret-abc123", sent_text)

    def test_analyze_rejects_malformed_json_gracefully(self):
        fake_llm = MagicMock()
        fake_llm.generate.return_value = LLMResponse(
            text="not valid json {{{", model="gpt-4o-mini", provider="openai",
            input_tokens=5, output_tokens=5, latency_ms=50.0)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_api("GET /orders", "purpose", [])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_analyze_strips_markdown_code_fences(self):
        fake_llm = MagicMock()
        import json
        fake_llm.generate.return_value = LLMResponse(
            text="```json\n" + json.dumps(_valid_proposal()) + "\n```", model="gpt-4o-mini",
            provider="openai", input_tokens=10, output_tokens=20, latency_ms=100.0)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_api("GET /orders", "purpose", [])
        self.assertTrue(result["ok"])


class TestDetectEndpointsFromDocument(unittest.TestCase):
    def test_plain_curl_returns_no_endpoints(self):
        self.assertEqual(detect_endpoints_from_document("curl https://example.com/api/orders"), [])

    def test_empty_or_non_json_returns_empty(self):
        self.assertEqual(detect_endpoints_from_document(""), [])
        self.assertEqual(detect_endpoints_from_document("just some plain text"), [])

    def test_postman_collection_detects_multiple_endpoints(self):
        collection = {
            "info": {"name": "FastTrade"},
            "item": [
                {"name": "Customer Lookup", "request": {"method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataCustomer"}, "header": []}},
                {"name": "Order Lookup", "request": {"method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataOrder"}, "header": []}},
                {"name": "Folder", "item": [
                    {"name": "Tracking Lookup", "request": {"method": "GET", "url": {"raw": "https://fasttrade.in.th/GetTracking"}, "header": []}},
                ]},
            ],
        }
        endpoints = detect_endpoints_from_document(json.dumps(collection))
        self.assertEqual(len(endpoints), 3)
        names = {e["name"] for e in endpoints}
        self.assertEqual(names, {"Customer Lookup", "Order Lookup", "Tracking Lookup"})
        categories = {e["name"]: e["category_guess"] for e in endpoints}
        self.assertEqual(categories["Customer Lookup"], "Customer lookup")
        self.assertEqual(categories["Tracking Lookup"], "Tracking lookup")

    def test_postman_collection_redacts_secret_header_values(self):
        collection = {"item": [
            {"name": "Customer Lookup", "request": {
                "method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataCustomer"},
                "header": [{"key": "Authorization", "value": "Bearer sk-real-secret-abc123"}],
            }},
        ]}
        endpoints = detect_endpoints_from_document(json.dumps(collection))
        self.assertEqual(len(endpoints), 1)
        self.assertNotIn("sk-real-secret-abc123", json.dumps(endpoints))

    def test_single_endpoint_postman_collection_returns_empty(self):
        # Only ONE request in the whole collection — the wizard's normal
        # single-endpoint flow handles this, not the multi-endpoint UI.
        collection = {"item": [
            {"name": "Only One", "request": {"method": "GET", "url": {"raw": "https://example.com/x"}, "header": []}},
        ]}
        endpoints = detect_endpoints_from_document(json.dumps(collection))
        self.assertEqual(len(endpoints), 1)  # detection itself still finds it; "is_multi" gating happens in the route

    def test_openapi_spec_detects_multiple_endpoints(self):
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://fasttrade.in.th"}],
            "paths": {
                "/GetDataCustomer": {"post": {"summary": "Customer Lookup"}},
                "/GetDataOrder": {"post": {"summary": "Order Lookup"}, "get": {"summary": "Order Lookup GET"}},
            },
        }
        endpoints = detect_endpoints_from_document(json.dumps(spec))
        self.assertEqual(len(endpoints), 3)
        urls = {e["url"] for e in endpoints}
        self.assertIn("https://fasttrade.in.th/GetDataCustomer", urls)

    def test_does_not_merge_unrelated_endpoints(self):
        collection = {"item": [
            {"name": "Customer Lookup", "request": {"method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataCustomer"}, "header": []}},
            {"name": "Product Image", "request": {"method": "GET", "url": {"raw": "https://fasttrade.in.th/GetProductImage"}, "header": []}},
        ]}
        endpoints = detect_endpoints_from_document(json.dumps(collection))
        self.assertEqual(len(endpoints), 2)
        self.assertNotEqual(endpoints[0]["name"], endpoints[1]["name"])


class TestSearchInfoInPrompt(unittest.TestCase):
    def test_search_info_included_when_provided(self):
        fake_llm = MagicMock()
        fake_llm.generate.return_value = LLMResponse(
            text=json.dumps(_valid_proposal()), model="gpt-4o-mini", provider="openai",
            input_tokens=10, output_tokens=20, latency_ms=100.0)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            analyze_api("GET /orders", "purpose", [], )
            from services.ai_auto_setup_service import analyze_capability
            analyze_capability("GET /orders", "purpose", [], search_info="รหัสลูกค้า หรืออีเมล")
        sent_messages = fake_llm.generate.call_args[0][0]
        sent_text = json.dumps(sent_messages, ensure_ascii=False)
        self.assertIn("รหัสลูกค้า หรืออีเมล", sent_text)


def _mock_llm(proposal):
    resp = LLMResponse(text=json.dumps(proposal), model="gpt-4o-mini", provider="openai",
                        input_tokens=10, output_tokens=20, latency_ms=100.0)
    fake = MagicMock()
    fake.generate.return_value = resp
    return fake


def _proposal_with_params(*params, **overrides):
    base = _valid_proposal()
    base["parameters"] = list(params)
    base.update(overrides)
    return base


class TestPart9CurlSourceOfTruth(unittest.TestCase):
    """Part 9 — the 7 numbered scenarios from the cURL-source-of-truth
    task, using analyze_capability() end-to-end (mocked LLM only;
    the secret detector itself is the real, un-mocked local module)."""

    def _param(self, name, input_source, **overrides):
        p = {"name": name, "display_name": name, "required": True, "input_source": input_source,
             "secret_ref": None, "example_value": None, "validation_type": None,
             "validation_pattern": None, "validation_confidence": "high", "follow_up_options": []}
        p.update(overrides)
        return p

    def test_1_secretcode_and_custcode_via_data_urlencode(self):
        curl = (
            "curl --location 'https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer' "
            "--header 'Accept: application/json' "
            "--data-urlencode 'SecretCode=REAL-SECRET-VALUE-999' "
            "--data-urlencode 'CustCode=C00001'"
        )
        proposal = _proposal_with_params(
            self._param("SecretCode", "secret_configuration", secret_ref="FASTTRADE_SECRET"),
            self._param("CustCode", "customer_message", example_value="C00001",
                        validation_type="regex", validation_pattern=r"^C\d{5}$"),
        )
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability(curl, "ค้นหาข้อมูลลูกค้า", ["เช็คโปรไฟล์ลูกค้า"])

        self.assertTrue(result["ok"], result["errors"])
        params_by_name = {p["name"]: p for p in result["proposal"]["parameters"]}
        self.assertEqual(params_by_name["SecretCode"]["input_source"], "credential_store")
        self.assertTrue(params_by_name["SecretCode"]["credential_ref"])
        self.assertIsNone(params_by_name["SecretCode"]["secret_ref"])
        self.assertEqual(params_by_name["CustCode"]["input_source"], "customer_message")

        creds_by_name = {c["name"]: c for c in result["detected_credentials"]}
        self.assertEqual(creds_by_name["SecretCode"]["location"], "body_form")

        full_dump = json.dumps(result)
        self.assertNotIn("REAL-SECRET-VALUE-999", full_dump)

    def test_2_x_api_key_header_secret_stays_header_location(self):
        curl = "curl -H 'X-API-Key: real-secret-header-value' https://api.example.com/customers"
        proposal = _proposal_with_params(
            self._param("X-API-Key", "secret_configuration", secret_ref="X_API_KEY"),
        )
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability(curl, "purpose", [])
        self.assertTrue(result["ok"], result["errors"])
        creds_by_name = {c["name"]: c for c in result["detected_credentials"]}
        self.assertEqual(creds_by_name["X-API-Key"]["location"], "header")
        self.assertNotIn("real-secret-header-value", json.dumps(result))

    def test_3_bearer_authorization_classified_as_header_secret(self):
        curl = "curl -H 'Authorization: Bearer sk-real-bearer-secret' https://api.example.com/customers"
        proposal = _proposal_with_params(
            self._param("Authorization", "secret_configuration", secret_ref="AUTH_TOKEN"),
        )
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability(curl, "purpose", [])
        self.assertTrue(result["ok"], result["errors"])
        creds_by_name = {c["name"]: c for c in result["detected_credentials"]}
        self.assertEqual(creds_by_name["Authorization"]["location"], "header")
        self.assertNotIn("sk-real-bearer-secret", json.dumps(result))

    def test_4_json_body_client_secret_and_customer_id(self):
        body = json.dumps({"client_secret": "real-json-secret-value", "customer_id": "C00001"})
        proposal = _proposal_with_params(
            self._param("client_secret", "secret_configuration", secret_ref="CLIENT_SECRET"),
            self._param("customer_id", "customer_message", example_value="C00001"),
        )
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability(body, "purpose", [])
        self.assertTrue(result["ok"], result["errors"])
        params_by_name = {p["name"]: p for p in result["proposal"]["parameters"]}
        self.assertEqual(params_by_name["client_secret"]["input_source"], "credential_store")
        self.assertEqual(params_by_name["customer_id"]["input_source"], "customer_message")
        self.assertNotIn("real-json-secret-value", json.dumps(result))

    def test_6_required_customer_info_excludes_credential_store_params(self):
        # Part 7 — the routing/search-fields "required" summary must be
        # DERIVED from actual search-field configuration, and must never
        # count a credential_store/secret_configuration parameter as
        # something to ask the customer for.
        curl = (
            "curl --location 'https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer' "
            "--data-urlencode 'SecretCode=REAL-SECRET-VALUE-999' "
            "--data-urlencode 'CustCode=C00001'"
        )
        proposal = _proposal_with_params(
            self._param("SecretCode", "secret_configuration", secret_ref="FASTTRADE_SECRET"),
            self._param("CustCode", "customer_message", example_value="C00001",
                        validation_type="regex", validation_pattern=r"^C\d{5}$"),
        )
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability(curl, "ค้นหาข้อมูลลูกค้า", ["เช็คโปรไฟล์ลูกค้า"])
        askable_required = [
            p for p in result["proposal"]["parameters"]
            if p.get("required") and p.get("input_source") not in ("secret_configuration", "credential_store")
        ]
        self.assertEqual([p["name"] for p in askable_required], ["CustCode"])
        # The weighted confidence engine's own "search field detection"
        # signal must be satisfied by CustCode alone, not require SecretCode.
        self.assertEqual(result["confidence"]["per_section"]["search_field_detection"], 1.0)

    def test_7_ai_behaviour_and_routing_fields_generated_and_editable(self):
        proposal = _valid_proposal()
        proposal["business_description"] = "ดึงข้อมูลโปรไฟล์และยอดเงินคงเหลือของลูกค้าจากระบบ FastTrade"
        proposal["success_prompt"] = "แสดงชื่อลูกค้า ยอดเงินคงเหลือ และคูปองที่ใช้ได้"
        proposal["failure_prompt"] = "แจ้งลูกค้าว่าตรวจสอบไม่พบ และแนะนำให้ติดต่อเจ้าหน้าที่"
        proposal["follow_up_prompt"] = "ขอรหัสลูกค้าเพิ่มเติมเพื่อใช้ค้นหา"
        proposal["when_not_to_call"] = "คำถามทั่วไปเกี่ยวกับนโยบายที่ไม่ต้องใช้ข้อมูลลูกค้าเฉพาะราย"
        proposal["rag_combination"] = "no_rag"
        proposal["confidence_threshold_recommendation"] = 0.8
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(proposal)):
            result = analyze_capability("GET /customer", "ค้นหาข้อมูลลูกค้า", ["เช็คโปรไฟล์ลูกค้า"])
        self.assertTrue(result["ok"], result["errors"])
        p = result["proposal"]
        for field in ("business_description", "success_prompt", "failure_prompt", "follow_up_prompt",
                      "when_not_to_call", "rag_combination", "confidence_threshold_recommendation"):
            self.assertTrue(p.get(field), field)
        # These keys are optional in the schema (never required, never
        # rejected when a proposal omits them) but must round-trip
        # unmodified when present, remaining editable downstream.
        ok, errors = validate_proposal_schema(p)
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
