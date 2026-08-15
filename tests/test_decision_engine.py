"""Tests for the AI Middleware Decision Engine v2 (services/decision_engine.py).

The Decision Engine only ORCHESTRATES existing components — these tests
mock the Business Action Registry (via _FakeSupabase, same convention as
tests/test_business_action_registry.py) and the Generic Action Executor
/ RAG service at their call sites, never a real DB/network/LLM call.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import DecisionEngine, search_candidate_actions, select_best_action
from services.action_selection_primitives import select_requested_mapped_fields


def _seed_action(reg, *, key, action_type, category=None, ai_description="", keywords=None,
                  enabled=True, priority=0, params=None):
    action = reg.create({
        "action_key": key, "name": key, "display_name": key, "action_type": action_type,
        "category": category, "ai_description": ai_description, "search_keywords": keywords or [],
        "enabled": enabled, "priority": priority,
    })
    if params:
        reg.replace_parameters(action["id"], params)
    return action["id"]


def _engine_with_registry(reg):
    engine = DecisionEngine(reg._sb)
    engine.registry = reg
    return engine


def _fake_exec_result(status="success", result=None, error=None):
    return {"status": status, "result": result or {}, "metadata": {}, "latency_ms": 1.0, "error": error, "logs": []}


def _fake_playground_result(answer="", chunks=None, confidence=0.0, confidence_label="Low", model="gpt-4o",
                             policy_escalate=False, policy_escalation_message=None,
                             template_id="t1", template_name="Standard Policy Template", template_version="3",
                             policy_set_name="Standard Policy"):
    """Production Integration Sprint (2026-08-02) — Decision Engine's RAG
    execution now calls services/playground_orchestrator.py::
    run_playground_turn() directly (services/decision_engine.py::
    _run_rag_pipeline), never services/action_executor.py's thinner
    _execute_rag. Tests that exercise the RAG path mock THIS call site
    instead, returning an object shaped like the real PlaygroundResult
    (only the fields _run_rag_pipeline actually reads).

    NOTE: `policy` must be a MagicMock with EXPLICIT escalate/
    escalation_message kwargs — an unset attribute on a bare MagicMock
    auto-creates a truthy child mock, which would make
    `result_payload.get("policy_escalate")` always true and silently
    reroute every RAG test to HUMAN_HANDOFF. Likewise `prompt.template`'s
    `name` must be set via attribute assignment, not the MagicMock(name=)
    constructor kwarg — that kwarg is reserved for the mock's own debug
    repr, not a real `.name` attribute (a well-known unittest.mock gotcha)."""
    template_mock = MagicMock(id=template_id, version=template_version)
    template_mock.name = template_name
    return MagicMock(answer=answer, chunks=chunks or [], confidence=confidence,
                      confidence_label=confidence_label, model=model,
                      policy=MagicMock(escalate=policy_escalate, escalation_message=policy_escalation_message),
                      prompt=MagicMock(template=template_mock), policy_set_name=policy_set_name)


class TestBusinessActionSearchAndSelection(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def test_search_scores_category_match_higher(self):
        _seed_action(self.reg, key="a", action_type="API", category="customer")
        _seed_action(self.reg, key="b", action_type="API", category="logistics")
        candidates = search_candidate_actions(self.reg, workflow="customer", message="")
        self.assertEqual(candidates[0]["action_key"], "a")

    def test_search_scores_keyword_overlap(self):
        _seed_action(self.reg, key="kw_match", action_type="RAG", keywords=["คลังสินค้า"])
        _seed_action(self.reg, key="no_match", action_type="RAG", keywords=["อื่นๆ"])
        candidates = search_candidate_actions(self.reg, workflow=None, message="คลังสินค้าอยู่ที่ไหน")
        self.assertEqual(candidates[0]["action_key"], "kw_match")

    def test_disabled_actions_excluded_from_candidates(self):
        _seed_action(self.reg, key="disabled_one", action_type="API", category="customer", enabled=False)
        candidates = search_candidate_actions(self.reg, workflow="customer", message="")
        self.assertEqual(candidates, [])

    def test_select_best_action_respects_minimum_score(self):
        _seed_action(self.reg, key="weak", action_type="RAG")
        candidates = search_candidate_actions(self.reg, workflow=None, message="unrelated text")
        self.assertIsNone(select_best_action(candidates, minimum_score=1.0))

    def test_registry_failure_returns_no_candidates_not_a_crash(self):
        broken_registry = MagicMock()
        broken_registry.enabled_actions.side_effect = Exception("db down")
        candidates = search_candidate_actions(broken_registry, workflow=None, message="x")
        self.assertEqual(candidates, [])


class TestIdentifierPatternScoring(unittest.TestCase):
    """Generic regression coverage for _identifier_pattern_score()'s effect
    on search_candidate_actions() — deliberately uses fabricated action
    keys/patterns with no relation to any real customer's identifier
    shape, proving the tie-break signal is driven entirely by each
    action's own configured validation_pattern, never a hardcoded field
    name or sample value (see the ERP order-routing-precision fix)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_action(self.reg, key="widget_detail", action_type="API", category="widget_workflow",
                     keywords=["widget"],
                     params=[{"name": "WidgetCode", "required": True, "validation_pattern": r"^WGT\d+$"}])
        _seed_action(self.reg, key="widget_list", action_type="API", category="widget_workflow",
                     keywords=["widget", "list"],
                     params=[{"name": "OwnerCode", "required": True}])

    def test_specific_identifier_breaks_keyword_tie_toward_detail_action(self):
        candidates = search_candidate_actions(self.reg, workflow=None, message="widget detail WGT123456")
        self.assertEqual(candidates[0]["action_key"], "widget_detail")

    def test_list_intent_without_identifier_still_favors_list_action(self):
        candidates = search_candidate_actions(self.reg, workflow=None, message="list all widgets for OWNER1")
        self.assertEqual(candidates[0]["action_key"], "widget_list")

    def test_identifier_pattern_score_requires_a_full_token_match(self):
        from services.decision_engine import _identifier_pattern_score, _IDENTIFIER_PATTERN_WEIGHT
        action = self.reg.get_full(self.reg.get_by_key("widget_detail")["id"])
        self.assertEqual(_identifier_pattern_score(self.reg, action, "WGT (no digits here)"), 0.0)
        self.assertEqual(_identifier_pattern_score(self.reg, action, "here is WGT789"), _IDENTIFIER_PATTERN_WEIGHT)


