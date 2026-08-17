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
from tests.test_session_service import FakeSb
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

        # Real User Journey UAT (2026-08-15) -- Auto mode now persists a
        # real session/profile via services/session_service.py and
        # profiles/manager.py, backed by a SEPARATE fake (FakeSb, which
        # supports select/insert/update/upsert/order/limit/single) since
        # _FakeSupabase above only supports what the Business Action
        # Registry needs.
        self.pg_fake_sb = FakeSb()
        self.patcher_session_sb = patch("services.session_service._get_sb", return_value=self.pg_fake_sb)
        self.patcher_session_sb.start()
        self.patcher_profiles_sb = patch("profiles.manager.supabase", self.pg_fake_sb)
        self.patcher_profiles_sb.start()

    def tearDown(self):
        self.patcher_sb.stop()
        self.patcher_session_sb.stop()
        self.patcher_profiles_sb.stop()

    # Playground Production Parity (2026-08-15) — Auto mode now calls the
    # REAL services/decision_engine.py::DecisionEngine.decide() (the SAME
    # engine line_bot/webhook.py uses), not a separate classify_question()
    # -only heuristic. These tests therefore mock the TRUE boundaries
    # decide() itself calls through — services.playground_orchestrator.
    # run_playground_turn (RAG) and services.action_executor.requests.
    # request (the real ERP network boundary, never services.
    # erp_test_harness.run_erp_test, which decide() never touches) —
    # rather than the old harness-level mocks.
    def test_auto_mode_selects_hybrid_and_calls_each_path_exactly_once(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ")) as mock_rag, \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "mode": "auto",
            })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["classification"]["classification"], "HYBRID")
        self.assertEqual(mock_rag.call_count, 1, "RAG must run exactly once")
        self.assertEqual(mock_erp.call_count, 1, "ERP must run exactly once")
        self.assertTrue(data["production_trace"]["hybrid_used"])
        self.assertTrue(data["production_trace"]["rag_used"])
        self.assertTrue(data["production_trace"]["erp_used"])

        # Question Segmentation — neither path receives the full compound question.
        rag_question_sent = mock_rag.call_args.args[0]
        self.assertIn("คูปองใช้งานอย่างไร", rag_question_sent)
        self.assertNotIn("C00001", rag_question_sent)

        # Customer Response Quality (2026-08-16) — the customer-facing
        # merged_answer must read as one natural reply, never expose
        # internal architecture wording; the labeled view is still
        # available separately for Developer Mode.
        self.assertNotIn("ข้อมูลเฉพาะลูกค้า", data["hybrid"]["merged_answer"])
        self.assertNotIn("ความรู้ทั่วไป", data["hybrid"]["merged_answer"])
        self.assertIn("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ", data["hybrid"]["merged_answer"])
        self.assertIn("ข้อมูลเฉพาะลูกค้า", data["hybrid"]["labeled_answer"])
        self.assertIn("ความรู้ทั่วไป", data["hybrid"]["labeled_answer"])

    def test_auto_mode_rag_only_question_never_calls_erp(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result()) as mock_rag, \
             patch("services.action_executor.requests.request") as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ขอทราบนโยบายการคืนสินค้า", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "RAG_ONLY")
        self.assertEqual(mock_rag.call_count, 1)
        mock_erp.assert_not_called()
        self.assertIsNone(data["erp"])
        self.assertFalse(data["production_trace"]["erp_used"])

    def test_auto_mode_erp_only_question_never_calls_rag(self):
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag, \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ข้อมูลลูกค้ารหัส C00001", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "ERP_ONLY")
        self.assertEqual(mock_erp.call_count, 1)
        mock_rag.assert_not_called()
        self.assertIsNone(data["rag"])
        self.assertEqual(data["production_trace"]["selected_business_action"], "get_customer_coupons")
        self.assertEqual(data["production_trace"]["erp_http_status"], 200)

    def test_auto_mode_erp_only_reply_text_never_raw_json(self):
        """Customer-Facing Reply Leak Fix (2026-08-17) — reproduces the
        exact live bug: a successful, mapped_fields-bearing ERP-only Auto
        turn (mode == "auto", no RAG, no Hybrid) previously had no
        top-level field carrying the real, composed decide_result reply
        text at all — the frontend's chat bubble fell back to erp.answer,
        which is raw json.dumps(mapped_fields) (list-of-dicts with
        Python/JSON None/null scattered through it, exactly like the
        reported screenshot: '{"รายการคำสั่งซื้อทั้งหมด": [{"Code": ...,
        "DateConfirm": null, ...}]}'). reply_text must carry the real
        natural answer instead; erp.answer is left untouched for its own
        legitimate debug-panel use."""
        list_action_id = _seed_action(
            self.reg, key="search_data_order_list", category="order", keywords=["order", "PO"],
            params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                     "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"}],
        )
        self.reg.replace_response_mapping(list_action_id, [
            {"json_path": "$.orders", "mapped_label": "รายการคำสั่งซื้อทั้งหมด",
             "field_metadata": {"keywords": ["order", "คำสั่งซื้อ"]}},
        ])
        fake_orders = [
            {"Code": "POS100820260815001", "Status": "รอยืนยันรายการ", "Total": 1022,
             "DateConfirm": None, "DateProgress": None, "Tracking": []},
            {"Code": "POS100820260809001", "Status": "ยกเลิก", "Total": 0,
             "DateConfirm": "2026-08-09 16:18:04", "DateProgress": None, "Tracking": []},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"orders": fake_orders})):
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "PO ล่าสุดของ FT1008", "mode": "auto",
            })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("reply_text"), "mode=auto must always expose reply_text")
        # The exact leak shape reported live: raw JSON object/array syntax,
        # or a bare Python/JSON null, must never appear in the field the
        # frontend's chat bubble is told to prefer.
        self.assertNotIn('{"', data["reply_text"])
        self.assertNotIn("[{", data["reply_text"])
        self.assertNotIn("null", data["reply_text"])
        self.assertNotIn("None", data["reply_text"])
        # erp.answer (the debug-panel field) is untouched — still exists,
        # still raw, proving this is a NEW field addition, not a removal.
        self.assertIn('"Code"', data["erp"]["answer"])

    def test_ambiguous_actions_produce_clarification_with_no_execution(self):
        _seed_action(self.reg, key="another_customer_action", category="customer", keywords=["ข้อมูลลูกค้า"])
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag, \
             patch("services.action_executor.requests.request") as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "ข้อมูลลูกค้า", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "CLARIFICATION_REQUIRED")
        self.assertIsNotNone(data["clarification"])
        self.assertEqual(len(data["clarification"]["candidate_action_ids"]), 2)
        mock_rag.assert_not_called()
        mock_erp.assert_not_called()

    def test_missing_erp_parameter_asks_for_it_no_execution_no_rag(self):
        # No CustCode-shaped value anywhere in the message -> decide()'s
        # OWN (unchanged) dynamic-collection flow asks for it instead of
        # executing or falling back to "ไม่พบข้อมูลในฐานความรู้" — verified
        # for real against a real BusinessActionRegistry, never mocked.
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag, \
             patch("services.action_executor.requests.request") as mock_erp:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "สวัสดีค่ะ ช่วยดูข้อมูลลูกค้าให้หน่อย", "mode": "auto",
            })
        data = resp.json()
        self.assertEqual(data["classification"]["classification"], "ERP_ONLY")
        mock_rag.assert_not_called()
        mock_erp.assert_not_called()
        self.assertIsNone(data["rag"])
        self.assertIsNotNone(data["erp"]["clarification_question"])
        self.assertIn("รหัสลูกค้า", data["erp"]["clarification_question"])

    def test_auto_mode_production_trace_includes_customer_intelligence_fields(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result()) as mock_rag:
            resp = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "Shipify ให้บริการอะไรบ้าง", "mode": "auto",
            })
        data = resp.json()
        trace = data["production_trace"]
        self.assertEqual(trace["mode"], "auto (production Decision Engine)")
        self.assertIn(trace["customer_stage"], ("cold", "warm", "hot", "negative"))
        self.assertIn("handoff_recommended", trace)
        self.assertIn("latency_ms", trace)
        dumped = str(data)
        self.assertNotIn("SecretCode", dumped)
        self.assertNotIn("credential_ref", dumped)

    def test_auto_mode_conversation_history_persists_and_reaches_decision_engine(self):
        """Phase 4 — Context Continuity, via REAL session persistence
        (Real User Journey UAT, 2026-08-15) — turn 2 must see turn 1's
        message through the real ai_session_messages history, the SAME
        mechanism line_bot/webhook.py itself uses, not client-supplied
        JSON (which the route no longer even reads for Auto mode)."""
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("ยินดีให้บริการค่ะ")):
            resp1 = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "รหัสของผมคือ C00001", "mode": "auto", "journey_label": "TEST-CONTEXT",
            })
        self.assertTrue(resp1.json()["ok"])
        session_id_1 = resp1.json()["session_id"]
        self.assertIsNotNone(session_id_1)

        captured = {}

        def _capture(msg, **kw):
            captured["history"] = kw.get("history")
            return _fake_rag_result()

        with patch("services.playground_orchestrator.run_playground_turn", side_effect=_capture):
            resp2 = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "แล้วของส่งหรือยัง", "mode": "auto", "journey_label": "TEST-CONTEXT",
            })
        self.assertTrue(resp2.json()["ok"])
        self.assertEqual(resp2.json()["session_id"], session_id_1,
                          "same journey_label must reuse the same persisted session")
        contents = [h["content"] for h in (captured.get("history") or [])]
        self.assertTrue(any("C00001" in c for c in contents))

    def test_multi_user_isolation_profiles_and_histories_never_cross(self):
        """Phase 5 — two distinct journey_labels must never merge
        sessions/profiles, mirroring real per-LINE-user isolation."""
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("ok")):
            resp_a = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "รหัสของผมคือ C00001", "mode": "auto", "journey_label": "UAT-USER-A",
            })
            resp_b = self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "รหัสของผมคือ C00002", "mode": "auto", "journey_label": "UAT-USER-B",
            })
        self.assertNotEqual(resp_a.json()["session_id"], resp_b.json()["session_id"])
        self.assertNotEqual(resp_a.json()["production_trace"]["playground_user_id"],
                             resp_b.json()["production_trace"]["playground_user_id"])

        captured = {}
        with patch("services.playground_orchestrator.run_playground_turn",
                   side_effect=lambda msg, **kw: captured.setdefault("history", kw.get("history")) or _fake_rag_result()):
            self.client.post("/admin/api/hybrid-playground/ask", json={
                "question": "แล้วไงต่อ", "mode": "auto", "journey_label": "UAT-USER-A",
            })
        contents = [h["content"] for h in (captured.get("history") or [])]
        self.assertTrue(any("C00001" in c for c in contents))
        self.assertFalse(any("C00002" in c for c in contents))

    def test_same_journey_label_stable_playground_user_id_across_turns(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_rag_result("ok")):
            r1 = self.client.post("/admin/api/hybrid-playground/ask",
                                   json={"question": "hello", "mode": "auto", "journey_label": "UAT-STABLE"})
            r2 = self.client.post("/admin/api/hybrid-playground/ask",
                                   json={"question": "hello again", "mode": "auto", "journey_label": "UAT-STABLE"})
        self.assertEqual(r1.json()["production_trace"]["playground_user_id"],
                          r2.json()["production_trace"]["playground_user_id"])

    def test_human_handoff_duplicate_protection_within_same_session(self):
        """Journey 8 — a second handoff-triggering message within the
        SAME active session must not re-simulate a notification send."""
        r1 = self.client.post("/admin/api/hybrid-playground/ask", json={
            "question": "ขอติดต่อเจ้าหน้าที่", "mode": "auto", "journey_label": "UAT-HANDOFF-DEDUP",
        })
        self.assertEqual(r1.json()["production_trace"]["routing_type"], "HUMAN_HANDOFF")
        self.assertTrue(r1.json()["production_trace"]["handoff_notification"]["simulated_sent"])

        r2 = self.client.post("/admin/api/hybrid-playground/ask", json={
            "question": "ขอคุยกับเจ้าหน้าที่", "mode": "auto", "journey_label": "UAT-HANDOFF-DEDUP",
        })
        self.assertEqual(r2.json()["production_trace"]["routing_type"], "HUMAN_HANDOFF")
        self.assertFalse(r2.json()["production_trace"]["handoff_notification"]["simulated_sent"])

    def test_journey_label_names_the_persisted_session(self):
        resp = self.client.post("/admin/api/hybrid-playground/ask", json={
            "question": "Shipify ให้บริการอะไรบ้าง", "mode": "auto", "journey_label": "UAT-01-RAG",
        })
        session_id = resp.json()["session_id"]
        self.assertIsNotNone(session_id)
        saved = next(r for r in self.pg_fake_sb.store["ai_sessions"] if r["id"] == session_id)
        self.assertEqual(saved["name"], "UAT-01-RAG")
        self.assertEqual(saved["channel"], "playground")

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
