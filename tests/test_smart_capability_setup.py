"""Tests for Smart Capability Setup — unified type detection (API/RAG/
TOOL/HUMAN_HANDOFF/NOTIFICATION/WORKFLOW/WEBHOOK), medium/low confidence
handling, question expansion, and per-type Draft/Enable rules. The LLM
is always mocked here — never a real network/LLM call in this file."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.ai_auto_setup_service import (
    analyze_api, analyze_capability, expand_example_questions, validate_proposal_schema,
)
from services.llm_service import LLMResponse


def _base_proposal(**overrides):
    proposal = {
        "action_name": "Test", "display_name": "ทดสอบ", "action_id": "test_action",
        "description": "ทดสอบ purpose", "category": "test", "action_type": "TOOL",
        "http_method": None, "base_url": None, "endpoint_path": None, "content_type": None,
        "headers": {}, "parameters": [], "parameter_groups": [], "keywords": ["test"],
        "example_questions": ["test question"], "response_mapping": [],
        "customer_facing_response_template": "{result}",
    }
    proposal.update(overrides)
    return proposal


def _mock_llm(text):
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(text=text, model="gpt-4o-mini", provider="openai",
                                              input_tokens=10, output_tokens=20, latency_ms=100.0)
    return fake


class TestSmartTypeDetection(unittest.TestCase):
    def _analyze_with(self, proposal_overrides):
        import json
        proposal = _base_proposal(**proposal_overrides)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(json.dumps(proposal))):
            return analyze_capability("some input", "some purpose", [])

    def test_api_detection(self):
        result = self._analyze_with({"action_type": "API", "detected_action_type": "API",
                                      "detection_confidence": "high",
                                      "http_method": "POST", "base_url": "https://x.test", "endpoint_path": "/api"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detected_action_type"], "API")

    def test_webhook_detection(self):
        result = self._analyze_with({"action_type": "WEBHOOK", "detected_action_type": "WEBHOOK",
                                      "detection_confidence": "high"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detected_action_type"], "WEBHOOK")

    def test_rag_detection(self):
        result = self._analyze_with({"action_type": "RAG", "detected_action_type": "RAG",
                                      "detection_confidence": "high", "knowledge_scope": "คลังสินค้า"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detected_action_type"], "RAG")
        self.assertEqual(result["proposal"]["knowledge_scope"], "คลังสินค้า")

    def test_tool_detection(self):
        result = self._analyze_with({"action_type": "TOOL", "detected_action_type": "TOOL",
                                      "detection_confidence": "high", "tool_name": "calculator"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["tool_name"], "calculator")

    def test_human_handoff_detection(self):
        result = self._analyze_with({"action_type": "HUMAN_HANDOFF", "detected_action_type": "HUMAN_HANDOFF",
                                      "detection_confidence": "high", "triggers": ["ลูกค้าขอคุยกับเจ้าหน้าที่"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detected_action_type"], "HUMAN_HANDOFF")

    def test_notification_detection(self):
        result = self._analyze_with({"action_type": "NOTIFICATION", "detected_action_type": "NOTIFICATION",
                                      "detection_confidence": "high", "triggers": ["ลูกค้าร้องเรียน"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detected_action_type"], "NOTIFICATION")

    def test_workflow_detection(self):
        result = self._analyze_with({"action_type": "WORKFLOW", "detected_action_type": "WORKFLOW",
                                      "detection_confidence": "high",
                                      "workflow_steps": ["ตรวจสอบสถานะ", "แจ้งเจ้าหน้าที่"]})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["proposal"]["workflow_steps"]), 2)

    def test_backward_compatible_analyze_api_alias_still_works(self):
        import json
        proposal = _base_proposal(action_type="API", http_method="GET", base_url="https://x.test",
                                   endpoint_path="/y")
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(json.dumps(proposal))):
            result = analyze_api("GET /y", "purpose", [])
        self.assertTrue(result["ok"])


class TestConfidenceHandling(unittest.TestCase):
    def test_medium_confidence_includes_interpretations(self):
        import json
        proposal = _base_proposal(
            detection_confidence="medium",
            type_interpretations=[
                {"label": "เชื่อมต่อ API เพื่อค้นหาข้อมูลออเดอร์", "action_type": "API"},
                {"label": "สร้าง Webhook เพื่อรับข้อมูลออเดอร์", "action_type": "WEBHOOK"},
            ])
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(json.dumps(proposal))):
            result = analyze_capability("ambiguous input", "purpose", [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detection_confidence"], "medium")
        self.assertEqual(len(result["proposal"]["type_interpretations"]), 2)

    def test_low_confidence_includes_clarification_question(self):
        import json
        proposal = _base_proposal(detection_confidence="low",
                                   clarification_question="คุณต้องการเชื่อมต่อ API หรือค้นหาจากเอกสารครับ?")
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(json.dumps(proposal))):
            result = analyze_capability("very vague input", "purpose", [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal"]["detection_confidence"], "low")
        self.assertIn("คุณต้องการ", result["proposal"]["clarification_question"])

    def test_missing_confidence_defaults_to_high(self):
        import json
        proposal = _base_proposal()
        del proposal  # noqa - build fresh without confidence field explicitly
        proposal = _base_proposal()
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm(json.dumps(proposal))):
            result = analyze_capability("clear input", "purpose", [])
        self.assertEqual(result["proposal"]["detection_confidence"], "high")

    def test_invalid_detection_confidence_fails_schema(self):
        proposal = _base_proposal(detection_confidence="super_sure")
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)

    def test_invalid_detected_action_type_fails_schema(self):
        proposal = _base_proposal(detected_action_type="NOT_A_TYPE")
        ok, errors = validate_proposal_schema(proposal)
        self.assertFalse(ok)


class TestQuestionExpansion(unittest.TestCase):
    def test_expands_single_question_into_multiple(self):
        import json
        suggestions = ["ออเดอร์นี้อยู่สถานะอะไร", "PO นี้ส่งหรือยัง", "ขอเช็คสถานะคำสั่งซื้อ"]
        with patch("services.ai_auto_setup_service.get_llm_service",
                   return_value=_mock_llm(json.dumps(suggestions))):
            result = expand_example_questions("เช็ค PO", "ค้นหาคำสั่งซื้อ")
        self.assertIn("เช็ค PO", result)
        self.assertIn("ออเดอร์นี้อยู่สถานะอะไร", result)
        self.assertGreaterEqual(len(result), 4)

    def test_empty_seed_returns_empty(self):
        self.assertEqual(expand_example_questions(""), [])

    def test_malformed_ai_response_falls_back_to_seed_only(self):
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=_mock_llm("not json {{{")):
            result = expand_example_questions("เช็ค PO")
        self.assertEqual(result, ["เช็ค PO"])

    def test_deduplicates_suggestions(self):
        import json
        with patch("services.ai_auto_setup_service.get_llm_service",
                   return_value=_mock_llm(json.dumps(["เช็ค PO", "เช็ค PO", "อีกคำถาม"]))):
            result = expand_example_questions("เช็ค PO")
        self.assertEqual(result.count("เช็ค PO"), 1)


class TestPerTypeDraftEnableRules(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def test_rag_requires_knowledge_scope(self):
        action = self.reg.create({"action_key": "kb_search", "name": "KB Search", "action_type": "RAG"})
        check = self.reg.validate_can_enable(action["id"])
        self.assertFalse(check["ok"])
        self.assertIn("missing_knowledge_scope", check["reasons"])
        self.reg.update(action["id"], {"setup_metadata": {"knowledge_scope": "คลังสินค้า"}})
        check2 = self.reg.validate_can_enable(action["id"])
        self.assertTrue(check2["ok"])

    def test_tool_requires_tool_name(self):
        action = self.reg.create({"action_key": "a_tool", "name": "A Tool", "action_type": "TOOL"})
        check = self.reg.validate_can_enable(action["id"])
        self.assertIn("missing_tool_name", check["reasons"])
        self.reg.update(action["id"], {"setup_metadata": {"tool_name": "calculator"}})
        self.assertTrue(self.reg.validate_can_enable(action["id"])["ok"])

    def test_human_handoff_requires_trigger(self):
        action = self.reg.create({"action_key": "handoff", "name": "Handoff", "action_type": "HUMAN_HANDOFF"})
        check = self.reg.validate_can_enable(action["id"])
        self.assertIn("missing_trigger", check["reasons"])
        self.reg.update(action["id"], {"setup_metadata": {"triggers": ["ลูกค้าขอคุยกับเจ้าหน้าที่"]}})
        self.assertTrue(self.reg.validate_can_enable(action["id"])["ok"])

    def test_workflow_requires_step(self):
        action = self.reg.create({"action_key": "flow", "name": "Flow", "action_type": "WORKFLOW"})
        check = self.reg.validate_can_enable(action["id"])
        self.assertIn("missing_workflow_step", check["reasons"])
        self.reg.update(action["id"], {"setup_metadata": {"workflow_steps": ["step 1"]}})
        self.assertTrue(self.reg.validate_can_enable(action["id"])["ok"])

    def test_notification_requires_trigger(self):
        action = self.reg.create({"action_key": "notify", "name": "Notify", "action_type": "NOTIFICATION"})
        check = self.reg.validate_can_enable(action["id"])
        self.assertIn("missing_trigger", check["reasons"])


if __name__ == "__main__":
    unittest.main()
