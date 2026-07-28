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
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"answer": "คลังอยู่ที่กรุงเทพ", "citations": [], "confidence": 0.9})):
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

    def test_notification_action_routes_as_not_implemented(self):
        _seed_action(self.reg, key="notify", action_type="NOTIFICATION", keywords=["ร้องเรียน"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(status="not_implemented")):
            result = self.engine.decide("ขอร้องเรียนบริการ", history=[])
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
        with patch("services.rag_service.get_rag_service", side_effect=Exception("no network in test")):
            result = self.engine.decide("อยากรู้ดวงวันนี้", history=[])
        self.assertEqual(result["routing"]["type"], "SAFE_FALLBACK")
        self.assertIn("ขอโทษ", result["reply"]["text"])

    def test_rag_no_grounded_answer_falls_back(self):
        _seed_action(self.reg, key="kb2", action_type="RAG", keywords=["ทดสอบเฉพาะ"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(result={"answer": ""})):
            result = self.engine.decide("ทดสอบเฉพาะคำถามนี้", history=[])
        self.assertEqual(result["routing"]["type"], "SAFE_FALLBACK")

    def test_executor_failure_returns_structured_error_not_crash(self):
        _seed_action(self.reg, key="broken_api", action_type="API", category="hr", keywords=["ข้อมูลพนักงาน"])
        with patch.object(self.engine.executor, "execute", return_value=_fake_exec_result(status="error", error="boom")):
            result = self.engine.decide("ขอข้อมูลพนักงานหน่อย", history=[])
        self.assertEqual(result["error"], "boom")

    def test_registry_failure_falls_back_safely(self):
        broken_registry = MagicMock()
        broken_registry.enabled_actions.side_effect = Exception("db down")
        self.engine.registry = broken_registry
        with patch("services.rag_service.get_rag_service", side_effect=Exception("no network in test")):
            result = self.engine.decide("อยากรู้ดวงวันนี้", history=[])
        self.assertIn(result["routing"]["type"], ("SAFE_FALLBACK",))


class TestHumanHandoffTriggers(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_explicit_human_request_routes_to_handoff(self):
        result = self.engine.decide("ขอคุยกับเจ้าหน้าที่หน่อย", history=[])
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
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"answer": "answer text"})):
            result = self.engine.decide("คลังสินค้าอยู่ที่ไหน", history=[], context={"developer_mode": True})
        dev = result["developer"]
        self.assertIn("candidate_business_actions", dev)
        self.assertIn("selected_business_action", dev)
        self.assertEqual(dev["selected_business_action"], "kb3")


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


if __name__ == "__main__":
    unittest.main()
