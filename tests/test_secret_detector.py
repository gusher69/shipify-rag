"""Tests for local, pre-LLM secret detection (services/secret_detector.py).
Never calls an LLM — this module is purely deterministic regex/parsing."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.secret_detector import detect_secrets, is_secret_name, redact_detected_secrets


class TestSecretNameRecognition(unittest.TestCase):
    def test_recognizes_common_secret_names(self):
        for name in ("SecretCode", "Authorization", "APIKey", "Api-Key", "X-API-Key", "Token",
                     "AccessToken", "Bearer", "Password", "ClientSecret", "private_key", "credential"):
            self.assertTrue(is_secret_name(name), name)

    def test_does_not_flag_ordinary_names(self):
        for name in ("CustCode", "OrderCode", "status", "customer_name"):
            self.assertFalse(is_secret_name(name), name)


class TestSemanticSecretNameMatching(unittest.TestCase):
    """Part 1 — semantic/case-insensitive substring matching, not a fixed
    exact-name list. All of these spellings must be recognized."""

    def test_recognizes_all_required_name_variants(self):
        for name in ("SecretCode", "secret_code", "CLIENT_SECRET", "ApiToken", "accessToken",
                     "auth_token", "passcode", "signature", "x-api-key", "private_key"):
            self.assertTrue(is_secret_name(name), name)


class TestCurlParsing(unittest.TestCase):
    def test_detects_header_secret_in_curl(self):
        curl = 'curl -X POST https://x.test/api -H "SecretCode: 16513268151" -H "Accept: application/json"'
        found = detect_secrets(curl)
        names = [f.name for f in found]
        self.assertIn("SecretCode", names)
        self.assertEqual(next(f for f in found if f.name == "SecretCode").value, "16513268151")

    def test_detects_bearer_token_and_strips_prefix(self):
        curl = 'curl -H "Authorization: Bearer sk-real-secret-abc123" https://x.test/api'
        found = detect_secrets(curl)
        auth = next(f for f in found if f.name == "Authorization")
        self.assertEqual(auth.value, "sk-real-secret-abc123")
        self.assertNotIn("Bearer", auth.value)

    def test_detects_form_field_secret(self):
        curl = 'curl -X POST https://x.test/api -d "SecretCode=16513268151&CustCode=C00001"'
        found = detect_secrets(curl)
        secret = next(f for f in found if f.name == "SecretCode")
        self.assertEqual(secret.value, "16513268151")
        self.assertEqual(secret.location, "body_form")
        self.assertFalse(any(f.name == "CustCode" for f in found))

    def test_detects_basic_auth(self):
        import base64
        b64 = base64.b64encode(b"user:supersecretpw").decode()
        curl = f'curl -H "Authorization: Basic {b64}" https://x.test/api'
        found = detect_secrets(curl)
        self.assertTrue(any(f.value == "supersecretpw" for f in found))


class TestBodyAndQueryDetection(unittest.TestCase):
    def test_detects_json_body_secret(self):
        body = json.dumps({"SecretCode": "abc999", "CustCode": "C00001"})
        found = detect_secrets(body)
        secret = next(f for f in found if f.name == "SecretCode")
        self.assertEqual(secret.value, "abc999")
        self.assertEqual(secret.location, "body_json")
        self.assertFalse(any(f.name == "CustCode" for f in found))

    def test_detects_query_string_secret(self):
        found = detect_secrets("https://api.example.com/orders?api_key=xyz123&status=open")
        secret = next(f for f in found if f.name == "api_key")
        self.assertEqual(secret.value, "xyz123")
        self.assertEqual(secret.location, "query_string")

    def test_detects_url_encoded_body(self):
        found = detect_secrets("SecretCode=16513268151&CustCode=C00001")
        self.assertTrue(any(f.name == "SecretCode" and f.value == "16513268151" for f in found))

    def test_detects_documentation_style_secret(self):
        found = detect_secrets("SecretCode: 16513268151\nCustCode: C00001")
        self.assertTrue(any(f.name == "SecretCode" and f.value == "16513268151" for f in found))
        self.assertFalse(any(f.name == "CustCode" for f in found))


class TestRedaction(unittest.TestCase):
    def test_redacts_before_llm_input_and_preserves_structure(self):
        raw = "SecretCode=16513268151&CustCode=C00001"
        found = detect_secrets(raw)
        redacted, log = redact_detected_secrets(raw, found)
        self.assertNotIn("16513268151", redacted)
        self.assertIn("CustCode=C00001", redacted)
        self.assertIn("[REDACTED_SECRET_1]", redacted)
        self.assertEqual(log[0]["name"], "SecretCode")
        self.assertNotIn("value", log[0])

    def test_redaction_log_never_contains_raw_value(self):
        raw = "Authorization: Bearer sk-real-secret-abc123"
        found = detect_secrets(raw)
        _, log = redact_detected_secrets(raw, found)
        self.assertNotIn("sk-real-secret-abc123", json.dumps(log))

    def test_multiple_secrets_each_get_own_placeholder(self):
        raw = "SecretCode=aaa&ClientSecret=bbb"
        found = detect_secrets(raw)
        redacted, log = redact_detected_secrets(raw, found)
        self.assertNotIn("aaa", redacted)
        self.assertNotIn("bbb", redacted)
        self.assertEqual(len(log), 2)

    def test_empty_input_returns_no_secrets(self):
        self.assertEqual(detect_secrets(""), [])


if __name__ == "__main__":
    unittest.main()
