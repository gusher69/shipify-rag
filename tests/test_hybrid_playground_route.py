"""Route-level tests for POST /admin/api/hybrid-playground/ask (Hybrid
Question Segmentation sprint, 2026-08-02) — uses the real FastAPI app
with a mocked Supabase-backed registry (_FakeSupabase, same convention
as tests/test_credential_routes.py) and mocks the two deep entry points
(run_playground_turn / run_erp_test) at their own module — never a real
DB/network/LLM call.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry


def _seed_action(reg, *, key, category=None, keywords=None, params=None):
    action = reg.create({
        "action_key": key, "name": key, "display_name": key, "action_type": "API",
        "category": category, "ai_description": "", "search_keywords": keywords or [],
        "enabled": True, "priority": 0,
    })
    if params:
        reg.replace_parameters(action["id"], params)
    reg.upsert_execution(action["id"], {"endpoint": "https://example.test/action", "http_method": "GET"})
    return action["id"]


def _fake_rag_result(answer="RAG answer", chunks=None, intent="policy_howto"):
    # NOTE: MagicMock(name=...) is reserved for the mock's own debug repr,
    # not a real ".name" attribute — must be set via .name = ... after
    # construction (a well-known unittest.mock gotcha).
    template_mock = MagicMock(id="t1", version="1", system_prompt="sys")
    template_mock.name = "T"
    return MagicMock(
        answer=answer, chunks=chunks or [],
        prompt=MagicMock(template=template_mock,
                          messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
                          final_prompt_text="final"),
        confidence=0.8, confidence_label="High", answerability="direct_answer",
        latency_ms=10.0, embedding_model="m", embedding_provider="p", embedding_dimensions=10,
        input_tokens=1, output_tokens=1, stages=[],
        model="gpt-4o", broad_intent="policy", actionable_intent=intent, intent_confidence=0.7, intent_entities={},
        temperature=0.3,
        policy=MagicMock(escalate=False, active_count=0, verdicts=[], notes=[]), policy_set_name="Standard",
    )


class TestHybridPlaygroundRoute(unittest.TestCase):
    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        self.client.post("/admin/login", data={"username": "admin", "password": "shipify2026"})
        self.fake_sb = _FakeSupabase()
        self.reg = BusinessActionRegistry(self.fake_sb)
        self.action_id = _seed_action(
            self.reg, key="get_customer_coupons", category="customer", keywords=["คูปอง", "ข้อมูลลูกค้า"],
            params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
                     "input_source": "customer_message", "validation_pattern": r"^C\d+$"}],
        )
        self.reg.set_parameter_groups(self.action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        self.patcher_sb = patch("admin.routes.get_sb", return_value=self.fake_sb)
        self.patcher_sb.start()

    def tearDown(self):
        self.patcher_sb.stop()

    def test_auto_mode_selects_hybrid_and_calls_each_path_exactly_once(self):
        erp_result = {"ok": True, "answer": "ลูกค้า C00001 มีคูปอง 2 ใบ", "trace": [], "summary": {},
                       "collected_params": {"CustCode": "C00001"}}
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ")) as mock_rag, \
             patch("services.erp_test_harness.run_erp_test", return_value=erp_result) as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "mode": "auto",
            })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["classification"]["classification"], "HYBRID")
        self.assertEqual(mock_rag.call_count, 1, "RAG must run exactly once")
        self.assertEqual(mock_erp.call_count, 1, "ERP must run exactly once")

        # Question Segmentation — neither path receives the full compound question.
        rag_question_sent = mock_rag.call_args.args[0]
        erp_question_sent = mock_erp.call_args.kwargs["message"]
        self.assertIn("คูปองใช้งานอย่างไร", rag_question_sent)
        self.assertNotIn("C00001", rag_question_sent)
        self.assertIn("C00001", erp_question_sent)
        self.assertNotIn("คูปองใช้งานอย่างไร", erp_question_sent)

        self.assertIn("ข้อมูลเฉพาะลูกค้า", data["hybrid"]["merged_answer"])
        self.assertIn("ความรู้ทั่วไป", data["hybrid"]["merged_answer"])

    def test_auto_mode_rag_only_question_never_calls_erp(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result()) as mock_rag, \
             patch("services.erp_test_harness.run_erp_test") as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ขอทราบนโยบายการคืนสินค้า", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "RAG_ONLY")
        self.assertEqual(mock_rag.call_count, 1)
        mock_erp.assert_not_called()
        self.assertIsNone(data["erp"])

    def test_auto_mode_erp_only_question_never_calls_rag(self):
        erp_result = {"ok": True, "answer": "ลูกค้า C00001 มีคูปอง 2 ใบ", "trace": [], "summary": {},
                       "collected_params": {"CustCode": "C00001"}}
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag, \
             patch("services.erp_test_harness.run_erp_test", return_value=erp_result) as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ข้อมูลลูกค้ารหัส C00001", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "ERP_ONLY")
        self.assertEqual(mock_erp.call_count, 1)
        mock_rag.assert_not_called()
        self.assertIsNone(data["rag"])

    def test_ambiguous_actions_produce_clarification_with_no_execution(self):
        _seed_action(self.reg, key="another_customer_action", category="customer", keywords=["ข้อมูลลูกค้า"])
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag, \
             patch("services.erp_test_harness.run_erp_test") as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ข้อมูลลูกค้า", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "CLARIFICATION_REQUIRED")
        self.assertIsNotNone(data["clarification"])
        self.assertEqual(len(data["clarification"]["candidate_action_ids"]), 2)
        mock_rag.assert_not_called()
        mock_erp.assert_not_called()

    def test_missing_erp_parameter_asks_clarification_no_execution_no_rag(self):
        # No CustCode-shaped value anywhere in the message -> the ERP
        # harness's OWN (unchanged) validation blocks execution and asks
        # for it; this is exercised for real, not mocked, since it's the
        # frozen erp_test_harness behavior being verified end-to-end here.
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "สวัสดีค่ะ ช่วยดูข้อมูลลูกค้าให้หน่อย", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "ERP_ONLY")
        self.assertIsNotNone(data["erp"])
        self.assertIsNotNone(data["erp"]["clarification_question"])
        mock_rag.assert_not_called()

    def test_one_erp_path_failure_still_returns_honest_rag_section(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ")), \
             patch("services.erp_test_harness.run_erp_test", side_effect=RuntimeError("erp harness crashed")):
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "mode": "hybrid",
                "action_id": self.action_id,
            })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("erp harness crashed", " ".join(data["errors"]))
        self.assertIn("ไม่สามารถดึงข้อมูลลูกค้าได้", data["hybrid"]["erp_contribution"])
        self.assertIn("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ", data["hybrid"]["rag_contribution"])

    def test_explicit_hybrid_mode_also_segments_the_question(self):
        erp_result = {"ok": True, "answer": "ลูกค้า C00001 มีคูปอง 2 ใบ", "trace": [], "summary": {},
                       "collected_params": {"CustCode": "C00001"}}
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ")) as mock_rag, \
             patch("services.erp_test_harness.run_erp_test", return_value=erp_result) as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "mode": "hybrid",
                "action_id": self.action_id,
            })
        data = resp.json()
        self.assertTrue(data["ok"])
        rag_question_sent = mock_rag.call_args.args[0]
        erp_question_sent = mock_erp.call_args.kwargs["message"]
        self.assertNotIn("C00001", rag_question_sent)
        self.assertNotIn("คูปองใช้งานอย่างไร", erp_question_sent)


if __name__ == "__main__":
    unittest.main()