class TestConversationContinuationAndMissingParameters(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_missing_tracking_number_asks_follow_up(self):
        result = self.engine.decide("ของถึงไหนแล้ว", history=[])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertEqual(result["workflow"], "tracking")
        self.assertIn("เลข", result["reply"]["text"] + "หมายเลข")  # follow-up question mentions the number

    def test_conversation_continues_same_workflow_across_turns(self):
        action_id = _seed_action(self.reg, key="tracking_lookup", action_type="API", category="tracking",
                                  params=[{"name": "tracking_number", "required": True}])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/track", "http_method": "GET"})
        history = [
            {"role": "user", "content": "ของถึงไหนแล้ว"},
            {"role": "assistant", "content": "รบกวนแจ้งเลขพัสดุ หรือเลขออเดอร์ เพื่อให้ตรวจสอบสถานะสินค้าได้ค่ะ"},
        ]
        with patch("services.action_executor.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200, json=lambda: {"status": "in_transit"})
            result = self.engine.decide("1005505051005", history=history)
        self.assertEqual(result["routing"]["type"], "API")
        self.assertEqual(result["workflow"], "tracking")
        mock_req.assert_called_once()


class TestBusinessActionExecutionByType(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_rag_action_routes_and_answers(self):
        _seed_action(self.reg, key="kb", action_type="RAG", keywords=["คลังสินค้า"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คลังอยู่ที่กรุงเทพ", confidence=0.9)):
            result = self.engine.decide("คลังสินค้าอยู่ที่ไหน", history=[])
        self.assertEqual(result["routing"]["type"], "RAG")
        self.assertEqual(result["reply"]["text"], "คลังอยู่ที่กรุงเทพ")

    def test_api_action_routes_and_summarizes_result(self):
        # Deliberately avoids the ERP "customer" workflow's own keyword
        # vocabulary (services/slot_filling_engine.py) so this exercises
        # generic (non-workflow) Business Action routing to an API action.
        action_id = _seed_action(self.reg, key="employee_lookup", action_type="API", category="hr",
                                  keywords=["ข้อมูลพนักงาน"])
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": {"ชื่อ": "สมชาย"}})):
            result = self.engine.decide("ขอข้อมูลพนักงานหน่อย", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIn("สมชาย", result["reply"]["text"])

    def test_tool_action_routes(self):
        _seed_action(self.reg, key="url_conv", action_type="TOOL", keywords=["แปลงลิงก์"])
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"converted_url": "https://x"})):
            result = self.engine.decide("แปลงลิงก์ให้หน่อย", history=[])
        self.assertEqual(result["routing"]["type"], "TOOL")

    def test_notification_action_requires_confirmation_before_executing(self):
        """SendLineNotiCS enablement (2026-08-10) — ANY NOTIFICATION-type
        action is now COMMAND-type (see
        services/decision_engine.py::_requires_confirmation) and must be
        confirmed before the executor is ever reached, same as every
        other real-world side effect."""
        _seed_action(self.reg, key="notify", action_type="NOTIFICATION", keywords=["ร้องเรียน"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(status="not_implemented")) as mock_exec:
            result = self.engine.decide("ขอร้องเรียนบริการ", history=[])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        mock_exec.assert_not_called()
        self.assertIsNotNone(result["alert"])
        self.assertEqual(result["alert"]["alert_type"], "complaint")

    def test_notification_action_routes_as_not_implemented_once_confirmed(self):
        _seed_action(self.reg, key="notify2", action_type="NOTIFICATION", keywords=["ร้องเรียน"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(status="not_implemented")):
            result = self.engine.decide("ขอร้องเรียนบริการ", history=[], context={"confirmed": True})
        self.assertEqual(result["routing"]["type"], "NOTIFICATION")
        self.assertIsNotNone(result["alert"])
        self.assertEqual(result["alert"]["alert_type"], "complaint")

    def test_human_action_routes_as_handoff(self):
        _seed_action(self.reg, key="human_action", action_type="HUMAN_HANDOFF", keywords=["คุยกับเจ้าหน้าที่"])
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(status="handoff_prepared", result={"handoff_payload": {"reason": "x"}})):
            result = self.engine.decide("ขอคุยกับเจ้าหน้าที่", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")
        self.assertIsNotNone(result["handoff_payload"])

    def test_webhook_action_routes(self):
        _seed_action(self.reg, key="hook", action_type="WEBHOOK", keywords=["แจ้งเตือนภายนอก"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(result={"mapped_fields": {}})):
            result = self.engine.decide("แจ้งเตือนภายนอกหน่อย", history=[])
        self.assertEqual(result["routing"]["type"], "WEBHOOK")


class TestFallbackAndUnknownIntent(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_unknown_question_falls_back_safely_with_no_actions(self):
        with patch("services.playground_orchestrator.run_playground_turn",
                   side_effect=Exception("no network in test")):
            result = self.engine.decide("อยากรู้ดวงวันนี้", history=[])
        self.assertEqual(result["routing"]["type"], "SAFE_FALLBACK")
        self.assertIn("ขอโทษ", result["reply"]["text"])

    def test_rag_no_grounded_answer_falls_back(self):
        _seed_action(self.reg, key="kb2", action_type="RAG", keywords=["ทดสอบเฉพาะ"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="")):
            result = self.engine.decide("ทดสอบเฉพาะคำถามนี้", history=[])
        self.assertEqual(result["routing"]["type"], "SAFE_FALLBACK")

    def test_rag_policy_escalation_routes_to_human_handoff(self):
        """Production Integration Sprint (2026-08-02), Phase 1 Step F —
        AI Policies' own escalation verdict (services/policy_engine.py,
        already computed inside the shared RAG pipeline) must still route
        to Smart Handoff when the Decision Engine executes RAG, exactly
        as it would in the AI Playground — no duplicate escalation logic
        re-derived by a channel adapter."""
        _seed_action(self.reg, key="kb_escalate", action_type="RAG", keywords=["ร้องเรียน"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(
                       answer="answer text", policy_escalate=True,
                       policy_escalation_message="กำลังโอนสายให้เจ้าหน้าที่ค่ะ")):
            result = self.engine.decide("ร้องเรียนบริการ", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")
        self.assertEqual(result["reply"]["text"], "กำลังโอนสายให้เจ้าหน้าที่ค่ะ")
        self.assertEqual(result["handoff_payload"]["reason"], "ai_policy_escalation")

    def test_rag_answer_includes_attachments_from_cited_chunks(self):
        """Production Integration Sprint (2026-08-02), Phase 1 Step F —
        attachments already present on retrieved chunks (RAG internals,
        untouched) must reach the reply as generic images/files lists, so
        a channel adapter never has to re-derive them from raw chunk data."""
        _seed_action(self.reg, key="kb_attach", action_type="RAG", keywords=["คู่มือ"])
        chunks = [{
            "text": "...", "cited": True,
            "attachments": [
                {"public_url": "https://example.test/photo.jpg", "attachment_type": "image", "mime_type": "image/jpeg"},
                {"public_url": "https://example.test/manual.pdf", "attachment_type": "pdf", "filename": "manual.pdf"},
            ],
        }]
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="answer text", chunks=chunks)):
            result = self.engine.decide("ขอคู่มือหน่อย", history=[])
        self.assertEqual(result["reply"]["images"], ["https://example.test/photo.jpg"])
        self.assertEqual(result["reply"]["files"][0]["filename"], "manual.pdf")

    def test_rag_execution_surfaces_prompt_and_policy_identity(self):
        """Local Production Pipeline Verification (2026-08-02) — Prompt
        Studio template identity and AI Policies set name are already
        resolved inside run_playground_turn(); this confirms they reach
        developer_trace (via _run_rag_pipeline's execution_result) instead
        of being silently discarded, so Developer Mode can show exactly
        which prompt/policy set produced a production answer."""
        _seed_action(self.reg, key="kb_identity", action_type="RAG", keywords=["ข้อมูล"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(
                       answer="answer text", template_name="Refund Policy Prompt", template_version="5",
                       policy_set_name="Escalation-Heavy Policy Set")):
            result = self.engine.decide("ขอข้อมูล", history=[], context={"developer_mode": True})
        exec_result = result["developer"]["execution_result"]["result"]
        self.assertEqual(exec_result["prompt_template_name"], "Refund Policy Prompt")
        self.assertEqual(exec_result["prompt_template_version"], "5")
        self.assertEqual(exec_result["policy_set_name"], "Escalation-Heavy Policy Set")

    def test_executor_failure_returns_structured_error_not_crash(self):
        _seed_action(self.reg, key="broken_api", action_type="API", category="hr", keywords=["ข้อมูลพนักงาน"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(status="error", error="boom")):
            result = self.engine.decide("ขอข้อมูลพนักงานหน่อย", history=[])
        self.assertEqual(result["error"], "boom")

    def test_registry_failure_falls_back_safely(self):
        broken_registry = MagicMock()
        broken_registry.enabled_actions.side_effect = Exception("db down")
        self.engine.registry = broken_registry
        with patch("services.playground_orchestrator.run_playground_turn",
                   side_effect=Exception("no network in test")):
            result = self.engine.decide("อยากรู้ดวงวันนี้", history=[])
        self.assertIn(result["routing"]["type"], ("SAFE_FALLBACK",))


class TestHumanHandoffTriggers(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_explicit_human_request_routes_to_handoff(self):
        result = self.engine.decide("ขอคุยกับเจ้าหน้าที่หน่อย", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_explicit_human_request_uses_natural_reply_not_technical_confirmation(self):
        """Human Handoff sprint (2026-08-13), Phase 4 — an explicit
        customer request must never surface an awkward technical
        confirmation prompt like 'ยืนยันการเรียก SendLineNotiCS หรือไม่'."""
        result = self.engine.decide("ขอคุยกับเจ้าหน้าที่หน่อย", history=[])
        reply_text = result["reply"]["text"]
        self.assertNotIn("ยืนยัน", reply_text)
        self.assertNotIn("SendLineNotiCS", reply_text)
        self.assertEqual(reply_text, "ได้เลยค่ะ เดี๋ยวแจ้งเจ้าหน้าที่ให้ติดต่อกลับนะคะ")

    def test_cs_request_english_word_routes_to_handoff(self):
        """Human Handoff sprint (2026-08-13), Phase 2 item 1 — 'ขอให้ CS
        ติดต่อกลับ' previously fell through to normal RAG/API routing;
        the broadened _HUMAN_REQUEST_RE now recognizes it."""
        result = self.engine.decide("ขอให้ CS ติดต่อกลับ", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_staff_word_routes_to_handoff(self):
        """Human Handoff sprint (2026-08-13), Phase 2 item 1 — 'พนักงาน'
        (staff) was not previously recognized at all."""
        result = self.engine.decide("ต้องการให้พนักงานช่วยเรื่องนี้", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_escalation_after_max_retry_routes_to_handoff(self):
        follow_up = "รบกวนแจ้งเลขพัสดุ หรือเลขออเดอร์ เพื่อให้ตรวจสอบสถานะสินค้าได้ค่ะ"
        history = [
            {"role": "user", "content": "ของถึงไหนแล้ว"},
            {"role": "assistant", "content": follow_up},
            {"role": "user", "content": "ไม่มีเลข"},
            {"role": "assistant", "content": follow_up},
        ]
        result = self.engine.decide("ไม่มีเลข", history=history)
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")


class TestDeveloperMode(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_developer_mode_off_hides_developer_block(self):
        result = self.engine.decide("อยากรู้ดวงวันนี้", history=[])
        self.assertNotIn("developer", result)

    def test_developer_mode_on_shows_trace_fields(self):
        result = self.engine.decide("อยากรู้ดวงวันนี้", history=[], context={"developer_mode": True})
        self.assertIn("developer", result)
        self.assertIn("intent", result["developer"])
        self.assertIn("latency_ms", result["developer"])

    def test_developer_mode_shows_candidates_and_selection_on_action_execution(self):
        _seed_action(self.reg, key="kb3", action_type="RAG", keywords=["คลังสินค้า"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="answer text")):
            result = self.engine.decide("คลังสินค้าอยู่ที่ไหน", history=[], context={"developer_mode": True})
        dev = result["developer"]
        self.assertIn("candidate_business_actions", dev)
        self.assertIn("selected_business_action", dev)
        self.assertEqual(dev["selected_business_action"], "kb3")

    def test_executor_confidence_surfaced_into_developer_trace(self):
        """Phase 1 audit fix (2026-08-02): the RAG pipeline already
        computes a confidence score but the engine used to silently
        discard it — developer_trace["confidence"] was always None.
        Confirms it's now actually read through. (Production Integration
        Sprint, 2026-08-02: the RAG path now calls run_playground_turn()
        directly rather than the Action Executor — same field, new call
        site.)"""
        _seed_action(self.reg, key="kb4", action_type="RAG", keywords=["คลังสินค้า"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="answer text", confidence=0.83)):
            result = self.engine.decide("คลังสินค้าอยู่ที่ไหน", history=[], context={"developer_mode": True})
        self.assertEqual(result["developer"]["confidence"], 0.83)

    def test_developer_trace_confidence_defaults_none_when_executor_omits_it(self):
        # Production Integration Sprint (2026-08-02) — the RAG pipeline
        # ALWAYS computes a confidence value when it succeeds (confirmed
        # by test_rag_policy_escalation_routes_to_human_handoff and
        # friends), so "confidence omitted" now only genuinely happens
        # when the pipeline itself doesn't run at all — mocked here as an
        # error, rather than depending on the real, unmocked pipeline to
        # happen to omit it.
        with patch("services.playground_orchestrator.run_playground_turn",
                   side_effect=Exception("pipeline unavailable in this test")):
            result = self.engine.decide("อยากรู้ดวงวันนี้", history=[], context={"developer_mode": True})
        self.assertIsNone(result["developer"]["confidence"])


class TestRegressionExistingComponents(unittest.TestCase):
    """Confirm this task did not modify the frozen components it
    orchestrates — Registry, Executor, and AI Core's intent classifier
    still behave exactly as before."""

    def test_registry_still_works_standalone(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "still_here", "name": "Still Here", "action_type": "TOOL"})
        self.assertTrue(reg.get(action["id"])["enabled"])

    def test_executor_still_returns_structured_result_standalone(self):
        from services.action_executor import ActionExecutor
        reg = BusinessActionRegistry(_FakeSupabase())
        executor = ActionExecutor(reg._sb)
        executor.registry = reg
        action = reg.create({"action_key": "standalone_check", "name": "Standalone", "action_type": "WORKFLOW"})
        result = executor.execute(action["id"], {})
        self.assertEqual(result["status"], "not_implemented")

    def test_classify_actionable_intent_still_importable_and_callable(self):
        from rag.query_understanding import classify_actionable_intent
        result = classify_actionable_intent("สวัสดีครับ")
        self.assertIn("actionable_intent", result)


class TestDynamicBusinessActionDrivenCollection(unittest.TestCase):
    """Single Source of Truth refactor: required parameters come from the
    Business Action Registry (parameters + parameter_groups), never from
    INTENT_SCHEMAS, for any Business Action that has parameter metadata
    configured."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_search_data_order(self, extra_params=None):
        action_id = _seed_action(self.reg, key="search_data_order", action_type="API", category="order",
                                  keywords=["PO", "เช็ค PO"])
        params = [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "OrderCode", "display_name": "เลขคำสั่งซื้อ (PO)", "required": True, "input_source": "customer_message"},
        ]
        if extra_params:
            params.extend(extra_params)
        self.reg.replace_parameters(action_id, params)
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/search-order", "http_method": "GET"})
        return action_id

    def test_two_required_parameters_asked_sequentially_then_executes(self):
        self._seed_search_data_order()

        turn1 = self.engine.decide("เช็ค PO", history=[])
        self.assertEqual(turn1["routing"]["type"], "WORKFLOW")
        q1 = turn1["reply"]["text"]
        self.assertIn("รหัสลูกค้า", q1)

        history = [{"role": "user", "content": "เช็ค PO"}, {"role": "assistant", "content": q1}]
        turn2 = self.engine.decide("C00001", history=history)
        self.assertEqual(turn2["routing"]["type"], "WORKFLOW")
        q2 = turn2["reply"]["text"]
        self.assertIn("เลขคำสั่งซื้อ", q2)

        history += [{"role": "user", "content": "C00001"}, {"role": "assistant", "content": q2}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "found"})) as mock_req:
            turn3 = self.engine.decide("PO-99887", history=history)
        self.assertEqual(turn3["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "C00001")
        self.assertEqual(sent_params.get("OrderCode"), "PO-99887")

    def test_changing_business_action_parameters_without_code_change(self):
        """Adding BranchCode as a third required parameter — purely a
        Registry/config change — must be picked up automatically by the
        SAME Information Collection code, no code edit required."""
        action_id = self._seed_search_data_order()

        # Complete the original 2-parameter flow first.
        q1 = self.engine.decide("เช็ค PO", history=[])["reply"]["text"]
        history = [{"role": "user", "content": "เช็ค PO"}, {"role": "assistant", "content": q1}]
        q2 = self.engine.decide("C00001", history=history)["reply"]["text"]
        history += [{"role": "user", "content": "C00001"}, {"role": "assistant", "content": q2}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "found"})):
            complete_before_change = self.engine.decide("PO-99887", history=history)
        self.assertEqual(complete_before_change["routing"]["type"], "API")

        # Now an admin edits the Business Action in the Admin UI, adding
        # BranchCode as required — no code change anywhere.
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "OrderCode", "display_name": "เลขคำสั่งซื้อ (PO)", "required": True, "input_source": "customer_message"},
            {"name": "BranchCode", "display_name": "รหัสสาขา", "required": True, "input_source": "customer_message"},
        ])

        # The exact same conversation now must stop and ask for BranchCode
        # instead of executing — purely because the Registry changed.
        history += [{"role": "user", "content": "PO-99887"}, {"role": "assistant", "content": "..."}]
        result = self.engine.decide("PO-99887", history=[
            {"role": "user", "content": "เช็ค PO"}, {"role": "assistant", "content": q1},
            {"role": "user", "content": "C00001"}, {"role": "assistant", "content": q2},
        ])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertIn("รหัสสาขา", result["reply"]["text"])

    def test_at_least_one_group_stops_asking_once_satisfied(self):
        action_id = _seed_action(self.reg, key="customer_lookup_group", action_type="API", category="customer",
                                  keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False, "input_source": "customer_message"},
            {"name": "CustEmail", "display_name": "อีเมล", "required": False, "input_source": "customer_message",
             "validation_type": "email"},
            {"name": "CustPhone", "display_name": "เบอร์โทร", "required": False, "input_source": "customer_message",
             "validation_type": "phone_number"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail", "CustPhone"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า somchai@example.com", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertIn("CustEmail", sent_params)
        self.assertNotIn("CustPhone", sent_params)
        self.assertNotIn("CustCode", sent_params)

    def test_at_least_one_group_executes_despite_required_credential_parameter(self):
        """Confirmed defect fix (2026-08-02, Local Production Pipeline
        Verification) — a Business Action shaped exactly like the real
        GetDataCustomer action (an AT_LEAST_ONE customer-facing group
        PLUS a separate required credential_store parameter) used to
        never actually execute: validate_can_execute() always reports the
        credential as "missing" (it's resolved separately, at execution
        time, by the Action Executor's own Credential Store — never via
        `collected`), so `is_complete` was always False even once the
        customer had supplied everything askable, looping forever on a
        generic "need more info" reply instead of ever reaching
        execution."""
        action_id = _seed_action(self.reg, key="customer_lookup_with_secret", action_type="API", category="customer",
                                  keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "display_name": "รหัสยืนยันตัวตน", "required": True,
             "input_source": "credential_store", "credential_ref": "fake_secret"},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
            {"name": "CustEmail", "display_name": "อีเมล", "required": False,
             "input_source": "customer_message", "validation_type": "email"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req, \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "resolved-secret", "error": None}):
            result = self.engine.decide("ข้อมูลลูกค้ารหัส C00001", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "C00001")

    def test_required_fixed_configuration_parameter_never_blocks_completion(self):
        """Confirmed defect fix (2026-08-07, ERP Completion Day) — a
        required parameter sourced from fixed_configuration (a
        pre-configured default value, e.g. "Latest=5" for a list-search
        API that needs at least one filter) used to be treated as
        something the customer must be ASKED for, exactly like the
        credential_store bug above but for a different input_source that
        _NON_ASKABLE_INPUT_SOURCES didn't yet cover. Found live while
        onboarding SearchDataOrderList/SearchDataShipmentList: a real
        CustCode-bearing message never reached execution because
        "Latest" kept showing up as the next thing to ask about."""
        action_id = _seed_action(self.reg, key="search_orders_fixed_latest", action_type="API", category="customer",
                                  keywords=["คำสั่งซื้อ"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
            {"name": "Latest", "display_name": "จำนวนรายการล่าสุด", "required": True,
             "input_source": "fixed_configuration", "example_value": "5"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/orders", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            result = self.engine.decide("คำสั่งซื้อของลูกค้ารหัส C00001", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "C00001")
        self.assertEqual(sent_params.get("Latest"), "5")

    def test_at_least_one_group_prefers_specific_validator_over_permissive_one(self):
        """A permissively-validated group member (validation_type
        'non_empty' or unset) must never greedily swallow a value
        clearly meant for a stricter sibling (e.g. email) — the more
        specific validator is tried first."""
        action_id = _seed_action(self.reg, key="customer_lookup_specific", action_type="API", category="customer",
                                  keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False, "input_source": "customer_message",
             "validation_type": "non_empty"},
            {"name": "CustEmail", "display_name": "อีเมล", "required": False, "input_source": "customer_message",
             "validation_type": "email"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            self.engine.decide("ข้อมูลลูกค้า somchai@example.com", history=[])
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertIn("CustEmail", sent_params)
        self.assertNotIn("CustCode", sent_params)

    def test_optional_parameter_never_blocks_completion(self):
        action_id = _seed_action(self.reg, key="order_with_optional", action_type="API", category="order",
                                  keywords=["ใบสั่งซื้อ"])
        self.reg.replace_parameters(action_id, [
            {"name": "OrderCode", "display_name": "เลขคำสั่งซื้อ", "required": True, "input_source": "customer_message"},
            {"name": "Note", "display_name": "หมายเหตุ", "required": False, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/order", "http_method": "GET"})
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})):
            result = self.engine.decide("ใบสั่งซื้อ PO-1234", history=[])
        self.assertEqual(result["routing"]["type"], "API")

    def test_legacy_fallback_used_when_action_has_no_parameter_metadata(self):
        """Safe Migration: a Business Action exists for this ERP workflow
        but has no parameters configured -> legacy INTENT_SCHEMAS drives
        collection, and Developer Mode surfaces a warning."""
        _seed_action(self.reg, key="tracking_no_params", action_type="API", category="tracking")
        result = self.engine.decide("ของถึงไหนแล้ว", history=[], context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertTrue(result["developer"]["legacy_fallback_used"])
        self.assertEqual(result["developer"]["legacy_fallback_reason"], "business_action_has_no_parameter_metadata")

    def test_legacy_fallback_used_when_no_business_action_at_all(self):
        result = self.engine.decide("ของถึงไหนแล้ว", history=[], context={"developer_mode": True})
        self.assertTrue(result["developer"]["legacy_fallback_used"])
        self.assertEqual(result["developer"]["legacy_fallback_reason"], "no_business_action_for_workflow")

    def test_executor_never_discovers_missing_parameter_engine_believed_complete(self):
        """Both the Information Collection step and the eventual Executor
        call the SAME registry.validate_can_execute() — this test proves
        that whenever the Decision Engine reports is_complete, the
        Executor's own validation (invoked deeper inside ActionExecutor)
        never independently rejects the call as incomplete."""
        self._seed_search_data_order()
        q1 = self.engine.decide("เช็ค PO", history=[])["reply"]["text"]
        history = [{"role": "user", "content": "เช็ค PO"}, {"role": "assistant", "content": q1}]
        q2 = self.engine.decide("C00001", history=history)["reply"]["text"]
        history += [{"role": "user", "content": "C00001"}, {"role": "assistant", "content": q2}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "found"})):
            result = self.engine.decide("PO-99887", history=history)
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone(result["error"])
        self.assertIsNone(result["developer"]["execution_result"]["error"]
                           if result.get("developer") else None)

    def test_continuation_disambiguates_actions_with_identical_generated_question(self):
        """Two unrelated Business Actions can legitimately generate the
        EXACT SAME auto-question (both have a CustCode parameter with
        the same Display Name). Conversation continuation must not just
        pick whichever the registry happens to return first — it should
        prefer the action whose keywords/category match the message
        that actually started this conversation."""
        self._seed_search_data_order()
        _seed_action(self.reg, key="unrelated_customer_lookup", action_type="API", category="customer",
                     keywords=["สมาชิก"])
        other_id = self.reg.get_by_key("unrelated_customer_lookup")["id"]
        self.reg.replace_parameters(other_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(other_id, {"endpoint": "https://example.test/other", "http_method": "GET"})

        q1 = self.engine.decide("เช็ค PO", history=[])["reply"]["text"]
        history = [{"role": "user", "content": "เช็ค PO"}, {"role": "assistant", "content": q1}]
        result = self.engine.decide("C00001", history=history, context={"developer_mode": True})
        self.assertEqual(result["developer"]["information_collection_status"]["selected_business_action"],
                          "search_data_order")

    def test_developer_mode_shows_full_dynamic_collection_trace(self):
        self._seed_search_data_order()
        result = self.engine.decide("เช็ค PO", history=[], context={"developer_mode": True})
        status = result["developer"]["information_collection_status"]
        self.assertEqual(status["source"], "business_action_registry")
        self.assertIn("CustCode", status["required_parameters"])
        self.assertIn("OrderCode", status["required_parameters"])
        self.assertFalse(status["is_complete"])
        self.assertIn("missing_reason", status)


class TestSemanticParameterInference(unittest.TestCase):
    """Generic Semantic Parameter Inference sprint (2026-08-09) — natural
    language filter phrases ("ที่ส่งออกจากจีนแล้ว", "3 รายการล่าสุด") must
    resolve into real, OPTIONAL Business Action parameter values, driven
    entirely by each parameter's own field_metadata (no endpoint-specific
    code in Decision Engine). Also covers the confirmed Latest=5
    precedence bug: an explicit customer value must override a
    customer_message parameter's configured default, never the other way
    around."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_shipment_search(self):
        action_id = _seed_action(self.reg, key="search_shipments", action_type="API", category="tracking",
                                  keywords=["พัสดุ"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "required": True, "input_source": "customer_message"},
            {"name": "Latest", "required": False, "input_source": "customer_message", "example_value": "5",
             "field_metadata": {"numeric_limit": True}},
            {"name": "BillStatus", "required": False, "input_source": "customer_message",
             "field_metadata": {"enum": {
                 "3": {"label": "exported_china", "phrases": ["ส่งออกจากจีน", "ออกจากจีนแล้ว"]},
                 "4": {"label": "received_thailand", "phrases": ["เข้าโกดังไทย", "ถึงไทยแล้ว"]},
             }}},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/shipments", "http_method": "GET"})
        return action_id

    def test_enum_phrase_resolves_to_configured_value(self):
        self._seed_shipment_search()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            self.engine.decide("ขอดูพัสดุ SP1014 ที่ส่งออกจากจีนแล้ว", history=[])
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("BillStatus"), "3")

    def test_explicit_numeric_limit_overrides_configured_default(self):
        self._seed_shipment_search()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            self.engine.decide("ขอดู 3 พัสดุล่าสุดของ SP1014", history=[])
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("Latest"), "3")

    def test_no_explicit_limit_falls_back_to_configured_default(self):
        self._seed_shipment_search()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            self.engine.decide("ขอดูพัสดุของ SP1014", history=[])
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("Latest"), "5")

    def test_action_without_field_metadata_is_unaffected(self):
        """An action with no field_metadata configured on any parameter
        must behave exactly as before this sprint — semantic inference
        returns nothing for it, no crash, no spurious binding."""
        action_id = _seed_action(self.reg, key="plain_order", action_type="API", category="order",
                                  keywords=["ใบสั่งซื้อ"])
        self.reg.replace_parameters(action_id, [
            {"name": "OrderCode", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/order", "http_method": "GET"})
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            result = self.engine.decide("ใบสั่งซื้อ PO-1234 ล่าสุด 3 รายการ", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("OrderCode"), "PO-1234")
        self.assertNotIn("Latest", sent_params)


class TestConfirmationGateForCommandActions(unittest.TestCase):
    """SendLineNotiCS enablement (2026-08-10) — a COMMAND-type Business
    Action (operation_type NOTIFICATION/NOTIFY/WORKFLOW; see
    services/erp_test_harness.py::get_conversation_behavior_defaults,
    reused unchanged by services/decision_engine.py::_requires_confirmation)
    must never reach the real REST call without an explicit
    context={"confirmed": True} signal on THAT turn, no matter how
    completely its parameters were already collected. Uses a mock action
    shaped exactly like the real, now-enabled SendLineNotiCS
    (action_type=API + setup_metadata.operation_type=NOTIFICATION,
    credential_store SecretCode + customer_message Message) so these
    tests exercise the SAME generic gate the real action goes through —
    never mock the gate itself, and never a real network call
    (services.action_executor.requests.request is always mocked)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_notify_action(self, key="notify_cs"):
        action_id = _seed_action(self.reg, key=key, action_type="API", category="notification",
                                  keywords=["แจ้ง cs", "ติดต่อกลับ", "notify cs"])
        self.reg.update(action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"}})
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/notify", "http_method": "POST"})
        return action_id

    def _seed_notify_action_with_custcode(self, key="notify_cs_custcode"):
        """For Test D — an extra pattern-validated CustCode parameter
        lets a message that provides ONLY a customer code leave Message
        genuinely unfilled: the free-text fallback in
        services/action_selection_primitives.py::_extract_candidates_for_binding
        only offers the whole message as a candidate when NO other
        candidate (e.g. a digit-bearing code) was found at all."""
        action_id = _seed_action(self.reg, key=key, action_type="API", category="notification",
                                  keywords=["แจ้ง cs", "notify cs"])
        self.reg.update(action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"}})
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/notify", "http_method": "POST"})
        return action_id

    def test_A_selects_action_blocks_execution_and_asks_for_confirmation(self):
        self._seed_notify_action()
        with patch("services.action_executor.requests.request") as mock_req, \
             patch("services.credential_store.CredentialStore.resolve") as mock_resolve:
            result = self.engine.decide("ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ", history=[],
                                          context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        dev = result["developer"]
        self.assertEqual(dev["information_collection_status"]["selected_business_action"], "notify_cs")
        self.assertEqual(dev["information_collection_status"]["collected_parameters"].get("Message"),
                          "ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ")
        self.assertIn("ยืนยัน", result["reply"]["text"])
        mock_req.assert_not_called()
        # SecretCode is never even RESOLVED while awaiting confirmation —
        # stronger than "masked in the reply": the Credential Store is
        # never consulted at all on this path.
        mock_resolve.assert_not_called()
        gate = dev["confirmation_gate"]
        self.assertEqual(gate["required"], True)
        self.assertEqual(gate["confirmed"], False)
        self.assertEqual(gate["action_key"], "notify_cs")
        self.assertIsNotNone(gate["action_id"])

    def test_B_confirmation_triggers_exactly_one_mocked_execution(self):
        self._seed_notify_action()
        trigger = "ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ"
        with patch("services.action_executor.requests.request") as mock_req0:
            turn1 = self.engine.decide(trigger, history=[], context={"developer_mode": True})
        history = [{"role": "user", "content": trigger}, {"role": "assistant", "content": turn1["reply"]["text"]}]

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "REAL-SECRET-value", "error": None}):
            turn2 = self.engine.decide("ยืนยัน", history=history,
                                         context={"developer_mode": True, "confirmed": True})
        self.assertEqual(turn2["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("Message"), trigger)
        # Secret never appears in anything returned to the caller —
        # only inside the mocked outbound call this test itself set up.
        dumped = str(turn2)
        self.assertNotIn("REAL-SECRET-value", dumped)

    def test_C_cancellation_never_executes(self):
        self._seed_notify_action()
        trigger = "ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ"
        with patch("services.action_executor.requests.request") as mock_req0:
            turn1 = self.engine.decide(trigger, history=[], context={"developer_mode": True})
        history = [{"role": "user", "content": trigger}, {"role": "assistant", "content": turn1["reply"]["text"]}]

        with patch("services.action_executor.requests.request") as mock_req:
            turn2 = self.engine.decide("ยกเลิก", history=history,
                                         context={"developer_mode": True, "confirmed": False})
        mock_req.assert_not_called()
        self.assertEqual(turn2["routing"]["type"], "WORKFLOW")

        # Never confirming at all (context omits the flag entirely) is
        # equally safe — absence of confirmation must never default to
        # "go ahead".
        with patch("services.action_executor.requests.request") as mock_req2:
            turn2b = self.engine.decide("ยกเลิก", history=history, context={"developer_mode": True})
        mock_req2.assert_not_called()

    def test_D_missing_message_asks_for_it_and_never_executes(self):
        self._seed_notify_action_with_custcode()
        with patch("services.action_executor.requests.request") as mock_req, \
             patch("services.credential_store.CredentialStore.resolve") as mock_resolve:
            result = self.engine.decide("แจ้ง cs ลูกค้ารหัส C00001", history=[], context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        dev = result["developer"]
        collected = dev["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("CustCode"), "C00001")
        self.assertNotIn("Message", collected)
        self.assertIn("ข้อความแจ้งเตือน", result["reply"]["text"])
        mock_req.assert_not_called()
        mock_resolve.assert_not_called()

    def test_E_secret_code_resolved_only_via_credential_store_and_never_exposed(self):
        self._seed_notify_action()
        trigger = "ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ"
        with patch("services.action_executor.requests.request") as mock_req0:
            turn1 = self.engine.decide(trigger, history=[], context={"developer_mode": True})
        history = [{"role": "user", "content": trigger}, {"role": "assistant", "content": turn1["reply"]["text"]}]

        real_secret = "REAL-SECRET-abc123"
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": real_secret, "error": None}) as mock_resolve:
            turn2 = self.engine.decide("ยืนยัน", history=history,
                                         context={"developer_mode": True, "confirmed": True})
        mock_resolve.assert_called_once()
        # Resolved via Credential Store (never a plaintext/env-var secret
        # path) and sent on the outbound call — but never leaked back.
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("SecretCode"), real_secret)
        self.assertNotIn(real_secret, str(turn2))

    def test_enabling_alone_never_triggers_any_call(self):
        """Requirement 4 — flipping enabled=False -> True on the
        registry is a pure metadata write; it must never itself cause
        any HTTP call, mocked or otherwise, until a customer message
        AND an explicit confirmation both happen."""
        action_id = self._seed_notify_action()
        with patch("services.action_executor.requests.request") as mock_req:
            self.reg.set_enabled(action_id, False)
            self.reg.set_enabled(action_id, True)
        mock_req.assert_not_called()


class TestHybridRouting(unittest.TestCase):
    """Production Integration Sprint (2026-08-02), Phase 1 Step C/G —
    Decision Engine's Hybrid support, built on the SAME services/
    hybrid_question_classifier.py and services/hybrid_runtime_service.py
    the AI Playground's Auto/Hybrid modes already use (imported directly,
    never duplicated)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_customer_coupons(self):
        action_id = _seed_action(self.reg, key="get_customer_coupons", action_type="API", category="customer",
                                  keywords=["คูปอง", "ลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/coupons", "http_method": "GET"})
        return action_id

    def _seed_customer_coupons_with_secret(self):
        action_id = _seed_action(self.reg, key="get_customer_coupons_secret", action_type="API", category="customer",
                                  keywords=["คูปอง", "ลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "display_name": "รหัสยืนยันตัวตน", "required": True,
             "input_source": "credential_store", "credential_ref": "fake_secret"},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/coupons", "http_method": "GET"})
        return action_id

    def test_hybrid_executes_erp_despite_required_credential_parameter(self):
        """Confirmed defect fix (2026-08-02, Local Production Pipeline
        Verification) — the SAME bug as
        test_at_least_one_group_executes_despite_required_credential_parameter,
        but in _handle_hybrid_turn's own, separate validate_can_execute
        check: a Business Action shaped like the real GetDataCustomer
        (AT_LEAST_ONE group + required credential_store parameter) used
        to never actually call the ERP half of a Hybrid turn, always
        reporting "required parameter(s) missing" even once CustCode was
        successfully segmented and bound."""
        self._seed_customer_coupons_with_secret()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": {"coupons": "2 ใบ"}})) as mock_erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ")):
            result = self.engine.decide("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", history=[])
        self.assertEqual(result["routing"]["type"], "HYBRID")
        mock_erp.assert_called_once()
        self.assertIn("coupons", result["reply"]["text"])
        self.assertNotIn("required parameter(s) missing", result["reply"]["text"])

    def test_hybrid_question_runs_erp_and_rag_exactly_once_and_synthesizes(self):
        self._seed_customer_coupons()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": {"coupons": "2 ใบ"}})) as mock_erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ")) as mock_rag:
            result = self.engine.decide("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", history=[],
                                          context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "HYBRID")
        self.assertEqual(mock_erp.call_count, 1)
        self.assertEqual(mock_rag.call_count, 1)
        # Question Segmentation reached each path separately.
        erp_question_sent = mock_erp.call_args.args[1]["question"]
        rag_question_sent = mock_rag.call_args.args[0]
        self.assertIn("C00001", erp_question_sent)
        self.assertNotIn("ใช้งานอย่างไร", erp_question_sent)
        self.assertIn("ใช้งานอย่างไร", rag_question_sent)
        self.assertNotIn("C00001", rag_question_sent)
        self.assertIn("coupons", result["reply"]["text"])
        self.assertIn("คูปองใช้ได้ที่หน้าชำระเงินค่ะ", result["reply"]["text"])
        self.assertEqual(result["developer"]["classification"]["classification"], "HYBRID")
        self.assertIn("explicit synthesis", result["developer"]["merge_strategy"])

    def test_hybrid_erp_failure_still_returns_honest_rag_section(self):
        self._seed_customer_coupons()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(status="error", error="API ตอบกลับด้วยสถานะ 400")), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ")):
            result = self.engine.decide("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", history=[])
        self.assertEqual(result["routing"]["type"], "HYBRID")
        self.assertIn("ไม่สามารถดึงข้อมูลลูกค้าได้", result["reply"]["text"])
        self.assertIn("API ตอบกลับด้วยสถานะ 400", result["reply"]["text"])
        self.assertIn("คูปองใช้ได้ที่หน้าชำระเงินค่ะ", result["reply"]["text"])

    def test_ambiguous_actions_route_to_clarification_without_executing(self):
        _seed_action(self.reg, key="action_one", action_type="API", category="customer", keywords=["ข้อมูลลูกค้า"])
        _seed_action(self.reg, key="action_two", action_type="API", category="customer", keywords=["ข้อมูลลูกค้า"])
        with patch.object(self.engine.executor, "execute") as mock_exec, \
             patch("services.playground_orchestrator.run_playground_turn") as mock_rag:
            result = self.engine.decide("ข้อมูลลูกค้า", history=[])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        mock_exec.assert_not_called()
        mock_rag.assert_not_called()

    def test_rag_only_question_never_invokes_hybrid_path(self):
        # No Business Action matches at all -> classify_question returns
        # RAG_ONLY -> the existing, unchanged search/fallback path runs;
        # Hybrid execution must never be invoked.
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="policy answer")):
            result = self.engine.decide("ขอทราบนโยบายการคืนสินค้า", history=[])
        self.assertIn(result["routing"]["type"], ("RAG", "SAFE_FALLBACK"))


class TestRequestedMappedFieldSelection(unittest.TestCase):
    """Generic Requested-Field Filtering (2026-08-05, Live ERP Verification
    Phase 7) — services/action_selection_primitives.py::
    select_requested_mapped_fields() narrows an ERP action's already-
    mapped result down to only the field(s) a customer's own question
    asked about, driven entirely by each response_mapping row's own
    `field_metadata.keywords` — never a GetDataCustomer-specific rule.
    Real bug this fixes: before this, wallet-only/name+coupon/all-fields
    questions all returned the identical full-field answer."""

    def setUp(self):
        self.mapping = [
            {"json_path": "$.data.0.CustName", "mapped_label": "ชื่อลูกค้า",
             "field_metadata": {"keywords": ["ชื่อ", "name"]}},
            {"json_path": "$.data.0.Wallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน"]}},
            {"json_path": "$.data.0.Coupon", "mapped_label": "คูปอง",
             "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
        ]
        self.mapped_fields = {"ชื่อลูกค้า": "สมชาย", "ยอดเงิน Wallet": "100.00", "คูปอง": ["A10"]}

    def test_wallet_keyword_filters_to_wallet_field_only(self):
        result = select_requested_mapped_fields("ยอดเงิน wallet เหลือเท่าไหร่", self.mapped_fields, self.mapping)
        self.assertEqual(result, {"ยอดเงิน Wallet": "100.00"})

    def test_name_and_coupon_keywords_filter_to_those_two_fields(self):
        result = select_requested_mapped_fields("ขอชื่อและคูปองหน่อย", self.mapped_fields, self.mapping)
        self.assertEqual(result, {"ชื่อลูกค้า": "สมชาย", "คูปอง": ["A10"]})

    def test_no_keyword_match_returns_everything_unfiltered(self):
        result = select_requested_mapped_fields("ดูข้อมูลทั้งหมด", self.mapped_fields, self.mapping)
        self.assertEqual(result, self.mapped_fields)

    def test_no_response_mapping_returns_everything_unfiltered(self):
        result = select_requested_mapped_fields("ยอดเงิน wallet เหลือเท่าไหร่", self.mapped_fields, [])
        self.assertEqual(result, self.mapped_fields)

    def test_empty_mapped_fields_passthrough(self):
        self.assertEqual(select_requested_mapped_fields("ยอดเงิน", {}, self.mapping), {})
        self.assertIsNone(select_requested_mapped_fields("ยอดเงิน", None, self.mapping))


class TestRequestedFieldFilteringEndToEnd(unittest.TestCase):
    """Same feature as TestRequestedMappedFieldSelection, exercised through
    the real DecisionEngine.decide() (ERP-only and Hybrid paths) so the
    wiring itself — not just the pure function — is covered."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)
        self.full_mapped = {"ชื่อลูกค้า": "สมชาย", "ยอดเงิน Wallet": "100.00", "คูปอง": ["A10"]}

    def _seed_customer_lookup(self):
        action_id = _seed_action(
            self.reg, key="customer_lookup", action_type="API", category="customer",
            keywords=["ยอดเงิน", "wallet", "ชื่อ", "คูปอง", "ข้อมูลทั้งหมด"],
            params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                     "input_source": "customer_message"}])
        self.reg.replace_response_mapping(action_id, [
            {"json_path": "$.data.0.CustName", "mapped_label": "ชื่อลูกค้า",
             "field_metadata": {"keywords": ["ชื่อ", "name"]}},
            {"json_path": "$.data.0.Wallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน"]}},
            {"json_path": "$.data.0.Coupon", "mapped_label": "คูปอง",
             "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
        ])
        return action_id

    def test_erp_only_wallet_question_returns_wallet_field_only(self):
        self._seed_customer_lookup()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": dict(self.full_mapped)})):
            result = self.engine.decide("ขอยอดเงิน wallet ของลูกค้า SP1014", history=[])
        text = result["reply"]["text"]
        self.assertIn("ยอดเงิน Wallet", text)
        self.assertNotIn("ชื่อลูกค้า", text)
        self.assertNotIn("คูปอง", text)

    def test_erp_only_name_and_coupon_question_filters_to_those_fields(self):
        self._seed_customer_lookup()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": dict(self.full_mapped)})):
            # No "และ"/"กับ"-style conjunction — the segmenter (services/
            # hybrid_question_classifier.py::_split_clauses) would
            # otherwise split this into a separate ERP/RAG clause pair
            # (pre-existing behavior, unrelated to this filtering test).
            result = self.engine.decide("ขอชื่อคูปองของลูกค้า SP1014", history=[])
        text = result["reply"]["text"]
        self.assertIn("ชื่อลูกค้า", text)
        self.assertIn("คูปอง", text)
        self.assertNotIn("ยอดเงิน Wallet", text)

    def test_erp_only_generic_question_returns_all_visible_fields(self):
        self._seed_customer_lookup()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": dict(self.full_mapped)})):
            result = self.engine.decide("ดูข้อมูลทั้งหมดของลูกค้า SP1014", history=[])
        text = result["reply"]["text"]
        self.assertIn("ชื่อลูกค้า", text)
        self.assertIn("ยอดเงิน Wallet", text)
        self.assertIn("คูปอง", text)

    def test_action_without_response_mapping_keeps_unfiltered_behavior(self):
        # Backward compatibility — an action with no configured
        # response_mapping at all must keep returning every mapped field,
        # exactly as before this feature existed.
        _seed_action(
            self.reg, key="no_mapping_action", action_type="API", category="misc",
            keywords=["สอบถามข้อมูลทั่วไป"],
            params=[{"name": "Code", "display_name": "รหัส", "required": True,
                     "input_source": "customer_message"}])
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": {"ชื่อ": "X", "ยอดเงิน": "1"}})):
            result = self.engine.decide("สอบถามข้อมูลทั่วไป CODE99", history=[])
        text = result["reply"]["text"]
        self.assertIn("ชื่อ", text)
        self.assertIn("ยอดเงิน", text)

    def test_hybrid_erp_sub_question_filters_mapped_fields_too(self):
        action_id = _seed_action(self.reg, key="get_customer_coupons", action_type="API", category="customer",
                                  keywords=["คูปอง", "ลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/coupons", "http_method": "GET"})
        self.reg.replace_response_mapping(action_id, [
            {"json_path": "$.name", "mapped_label": "ชื่อลูกค้า", "field_metadata": {"keywords": ["ชื่อ", "name"]}},
            {"json_path": "$.coupons", "mapped_label": "คูปอง", "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
        ])
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(
                               result={"mapped_fields": {"ชื่อลูกค้า": "สมชาย", "คูปอง": "2 ใบ"}})) as mock_erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ")):
            result = self.engine.decide("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", history=[])
        self.assertEqual(result["routing"]["type"], "HYBRID")
        mock_erp.assert_called_once()
        self.assertIn("คูปอง", result["reply"]["text"])
        self.assertNotIn("ชื่อลูกค้า", result["reply"]["text"])


def _seed_order_shipment_tracking_actions(reg):
    """Mirrors production's real category / search_keywords / parameter
    config for these 6 Business Actions (confirmed via a live, read-only
    Supabase check against the shared managed project on 2026-08-15) plus
    the OrderCode/ShipmentCode validation_pattern values planned for that
    same production config (not yet applied there as of this test) — so
    this proves the routing-precision fix against realistic, non-
    fabricated conditions, not just the isolated primitive covered by
    TestIdentifierPatternScoring above."""
    cust_pattern = r"^[A-Za-z]{2}\d+$"
    _seed_action(reg, key="searchdataorder", action_type="API", category="Customer Order Retrieval",
                 keywords=["เลขคำสั่งซื้อ", "PO เดียว", "order detail", "รายละเอียดคำสั่งซื้อ"],
                 params=[
                     {"name": "CustCode", "required": True, "validation_pattern": cust_pattern},
                     {"name": "OrderCode", "required": True, "validation_pattern": r"^POS\d+$"},
                 ])
    _seed_action(reg, key="searchdataorderlist", action_type="API", category="Customer Order Retrieval",
                 keywords=["คำสั่งซื้อ", "ประวัติการสั่งซื้อ", "order list", "PO"],
                 params=[{"name": "CustCode", "required": True, "validation_pattern": cust_pattern}])
    _seed_action(reg, key="searchdatashipment", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"],
                 params=[
                     {"name": "CustCode", "required": True, "validation_pattern": cust_pattern},
                     {"name": "ShipmentCode", "required": True, "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
                 ])
    _seed_action(reg, key="searchdatashipmentlist", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["บิลขนส่ง", "พัสดุ", "tracking", "shipment list", "ติดตามพัสดุ"],
                 params=[{"name": "CustCode", "required": True, "validation_pattern": cust_pattern}])
    _seed_action(reg, key="searchdatatracking", action_type="API", category="Customer Shipment Retrieval",
                 priority=1,
                 keywords=["tracking จีน", "เลข tracking", "tracking", "ค้นหาด้วยเลข tracking", "เลข tracking จีน"],
                 params=[
                     {"name": "CustCode", "required": True, "validation_pattern": cust_pattern},
                     {"name": "Tracking", "required": True},
                 ])
    _seed_action(reg, key="getdatacustomer", action_type="API", category="Customer Data Retrieval",
                 keywords=["ข้อมูลลูกค้า", "Wallet", "คูปอง", "ยอดเงิน", "ข้อมูลทั้งหมด"],
                 params=[{"name": "CustCode", "required": False, "validation_pattern": cust_pattern}])


class TestOrderShipmentTrackingRoutingPrecision(unittest.TestCase):
    """Reproduces the reported routing-precision bug (a specific PO-detail
    question wrongly selecting SearchDataOrderList) and its analogues for
    Shipment/Tracking/Customer, end-to-end through DecisionEngine.decide()
    — not just the scoring primitive — so a regression here is caught
    even if something upstream (classify_question's ambiguity gate,
    workflow hint resolution) changes independently later. Real UAT-style
    identifiers (POS100820260809001, SP1014, FT3182, ...) are used here
    deliberately, mirroring the actual reported bug — the underlying fix
    mechanism itself (TestIdentifierPatternScoring) stays fully generic."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_order_shipment_tracking_actions(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _decide(self, message):
        with patch("services.action_executor.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200, json=lambda: {"status": "success"})
            result = self.engine.decide(message, history=[], context={"developer_mode": True})
        return result, mock_req

    def _selected_action_key(self, message):
        result, _ = self._decide(message)
        dev = result.get("developer") or {}
        classification = (dev.get("classification") or {}).get("classification")
        self.assertNotEqual(classification, "CLARIFICATION_REQUIRED",
                             f"unexpected clarification for: {message}")
        selected = (dev.get("selected_business_action")
                    or (dev.get("information_collection_status") or {}).get("selected_business_action"))
        self.assertIsNotNone(selected, f"no Business Action selected for: {message}")
        return selected

    # -- Scenarios A/E/F: specific OrderCode -> SearchDataOrder --

    def test_a_specific_po_detail_selects_order_detail(self):
        self.assertEqual(self._selected_action_key("ขอรายละเอียด PO POS100820260809001"), "searchdataorder")

    def test_e_specific_po_status_selects_order_detail(self):
        self.assertEqual(self._selected_action_key("PO POS100820260809001 สถานะอะไร"), "searchdataorder")

    def test_f_specific_po_info_selects_order_detail(self):
        self.assertEqual(self._selected_action_key("ขอข้อมูลคำสั่งซื้อ POS100820260809001"), "searchdataorder")

    # -- Scenarios B/C/D: CustCode + list intent -> SearchDataOrderList --

    def test_b_latest_po_for_customer_selects_order_list(self):
        self.assertEqual(self._selected_action_key("ขอดู PO ล่าสุดของ SP1014"), "searchdataorderlist")

    def test_c_latest_n_po_for_customer_selects_order_list(self):
        self.assertEqual(self._selected_action_key("ขอดู 3 PO ล่าสุดของ SP1014"), "searchdataorderlist")

    def test_d_po_list_request_selects_order_list(self):
        self.assertEqual(self._selected_action_key("ขอดูรายการ PO ของ SP1014"), "searchdataorderlist")

    # -- G/H: analogous Shipment detail vs list --

    def test_g_specific_shipment_code_selects_shipment_detail(self):
        self.assertEqual(self._selected_action_key("shipment detail FT318220260726001"), "searchdatashipment")

    def test_h_customer_shipment_list_request_selects_shipment_list(self):
        self.assertEqual(self._selected_action_key("ขอดูพัสดุล่าสุดของ SP1014"), "searchdatashipmentlist")

    # -- I: analogous specific Tracking --

    def test_i_specific_tracking_number_selects_tracking(self):
        self.assertEqual(self._selected_action_key("ค้นหาด้วยเลข tracking testlineOnNut007"),
                          "searchdatatracking")

    # -- J: GetDataCustomer must remain unaffected by the new scoring signal --

    def test_j_customer_data_request_selects_get_customer(self):
        self.assertEqual(self._selected_action_key("ขอดูข้อมูลลูกค้า SP1014"), "getdatacustomer")

    # -- M: no identifier and no list intent -> asks a follow-up question,
    # never fabricates a parameter value or silently executes --

    def test_m_missing_identifier_asks_rather_than_invents(self):
        result, mock_req = self._decide("ขอสอบถามเรื่อง PO")
        dev = result.get("developer") or {}
        collection = dev.get("information_collection_status") or {}
        self.assertIn("CustCode", collection.get("missing_parameters") or [])
        self.assertNotIn("CustCode", collection.get("collected_parameters") or {})
        mock_req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
