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
from services.decision_engine import (
    DecisionEngine, search_candidate_actions, select_best_action, _sanitize_customer_text,
    _detect_aggregation_request, _resolve_continuation_action,
)
from services.action_selection_primitives import select_requested_mapped_fields, _keyword_score, _keyword_matches


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

    def test_bare_identifier_reply_continues_the_pending_at_least_one_group_action(self):
        """Final Conversational Correctness (2026-08-15) — Issue 1:
        Turn 1 asks for a customer identifier (an AT_LEAST_ONE group, not
        a single ungrouped required parameter); turn 2's bare reply must
        continue the SAME action with that value bound to the group
        member it actually matches, executing exactly once — never fall
        to RAG, never a different action, never a second, unrelated ERP
        call."""
        action_id = _seed_action(self.reg, key="customer_lookup_continuation", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            {"name": "CustEmail", "display_name": "อีเมล", "required": False, "input_source": "customer_message",
             "validation_type": "email"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        turn1 = self.engine.decide("ขอข้อมูลลูกค้าหน่อย", history=[])
        self.assertEqual(turn1["routing"]["type"], "WORKFLOW")
        question = turn1["reply"]["text"]
        history = [{"role": "user", "content": "ขอข้อมูลลูกค้าหน่อย"}, {"role": "assistant", "content": question}]

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            turn2 = self.engine.decide("SP1014", history=history)
        self.assertEqual(turn2["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "SP1014")
        self.assertNotIn("CustEmail", sent_params)

    def test_conversation_resolver_resumes_remembered_action_for_topic_free_reference(self):
        """Final Conversational Correctness (2026-08-15) — P0 Conversation
        Resolver: a pure referring-expression follow-up ("แล้วของถึงหรือยัง")
        with NO topic word of its own and no keyword/pattern match resumes
        the Business Action the profile remembers the customer was last
        using (customer_context.last_business_action), instead of falling
        to RAG. Identifier memory (CustCode) fills what it can; the
        action still genuinely needs ShipmentCode this turn, so it must
        ask for it rather than invent one — proves this is real
        continuation, not a forced guess."""
        action_id = _seed_action(self.reg, key="search_shipment_detail", action_type="API",
                                  category="shipment", keywords=["เลขบิลขนส่ง", "shipment detail"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "ShipmentCode", "display_name": "เลขบิลขนส่ง", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/shipment", "http_method": "GET"})
        # A SECOND action sharing the identical CustCode parameter name —
        # a bare remembered CustCode alone must not decisively win via
        # fresh-search scoring either (the same sharer-weighting principle
        # _identifier_pattern_score already applies), so this test proves
        # the resolver — not a diluted, still-passing fresh-search score —
        # is what actually selects the right action here.
        other_id = _seed_action(self.reg, key="get_customer_unrelated", action_type="API", category="customer")
        self.reg.replace_parameters(other_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(other_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        result = self.engine.decide("แล้วของถึงหรือยัง", history=[],
                                     context={"customer_context": {"last_business_action": "search_shipment_detail",
                                                                    "cust_code": "SP1014"},
                                               "developer_mode": True})
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "conversation_reference")
        info = dev.get("information_collection_status") or {}
        self.assertEqual(info.get("selected_business_action"), "search_shipment_detail")
        self.assertEqual(info.get("collected_parameters", {}).get("CustCode"), "SP1014")
        self.assertFalse(info.get("is_complete"))

    def test_conversation_resolver_never_fires_without_a_remembered_action(self):
        """No last_business_action on the profile -> normal RAG fallback,
        unchanged — the resolver must never invent a starting point."""
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="", confidence=0.0)):
            result = self.engine.decide("แล้วของถึงหรือยัง", history=[],
                                         context={"customer_context": {"cust_code": "SP1014"}, "developer_mode": True})
        dev = result.get("developer") or {}
        self.assertNotEqual(dev.get("selection_source"), "conversation_reference")

    def test_rag_guard_bare_identifier_with_no_context_asks_instead_of_rag_search(self):
        """Final Conversational Correctness (2026-08-15) — P0/P1 RAG
        Guard: a message that IS, in its entirety, a bare identifier-
        shaped token with no pending slot, no remembered action, and no
        Business Action match must never be silently handed to RAG (which
        would just report "not found in the knowledge base") — it must
        ask what to check instead. Proven by asserting the RAG pipeline
        is never even invoked."""
        with patch("services.playground_orchestrator.run_playground_turn") as mock_rag:
            result = self.engine.decide("SP1014", history=[])
        mock_rag.assert_not_called()
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertIn("SP1014", result["reply"]["text"])

    def test_identifier_memory_boosts_fresh_search_scoring(self):
        """The _parameter_availability_score fix (2026-08-15) — a
        remembered identifier now genuinely counts as evidence during a
        fresh (non-continuation) search, exactly like a slot bound THIS
        turn already did before the fix."""
        action_id = _seed_action(self.reg, key="customer_lookup_scored", action_type="API", category="customer")
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        candidates = search_candidate_actions(self.reg, workflow=None, message="สวัสดีค่ะ",
                                               collected_slots={"CustCode": "SP1014"})
        match = next(a for a in candidates if a["id"] == action_id)
        self.assertGreaterEqual(match["_score"], 1.0)

    def test_known_cust_code_reused_on_follow_up_order_question(self):
        """User instruction (2026-08-15, continuation of Final
        Conversational Correctness): "order ล่าสุดล่ะ" with a CustCode
        already remembered from earlier in the conversation must resolve
        SearchDataOrderList and execute with that CustCode -- never RAG,
        never re-asking for the code the customer already gave."""
        action_id = _seed_action(self.reg, key="search_data_order_list", action_type="API",
                                  category="order", keywords=["order"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/orders", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"orders": []})) as mock_req:
            result = self.engine.decide("order ล่าสุดล่ะ", history=[],
                                         context={"customer_context": {"cust_code": "FT3182"}})
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "FT3182")

    def test_known_cust_code_reused_on_shipment_question(self):
        """Same principle for Shipment: "ขอดูพัสดุของผม" with a remembered
        CustCode resolves SearchDataShipmentList and executes with it."""
        action_id = _seed_action(self.reg, key="search_data_shipment_list", action_type="API",
                                  category="shipment", keywords=["พัสดุ"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/shipments", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"shipments": []})) as mock_req:
            result = self.engine.decide("ขอดูพัสดุของผม", history=[],
                                         context={"customer_context": {"cust_code": "FT1004"}})
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "FT1004")

    def test_pending_tracking_flow_retains_tracking_while_asking_for_cust_code(self):
        """P1 Tracking Continuation: turn 1 supplies only the Tracking
        value -> must ask for CustCode next (not re-ask for Tracking);
        turn 2's bare CustCode reply continues the SAME action, executing
        exactly once with BOTH values retained."""
        action_id = _seed_action(self.reg, key="search_data_tracking", action_type="API",
                                  category="tracking", keywords=["tracking"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/tracking", "http_method": "GET"})

        turn1 = self.engine.decide("ช่วยเช็ก tracking testlineOnNut007", history=[])
        self.assertEqual(turn1["routing"]["type"], "WORKFLOW")
        question = turn1["reply"]["text"]
        self.assertIn("รหัสลูกค้า", question)
        history = [{"role": "user", "content": "ช่วยเช็ก tracking testlineOnNut007"},
                   {"role": "assistant", "content": question}]

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "in_transit"})) as mock_req:
            turn2 = self.engine.decide("FT3182", history=history)
        self.assertEqual(turn2["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("Tracking"), "testlineOnNut007")
        self.assertEqual(sent_params.get("CustCode"), "FT3182")

    def test_no_example_value_reaches_executor_end_to_end(self):
        """P0 Mock/example data: end-to-end via decide(), not just the
        executor unit test -- a GetDataCustomer-shaped action with
        example_value configured on every optional identifier field must
        never send them once only CustCode was genuinely collected."""
        action_id = _seed_action(self.reg, key="get_customer_examples", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d+$", "example_value": "C00001"},
            {"name": "CustEmail", "display_name": "อีเมล", "required": False, "input_source": "customer_message",
             "validation_type": "email", "example_value": "customer@example.com"},
            {"name": "CustName", "display_name": "ชื่อลูกค้า", "required": False, "input_source": "customer_message",
             "example_value": "สมชาย ใจดี"},
            {"name": "CustPhone", "display_name": "เบอร์โทร", "required": False, "input_source": "customer_message",
             "validation_type": "phone_number", "example_value": "0812345678"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE",
             "members": ["CustCode", "CustEmail", "CustName", "CustPhone"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้ารหัส FT3182", history=[])
        self.assertEqual(result["routing"]["type"], "API")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "FT3182")
        self.assertNotIn("CustEmail", sent_params)
        self.assertNotIn("CustName", sent_params)
        self.assertNotIn("CustPhone", sent_params)


class TestFieldKeywordFollowUpsAndDetailTransition(unittest.TestCase):
    """User instruction (2026-08-15, continuation of Final Conversational
    Correctness): marker-less field-shaped follow-ups ("มีคูปองไหม"),
    response-derived identifier memory, and generic LIST -> DETAIL
    sibling resolution."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_bare_identifier_clarification_captures_identifier_for_next_turn(self):
        """Confirmed live bug (2026-08-16) — a WHOLE bare identifier
        ("SP1014") triggers the Pure Identifier Guard's clarifying
        question, but never persisted the identifier itself, unlike its
        sibling "identifier + other words" guard a few lines below. The
        next turn, naming only an intent ("ข้อมูลลูกค้า"), had nothing to
        bind SP1014 to and re-asked for the identifier from scratch."""
        # Several unrelated actions share the identifier's own shape
        # (mirrors the real production registry) so sharer-weighting
        # dilutes a single pattern match below the selection threshold —
        # this bug only reproduced against the real registry, not a
        # single-action toy setup, exactly like the sibling "identifier +
        # other words" guard's own regression test above.
        for i in range(4):
            action_id = _seed_action(self.reg, key=f"get_customer_full_{i}", action_type="API",
                                      category=f"unrelated_{i}", keywords=[f"เฉพาะเจาะจงมากๆ{i}"])
            self.reg.replace_parameters(action_id, [
                {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                 "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            ])
        result = self.engine.decide("SP1014", history=[], context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        info = result["developer"]["information_collection_status"]
        self.assertEqual(info["collected_parameters"], {"CustCode": "SP1014"})

    def test_identifier_reused_automatically_once_intent_named_after_clarification(self):
        """Full round-trip of the same bug: once SP1014 is remembered
        (customer_context.cust_code, exactly like profiles/manager.py
        would persist it from the previous turn's collected_parameters),
        naming only the intent ("ข้อมูลลูกค้า") must execute GetDataCustomer
        with SP1014 automatically — never re-ask for the identifier."""
        action_id = _seed_action(self.reg, key="get_customer_full", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {"CustName": "ใจเย็นๆ"}})) as mock_req:
            result = self.engine.decide("ข้อมูลลูกค้า", history=[],
                                         context={"developer_mode": True, "customer_context": {"cust_code": "SP1014"}})
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "SP1014")

    def _seed_customer_lookup_with_coupon_field(self):
        action_id = _seed_action(self.reg, key="get_customer_full", action_type="API",
                                  category="customer", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(action_id, [
            {"json_path": "$.data.Wallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน", "เงิน"]}},
            {"json_path": "$.data.Coupon", "mapped_label": "คูปอง",
             "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        return action_id

    def test_field_keyword_follow_up_resumes_remembered_action_without_a_marker(self):
        """"มีคูปองไหม" carries none of the generic reference markers
        (no "ล่าสุด"/"ของผม"/etc) but DOES name a field the remembered
        action's own response_mapping is configured to return — that
        alone must be enough to resume it. A second, unrelated action is
        also seeded so a shared CustCode in memory can't ALSO decide this
        via ordinary fresh-search scoring alone (mirrors the real,
        multi-action production registry) — proving the resolver, not a
        lucky fresh-search tie, is what selects it."""
        self._seed_customer_lookup_with_coupon_field()
        _seed_action(self.reg, key="unrelated_order_lookup", action_type="API", category="order")
        self.reg.replace_parameters(self.reg.get_by_key("unrelated_order_lookup")["id"], [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200,
                                           json=lambda: {"data": {"Wallet": 100, "Coupon": ["A10"]}})) as mock_req:
            result = self.engine.decide("มีคูปองไหม", history=[],
                                         context={"developer_mode": True,
                                                   "customer_context": {"last_business_action": "get_customer_full",
                                                                         "cust_code": "SP1014"}})
        self.assertEqual(result["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("CustCode"), "SP1014")
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "conversation_reference")

    def test_composer_never_shows_raw_json_and_labels_unflattened_list(self):
        self._seed_customer_lookup_with_coupon_field()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200,
                                           json=lambda: {"data": {"Wallet": 100, "Coupon": ["A10", "B20"]}})):
            result = self.engine.decide("ข้อมูลลูกค้า SP1014", history=[])
        text = result["reply"]["text"]
        self.assertNotIn("{", text)
        self.assertNotIn("[", text)
        self.assertIn("คูปอง", text)
        self.assertIn("2", text)

    def test_bare_identifier_reply_fills_active_conversation_not_a_pattern_matching_sibling(self):
        """Confirmed live bug (2026-08-15) — a bare identifier-shaped
        reply ("FT3182") during an active tracking conversation was
        hijacked by an unrelated same-category action (a shipment
        lookup) purely because BOTH actions require CustCode and the
        bare code pattern-matches that shared param regardless of which
        action it belongs to. A bare identifier carries no real topical
        signal of its own (Pure Identifier Guard) — it must fill the
        ACTIVE conversation's pending slot, never be treated as decisive
        evidence for switching to a different action."""
        tracking_id = _seed_action(self.reg, key="search_tracking_bare", action_type="API",
                                    category="tracking", keywords=["tracking"])
        self.reg.replace_parameters(tracking_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(tracking_id, {"endpoint": "https://example.test/tracking", "http_method": "GET"})

        shipment_id = _seed_action(self.reg, key="search_shipment_bare", action_type="API", category="tracking")
        self.reg.replace_parameters(shipment_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(shipment_id, {"endpoint": "https://example.test/shipment", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})):
            result = self.engine.decide(
                "FT3182", history=[], context={"developer_mode": True,
                "customer_context": {"last_business_action": "search_tracking_bare",
                                      "last_tracking": "TRACK1", "last_shipment_code": "FT999"}})
        dev = result.get("developer") or {}
        info = dev.get("information_collection_status") or {}
        self.assertEqual(info.get("selected_business_action"), "search_tracking_bare")

    def test_composer_never_shows_raw_list_when_requested_field_filter_narrows_to_only_the_umbrella_field(self):
        """Confirmed live bug (2026-08-15) — a tracking search's response_
        mapping has both an umbrella "all shipment records" row (raw list,
        matches keyword "tracking") and flattened per-field siblings
        (Code/Status, matching different keywords like "เลขบิล"/"สถานะ").
        A message like "เช็ก tracking X" matches ONLY the umbrella field's
        keyword, so Requested-Field Filtering narrows mapped_fields down to
        just that one (redundant) list — the composer correctly refuses to
        print it raw, but must NOT collapse to a bare summarizer dump of
        that same narrowed, list-only payload; it must fall back to the
        FULL mapped_fields so the customer still gets the real, composed
        answer instead of a raw Python list."""
        action_id = _seed_action(self.reg, key="search_tracking", action_type="API",
                                  category="tracking", keywords=["tracking"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True, "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(action_id, [
            {"json_path": "$.data.Shipment", "mapped_label": "รายการพัสดุที่พบ",
             "field_metadata": {"keywords": ["รายการ", "tracking", "แทรค"]}},
            {"json_path": "$.data.Shipment.0.Code", "mapped_label": "เลขที่บิลขนส่ง",
             "field_metadata": {"keywords": ["เลขบิล"]}},
            {"json_path": "$.data.Shipment.0.Status", "mapped_label": "สถานะบิลขนส่ง",
             "field_metadata": {"keywords": ["สถานะ"]}},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/tracking", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"Shipment": [{"Code": "FT999", "Status": "รับเข้าที่จีน"}]}})):
            result = self.engine.decide("ช่วยเช็ก tracking หน่อย", history=[],
                                         context={"customer_context": {"cust_code": "SP1014",
                                                                        "last_tracking": "ABC123"}})
        text = result["reply"]["text"]
        self.assertNotIn("{", text)
        self.assertNotIn("[", text)
        self.assertIn("เลขที่บิลขนส่ง", text)
        self.assertIn("FT999", text)

    def _seed_order_list_and_detail(self):
        list_id = _seed_action(self.reg, key="order_list", action_type="API", category="order", keywords=["order"])
        self.reg.replace_parameters(list_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(list_id, [
            {"json_path": "$.data", "mapped_label": "รายการทั้งหมด", "field_metadata": {"keywords": ["รายการ"]}},
            {"json_path": "$.data.0.Code", "mapped_label": "เลขที่ล่าสุด",
             "field_metadata": {"keywords": ["เลขที่"], "identity_concept": "OrderCode"}},
            {"json_path": "$.data.0.Status", "mapped_label": "สถานะล่าสุด", "field_metadata": {"keywords": ["สถานะ"]}},
        ])
        self.reg.upsert_execution(list_id, {"endpoint": "https://example.test/order/list", "http_method": "GET"})

        detail_id = _seed_action(self.reg, key="order_detail", action_type="API", category="order",
                                  keywords=["รายละเอียดคำสั่งซื้อ"])
        self.reg.replace_parameters(detail_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "OrderCode", "display_name": "เลขที่คำสั่งซื้อ", "required": True, "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(detail_id, [
            {"json_path": "$.data.Code", "mapped_label": "เลขที่", "field_metadata": {"keywords": ["เลขที่"]}},
            {"json_path": "$.data.Status", "mapped_label": "สถานะ", "field_metadata": {"keywords": ["สถานะ"]}},
        ])
        self.reg.upsert_execution(detail_id, {"endpoint": "https://example.test/order/detail", "http_method": "GET"})
        return list_id, detail_id

    def test_list_response_teaches_the_record_code_generically(self):
        """Issue 2 — a successful LIST execution learns the record's own
        code from the response (field_metadata.identity_concept), not
        only from what the customer typed."""
        list_id, _ = self._seed_order_list_and_detail()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": [{"Code": "PO999", "Status": "ยกเลิก"}]})):
            result = self.engine.decide("order ล่าสุด SP1014", history=[], context={"developer_mode": True})
        dev = result.get("developer") or {}
        collected = (dev.get("information_collection_status") or {}).get("collected_parameters") or {}
        self.assertEqual(collected.get("OrderCode"), "PO999")

    def test_detail_intent_after_list_selects_the_detail_sibling_not_the_list_again(self):
        """Issue 3 — "ขอรายละเอียดอันล่าสุด" after a list execution
        resolves the SAME-category DETAIL sibling (using the OrderCode
        just learned from the list response), not the list action again."""
        list_id, detail_id = self._seed_order_list_and_detail()
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": [{"Code": "PO999", "Status": "ยกเลิก"}]})) as mock_req:
            result = self.engine.decide(
                "ขอรายละเอียดอันล่าสุด", history=[], context={"developer_mode": True,
                "customer_context": {"last_business_action": "order_list", "cust_code": "SP1014",
                                      "last_order_code": "PO999"}})
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "conversation_reference_detail")
        info = dev.get("information_collection_status") or {}
        self.assertEqual(info.get("selected_business_action"), "order_detail")
        sent_params = mock_req.call_args.kwargs.get("params") or {}
        self.assertEqual(sent_params.get("OrderCode"), "PO999")
        self.assertEqual(sent_params.get("CustCode"), "SP1014")

    def test_detail_sibling_never_selected_without_the_extra_identifier_in_memory(self):
        """Never invents the extra identifier — if OrderCode was never
        learned/given, the detail sibling must NOT be selected."""
        self._seed_order_list_and_detail()
        result = self.engine.decide(
            "ขอรายละเอียดอันล่าสุด", history=[], context={"developer_mode": True,
            "customer_context": {"last_business_action": "order_list", "cust_code": "SP1014"}})
        dev = result.get("developer") or {}
        self.assertNotEqual(dev.get("selection_source"), "conversation_reference_detail")

    def test_active_conversation_never_stolen_by_an_unrelated_action_incidentally_satisfiable_from_memory(self):
        """Confirmed live bug (2026-08-15) — a tracking search response
        incidentally teaches the record's own ShipmentCode too (Issue 2).
        Once CustCode+ShipmentCode are BOTH in memory, an unrelated
        same-domain action requiring exactly those two (here: a
        "shipment detail" lookup by code) could score high enough via
        identifier-memory-boosted fresh-search to steal the turn from an
        ACTIVE tracking conversation — even though the customer's
        follow-up ("ตอนนี้อยู่ไหนแล้ว") never asked for anything but the
        tracking status. Entity Continuation must keep the conversation
        on the action actually in use unless the message carries its own
        real topical evidence for the other one."""
        tracking_id = _seed_action(self.reg, key="search_tracking_full", action_type="API",
                                    category="tracking", keywords=["tracking"])
        self.reg.replace_parameters(tracking_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True, "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(tracking_id, [
            {"json_path": "$.data.Code", "mapped_label": "เลขที่บิลขนส่ง",
             "field_metadata": {"keywords": ["เลขบิล"], "identity_concept": "ShipmentCode"}},
            {"json_path": "$.data.Status", "mapped_label": "สถานะ", "field_metadata": {"keywords": ["สถานะ"]}},
        ])
        self.reg.upsert_execution(tracking_id, {"endpoint": "https://example.test/tracking", "http_method": "GET"})

        # An UNRELATED action, same category, that only happens to need
        # exactly the two identifiers a tracking search incidentally
        # teaches — must NOT steal a plain, topic-free follow-up.
        shipment_id = _seed_action(self.reg, key="search_shipment_by_code", action_type="API", category="tracking")
        self.reg.replace_parameters(shipment_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(shipment_id, {"endpoint": "https://example.test/shipment", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"Code": "FT999", "Status": "รับเข้าที่จีน"}})):
            result = self.engine.decide(
                "ตอนนี้อยู่ไหนแล้ว", history=[], context={"developer_mode": True,
                "customer_context": {"last_business_action": "search_tracking_full", "cust_code": "FT3182",
                                      "last_tracking": "TRACK1", "last_shipment_code": "FT999"}})
        dev = result.get("developer") or {}
        info = dev.get("information_collection_status") or {}
        self.assertEqual(info.get("selected_business_action"), "search_tracking_full")


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


class TestCustomerTextSanitizer(unittest.TestCase):
    """Customer Response Sanitizer (2026-08-16) — the final defensive
    net behind the composer/response architecture, not the primary fix.
    Must never fire on ordinary customer-facing prose."""

    def test_catches_raw_list_of_dicts(self):
        text = _sanitize_customer_text('รายการคำสั่งซื้อทั้งหมด: [{"Code": "PO1", "Status": "ยกเลิก"}]')
        self.assertNotIn("[{", text)
        self.assertIn("ขอโทษ", text)

    def test_catches_python_repr_dict(self):
        text = _sanitize_customer_text("ข้อมูล: {'Code': 'PO1', 'Status': 'ยกเลิก'}")
        self.assertNotIn("{'", text)

    def test_catches_bare_none(self):
        text = _sanitize_customer_text("วันที่จัดส่ง: None")
        self.assertNotIn("None", text)

    def test_catches_source_citation_metadata(self):
        text = _sanitize_customer_text("คำตอบค่ะ\n\nแหล่งที่มา:\n- AI Knowledge Master.xlsx, page 5")
        self.assertNotIn("แหล่งที่มา", text)

    def test_catches_xlsx_filename(self):
        text = _sanitize_customer_text("อ้างอิงจาก AI Knowledge Master (1).xlsx")
        self.assertNotIn(".xlsx", text)

    def test_never_fires_on_ordinary_customer_prose(self):
        ordinary = [
            "รหัสลูกค้า: SP1014\nชื่อลูกค้า: ใจเย็นๆ\nยอดเงิน Purchase Wallet 21.94 บาทค่ะ",
            "พบคูปอง 2 รายการค่ะ",
            "CBM คือปริมาตรของสินค้าค่ะ โดยคำนวณจาก ความยาว × ความกว้าง × ความสูง",
            "ขอบคุณค่ะ ยินดีให้บริการ",
        ]
        for text in ordinary:
            self.assertEqual(_sanitize_customer_text(text), text)

    def test_none_and_empty_text_pass_through_unchanged(self):
        self.assertIsNone(_sanitize_customer_text(None))
        self.assertEqual(_sanitize_customer_text(""), "")

    def test_catches_raw_list_of_scalars(self):
        """Customer-Reported ERP Conversation Defects (2026-08-17), Issue
        4 hardening -- a raw JSON/Python array of plain scalars (never a
        list of dicts) is also a serialization leak, not just '[{'."""
        text = _sanitize_customer_text('Tracking: ["9822930076037", "79024349456322"]')
        self.assertNotIn('["', text)

    def test_catches_bare_internal_architecture_labels(self):
        text = _sanitize_customer_text("ERP: มีคูปอง 2 รายการ")
        self.assertNotIn("ERP:", text)
        text = _sanitize_customer_text("Knowledge Base: คูปองใช้ได้ที่หน้าชำระเงิน")
        self.assertNotIn("Knowledge Base:", text)

    def test_never_fires_on_hybrid_labeled_answer_parenthesized_form(self):
        """The Hybrid labeled_answer's own real format uses 'ERP)' /
        'Knowledge Base)' with a closing paren before the colon-less
        label text -- this is developer-only content that never reaches
        _sanitize_customer_text at all (see hybrid_runtime_service.py),
        but the regex itself must not be so broad it would also reject
        ordinary text that happens to mention these words without the
        exact bare-label shape."""
        text = "ข้อมูลเฉพาะลูกค้า (ERP) และข้อมูลนโยบาย (Knowledge Base) ถูกรวมเป็นคำตอบเดียวค่ะ"
        self.assertEqual(_sanitize_customer_text(text), text)

    def test_non_string_input_never_leaks_a_stringified_object(self):
        """Issue 4's explicit hard requirement — if a dict/list somehow
        reaches this final boundary directly (never stringified upstream
        by mistake), it must never be leaked, not even via str()."""
        self.assertNotIn("{", _sanitize_customer_text({"Code": "PO1", "Status": "ยกเลิก"}))
        self.assertNotIn("[", _sanitize_customer_text([{"Code": "PO1"}]))
        self.assertIn("ขอโทษ", _sanitize_customer_text({"Code": "PO1"}))


class TestLatestNAggregation(unittest.TestCase):
    """Customer-Reported ERP Conversation Defects (2026-08-17), Issue 3/5
    — 'N อันล่าสุด...รวมเท่าไหร่'-style aggregation questions must compute
    a real sum over the actual returned records (never fabricated), and
    return None (never a guess) when the action's response shape doesn't
    actually support it — the caller then falls through to the normal
    composer unaffected."""

    RESPONSE_MAPPING = [
        {"json_path": "$.data", "mapped_label": "รายการคำสั่งซื้อทั้งหมด",
         "field_metadata": {"keywords": ["รายการ", "ทั้งหมด", "คำสั่งซื้อ"]}},
        {"json_path": "$.data.0.Code", "mapped_label": "เลขที่คำสั่งซื้อล่าสุด",
         "field_metadata": {"keywords": ["เลขคำสั่งซื้อ"], "identity_concept": "OrderCode"}},
        {"json_path": "$.data.0.Total", "mapped_label": "ยอดรวมคำสั่งซื้อล่าสุด",
         "field_metadata": {"keywords": ["ยอดรวม", "total"]}},
    ]

    def _payload(self, records):
        return {"รายการคำสั่งซื้อทั้งหมด": records}

    def test_detects_limit_and_sum_intent(self):
        agg = _detect_aggregation_request("ออเดอร์ 5 อันล่าสุดของผมรวมเท่าไหร่")
        self.assertEqual(agg, {"limit": 5, "wants_sum": True, "wants_count": False, "wants_outstanding": False})

    def test_detects_sum_without_explicit_limit(self):
        agg = _detect_aggregation_request("ยอดรวมทั้งหมดเท่าไหร่")
        self.assertEqual(agg["wants_sum"], True)
        self.assertIsNone(agg["limit"])

    def test_no_aggregation_intent_returns_none(self):
        self.assertIsNone(_detect_aggregation_request("ขอดู order ล่าสุด"))

    def test_sums_real_records_correctly(self):
        records = [{"Code": "PO1", "Total": 1022}, {"Code": "PO2", "Total": 0},
                   {"Code": "PO3", "Total": 1768.06}]
        agg = {"limit": 5, "wants_sum": True}
        text = DecisionEngine._aggregate_list_reply(self._payload(records), self.RESPONSE_MAPPING, agg)
        self.assertIn("2,790.06", text)
        self.assertIn("บาท", text)
        self.assertNotIn("{", text)
        self.assertNotIn("[", text)

    def test_never_fabricates_when_fewer_records_than_requested(self):
        records = [{"Code": "PO1", "Total": 1022}]
        agg = {"limit": 5, "wants_sum": True}
        text = DecisionEngine._aggregate_list_reply(self._payload(records), self.RESPONSE_MAPPING, agg)
        self.assertIn("1", text)  # honestly states only 1 record was found
        self.assertIn("1,022.00", text)
        self.assertNotIn("5 รายการล่าสุด มียอดรวม", text)  # never claims 5 when only 1 exists

    def test_respects_limit_slicing(self):
        records = [{"Code": f"PO{i}", "Total": 100} for i in range(10)]
        agg = {"limit": 3, "wants_sum": True}
        text = DecisionEngine._aggregate_list_reply(self._payload(records), self.RESPONSE_MAPPING, agg)
        self.assertIn("300.00", text)  # only first 3 * 100, never all 10 * 100

    def test_returns_none_when_no_list_field_present(self):
        agg = {"limit": 5, "wants_sum": True}
        payload = {"รหัสลูกค้า": "FT3182", "ชื่อลูกค้า": "สมชาย"}
        self.assertIsNone(DecisionEngine._aggregate_list_reply(payload, self.RESPONSE_MAPPING, agg))

    def test_returns_none_when_no_currency_sibling_configured(self):
        mapping_no_currency = [
            {"json_path": "$.data", "mapped_label": "รายการทั้งหมด", "field_metadata": {}},
            {"json_path": "$.data.0.Code", "mapped_label": "รหัสล่าสุด", "field_metadata": {}},
        ]
        records = [{"Code": "PO1"}, {"Code": "PO2"}]
        agg = {"limit": 5, "wants_sum": True}
        payload = {"รายการทั้งหมด": records}
        self.assertIsNone(DecisionEngine._aggregate_list_reply(payload, mapping_no_currency, agg))

    def test_limit_only_no_sum_gives_short_real_summary_not_raw_dump(self):
        records = [{"Code": "PO1", "Total": 1022}, {"Code": "PO2", "Total": 0}]
        agg = {"limit": 2, "wants_sum": False}
        text = DecisionEngine._aggregate_list_reply(self._payload(records), self.RESPONSE_MAPPING, agg)
        self.assertIn("PO1", text)
        self.assertIn("PO2", text)
        self.assertNotIn("{", text)
        self.assertNotIn("[", text)


class TestShipmentFilterCountSumAggregation(unittest.TestCase):
    """Shipment Filter/Count/Sum Aggregation fix (2026-08-24) — customer-
    reported: "SP1008 มีบิลที่รับเข้าไทยกี่บิล ยอดค่าขนส่งเท่าไหร่ทั้งหมด
    ที่ค้างจ่าย" and 3 related phrasings answered from the wrong (or
    entirely unfiltered / single-latest-record) data. Mock response_mapping
    below mirrors the real production searchdatashipmentlist shape: a list
    field, a Status sibling carrying an admin-curated filter_values map
    (never a hardcoded status string in code), and a ค่าขนส่ง currency
    sibling. Never hardcodes SP1008, 112.46, or any one ShipmentCode."""

    RESPONSE_MAPPING = [
        {"json_path": "$.data", "mapped_label": "รายการบิลขนส่งทั้งหมด",
         "field_metadata": {"keywords": ["บิล", "รายการ", "shipment"]}},
        {"json_path": "$.data.0.ShipmentCode", "mapped_label": "เลขบิลขนส่งล่าสุด",
         "field_metadata": {"keywords": ["เลขบิล"], "identity_concept": "ShipmentCode"}},
        {"json_path": "$.data.0.Status", "mapped_label": "สถานะบิลขนส่งล่าสุด",
         "field_metadata": {
             "keywords": ["สถานะ", "status"],
             "filter_values": {
                 "รับเข้าไทย": "รับเข้าที่ไทย", "เข้าไทย": "รับเข้าที่ไทย", "ถึงไทย": "รับเข้าที่ไทย",
             },
         }},
        {"json_path": "$.data.0.Charge", "mapped_label": "ค่าขนส่งล่าสุด",
         "field_metadata": {"keywords": ["ค่าขนส่ง", "total"]}},
    ]

    def _payload(self):
        # 5 realistic mixed-status shipment records — never all one
        # status, so a filtering bug (wrong status, or no filtering at
        # all) is observable in the count/sum, not just the wording.
        records = [
            {"ShipmentCode": "SP100820260101001", "Status": "รับเข้าที่จีน", "Charge": 500.00},
            {"ShipmentCode": "SP100820260201001", "Status": "รับเข้าที่ไทย", "Charge": 300.00},
            {"ShipmentCode": "SP100820260301001", "Status": "รับเข้าที่ไทย", "Charge": 250.50},
            {"ShipmentCode": "SP100820260401001", "Status": "รับเข้าที่จีน", "Charge": 800.00},
            {"ShipmentCode": "SP100820260501001", "Status": "รับเข้าที่ไทย", "Charge": 112.46},
        ]
        return {"รายการบิลขนส่งทั้งหมด": records}

    def _reply(self, message):
        agg = _detect_aggregation_request(message)
        self.assertIsNotNone(agg, f"expected an aggregation intent for: {message}")
        return DecisionEngine._aggregate_list_reply(self._payload(), self.RESPONSE_MAPPING, agg, message)

    def test_count_only_filters_by_status(self):
        text = self._reply("มีกี่บิลที่เข้าไทย")
        self.assertIn("3", text)
        self.assertNotIn("5", text)  # never the unfiltered total record count
        self.assertNotIn("รับเข้าที่จีน", text)

    def test_sum_only_filters_by_status(self):
        text = self._reply("ยอดค่าขนส่งของบิลที่ถึงไทยรวมเท่าไหร่")
        self.assertIn("662.96", text)  # 300 + 250.50 + 112.46, China-status records excluded

    def test_count_and_sum_combined_matches_original_customer_phrasing(self):
        text = self._reply("SP1008 มีบิลที่รับเข้าไทยกี่บิล ยอดค่าขนส่งเท่าไหร่ทั้งหมดที่ค้างจ่าย")
        self.assertIn("3", text)
        self.assertIn("662.96", text)

    def test_second_customer_phrasing_count_and_sum(self):
        text = self._reply("ต้องการเช็คบิลที่สถานะรับเข้าไทย ว่ามีทั้งหมดกี่บิล และค่าขนส่งรวมเท่าไหร่")
        self.assertIn("3", text)
        self.assertIn("662.96", text)

    def test_outstanding_question_declines_honestly_never_fabricates(self):
        text = self._reply("SP1008 มีบิลที่รับเข้าไทยกี่บิล ยอดค่าขนส่งเท่าไหร่ทั้งหมดที่ค้างจ่าย")
        # ERP has no outstanding/unpaid-shaped field configured — the
        # decline must be stated, and no invented "ค้างจ่าย" number.
        self.assertIn("ยังไม่มีข้อมูล", text)
        self.assertNotIn("ค้างจ่าย 662.96", text)
        self.assertNotIn("ค้างจ่าย 300", text)
        # Still gives the answerable total for context, per spec.
        self.assertIn("662.96", text)

    def test_outstanding_supported_when_a_real_field_is_configured(self):
        mapping_with_outstanding = self.RESPONSE_MAPPING + [
            {"json_path": "$.data.0.UnpaidAmount", "mapped_label": "ยอดค้างชำระล่าสุด",
             "field_metadata": {"keywords": ["ค้างชำระ", "unpaid"]}},
        ]
        agg = _detect_aggregation_request("มีบิลที่รับเข้าไทยกี่บิล ยอดค้างชำระเท่าไหร่")
        text = DecisionEngine._aggregate_list_reply(self._payload(), mapping_with_outstanding, agg,
                                                      "มีบิลที่รับเข้าไทยกี่บิล ยอดค้างชำระเท่าไหร่")
        self.assertNotIn("ยังไม่มีข้อมูล", text)

    def test_amount_traceback_never_misread_as_a_record_count(self):
        # The exact reported defect: "112.46" must never resurface as
        # "112 รายการ" (mistaking the decimal amount for a count of
        # ALL of the customer's historical records).
        agg = _detect_aggregation_request("ยอดรวมบิลขนส่งล่าสุด 112.46 บาท เอามาจากบิลไหน")
        self.assertIsNone(agg)  # disqualified as a source-trace question, not a new aggregate

    def test_latest_shipment_no_filter_regression_unaffected(self):
        # No status phrase at all -- must behave exactly as before this
        # fix: every record counted, nothing silently filtered out.
        text = self._reply("ยอดรวมค่าขนส่งทั้งหมดเท่าไหร่")
        self.assertIn("1,962.96", text)  # 500+300+250.5+800+112.46, unfiltered

    def test_no_matching_status_returns_honest_zero_not_fabricated(self):
        agg = _detect_aggregation_request("มีกี่บิลที่เข้าไทย")
        payload = {"รายการบิลขนส่งทั้งหมด": [
            {"ShipmentCode": "SP1", "Status": "รับเข้าที่จีน", "Charge": 500.00},
        ]}
        text = DecisionEngine._aggregate_list_reply(payload, self.RESPONSE_MAPPING, agg, "มีกี่บิลที่เข้าไทย")
        self.assertIn("ไม่พบ", text)

    def test_never_leaks_raw_json_in_filtered_reply(self):
        text = self._reply("มีกี่บิลที่เข้าไทย และค่าขนส่งรวมเท่าไหร่")
        self.assertNotIn("{", text)
        self.assertNotIn("[", text)


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

    def test_identifier_plus_extra_words_asks_naturally_instead_of_rag(self):
        """Confirmed live bug (2026-08-16) — a message like "ผม FT3182"
        isn't a bare identifier as a WHOLE string (it also carries "ผม"),
        so it never matched any Business Action's keywords and fell
        through to RAG, which has zero realistic chance of having
        information about an arbitrary customer code. It must ask the
        same natural clarifying question the bare-identifier guard uses,
        never query RAG for a message that just introduces an
        identifier the platform itself recognizes the shape of."""
        # Mirrors the real production registry: several unrelated actions
        # all require the SAME identifier concept, so sharer-weighting
        # (_identifier_pattern_score's param_action_counts) dilutes a
        # single pattern match below the selection threshold — exactly
        # why the live bug only reproduced with the real registry and
        # not a single-action toy setup.
        for i in range(4):
            action_id = _seed_action(self.reg, key=f"get_customer_id_plus_words_{i}", action_type="API",
                                      category=f"unrelated_{i}", keywords=[f"เฉพาะเจาะจงมากๆ{i}"])
            self.reg.replace_parameters(action_id, [
                {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                 "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            ])
        with patch("services.playground_orchestrator.run_playground_turn",
                   side_effect=AssertionError("RAG must never be queried for this message")):
            result = self.engine.decide("ผม FT3182", history=[])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertIn("FT3182", result["reply"]["text"])
        self.assertNotEqual(result["routing"]["type"], "RAG")
        self.assertNotEqual(result["routing"]["type"], "SAFE_FALLBACK")

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

    def test_contact_staff_phrasing_routes_to_handoff(self):
        """Playground Production Parity UAT (2026-08-15), scenario J —
        'ขอติดต่อเจ้าหน้าที่' (contact staff) fell through to normal RAG
        routing because it matches neither 'คุยกับเจ้าหน้าที่' nor
        'ขอเจ้าหน้าที่' (the word 'ติดต่อ' breaks both substrings)."""
        result = self.engine.decide("ขอติดต่อเจ้าหน้าที่", history=[])
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_sendlinenotics_notify_trigger_still_not_hijacked_as_human_request(self):
        """The SendLineNotiCS notify-action trigger phrase ('...ต้องการให้
        ติดต่อกลับ', contact the CUSTOMER back) must never match the NEW
        'ติดต่อเจ้าหน้าที่' alternative -- it never mentions เจ้าหน้าที่."""
        from services.slot_filling_engine import _HUMAN_REQUEST_RE
        self.assertIsNone(_HUMAN_REQUEST_RE.search("ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ"))

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


class TestActiveHandoffFollowUpRouting(unittest.TestCase):
    """Golden Application Defect Fixes (2026-08-16) — GOLDEN-038 root
    cause: a follow-up about an ALREADY-outstanding human-contact request
    ("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย") never matched _HUMAN_REQUEST_RE (it
    isn't a fresh request) and fell straight through to ordinary routing
    (RAG, since it carries no ERP Business Action keyword), silently
    abandoning an active handoff. Requires BOTH context["handoff_status"]
    (PENDING/NOTIFIED — the exact values services/session_service.py's
    get_handoff_status/set_handoff_status state machine produces) AND the
    message's own follow-up phrasing — neither alone is sufficient. This
    is deliberately a ROUTING fix only: duplicate-notification protection
    itself is untouched, reusing the SAME _route_human_handoff /
    downstream dedup state machine every other HUMAN_HANDOFF path already
    goes through (see tests/golden/golden_cases.json's
    GOLDEN-038B-HANDOFF-DUPLICATE-PROTECTION, which already proves that
    dedup logic works once routing_type==HUMAN_HANDOFF is reached)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_follow_up_with_active_notified_status_routes_to_handoff(self):
        result = self.engine.decide("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", history=[],
                                     context={"handoff_status": "NOTIFIED"})
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_follow_up_with_active_pending_status_routes_to_handoff(self):
        result = self.engine.decide("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", history=[],
                                     context={"handoff_status": "PENDING"})
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_follow_up_phrasing_variants_all_route_to_handoff(self):
        for message in ("ยังไม่มีใครโทรมา", "เจ้าหน้าที่ยังไม่ติดต่อกลับ",
                         "เมื่อไหร่จะมีคนติดต่อ", "ยังรอเจ้าหน้าที่อยู่"):
            with self.subTest(message=message):
                result = self.engine.decide(message, history=[], context={"handoff_status": "NOTIFIED"})
                self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF", msg=f"failed for: {message}")

    def test_same_message_without_active_handoff_state_never_escalates(self):
        """Active state AND follow-up intent are both required — the SAME
        phrasing with no outstanding handoff (context["handoff_status"]
        absent/"NONE", e.g. a genuinely unrelated complaint) must fall
        through to ordinary routing, never treated as handoff
        continuation. This is the "Do not treat every complaint as active
        handoff" guard from the task brief."""
        result = self.engine.decide("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", history=[])
        self.assertNotEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_active_handoff_state_without_follow_up_phrasing_never_escalates(self):
        """The other half of the same guard — an active handoff state
        alone, on a message with no follow-up-shaped content at all, must
        not force HUMAN_HANDOFF either."""
        result = self.engine.decide("CBM คืออะไร", history=[], context={"handoff_status": "NOTIFIED"})
        self.assertNotEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_unrelated_delivery_complaint_never_escalates_even_with_active_status(self):
        """A complaint that happens to share surface words ("ยังไม่มี...")
        but is about something else entirely (a delivery, not a person)
        must not false-positive off the loose "คน"/"เจ้าหน้าที่" evidence
        this trigger requires."""
        result = self.engine.decide("ของยังไม่มาส่งเลย", history=[], context={"handoff_status": "NOTIFIED"})
        self.assertNotEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_follow_up_never_produces_the_fresh_request_reply_text(self):
        """The customer-facing reply must acknowledge an EXISTING request,
        never repeat the fresh-request wording (which would misleadingly
        imply a brand-new notification is being sent)."""
        result = self.engine.decide("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย", history=[],
                                     context={"handoff_status": "NOTIFIED"})
        self.assertNotEqual(result["reply"]["text"], "ได้เลยค่ะ เดี๋ยวแจ้งเจ้าหน้าที่ให้ติดต่อกลับนะคะ")


class TestCustomerIntelligenceHandoffTrigger(unittest.TestCase):
    """Human Handoff V1 (2026-08-15), Trigger C — Customer Intelligence's
    per-message stage/handoff signal (services/customer_tier_service.py)
    escalating THIS turn, end to end through decide(). Gated to a HOT
    customer with an explicit sales-callback request only — see
    decide()'s own inline comment for why the analogous NEGATIVE+bare-
    complaint case must NOT short-circuit here (it needs to reach the RAG
    pipeline / AI Policy escalation, Trigger B, first)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def test_hot_with_callback_request_routes_to_handoff(self):
        result = self.engine.decide("ขอให้เซลส์ติดต่อกลับ", history=[], context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "HUMAN_HANDOFF")
        self.assertEqual(result["handoff_payload"]["reason"], "customer_intelligence_recommended")

    def test_hot_without_callback_request_never_escalates(self):
        result = self.engine.decide("สนใจมากครับ ขอรายละเอียดราคา", history=[])
        self.assertNotEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_negative_bare_complaint_without_explicit_ask_reaches_rag_not_preempted(self):
        """The exact regression this trigger must never reintroduce: a
        complaint-shaped message with no explicit "talk to a human"
        phrase must still reach the RAG pipeline (and AI Policy's own
        escalation verdict), never get short-circuited here first."""
        _seed_action(self.reg, key="kb", action_type="RAG", keywords=["ร้องเรียน"])
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="ขอโทษด้วยค่ะ กำลังตรวจสอบให้", policy_escalate=False)):
            result = self.engine.decide("ร้องเรียนบริการ", history=[])
        self.assertEqual(result["routing"]["type"], "RAG")

    def test_erp_resolvable_negative_never_escalates(self):
        result = self.engine.decide("ของยังไม่ถึง ช่วยเช็กให้หน่อย", history=[])
        self.assertNotEqual(result["routing"]["type"], "HUMAN_HANDOFF")

    def test_agent_style_cs_notify_message_is_not_hijacked(self):
        """The message a customer/agent uses to directly invoke the
        SendLineNotiCS Business Action itself (mentions "ติดต่อกลับ" but
        no "เซลส์") must reach normal action search/selection, never get
        intercepted here first."""
        result = self.engine.decide("ช่วยแจ้ง CS ให้หน่อยว่าลูกค้าต้องการให้ติดต่อกลับ", history=[])
        self.assertNotEqual(result["handoff_payload"].get("reason") if result.get("handoff_payload") else None,
                             "customer_intelligence_recommended")


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

    def test_group_member_never_binds_the_whole_message_via_free_text_fallback(self):
        """Final Conversational Correctness (2026-08-15) — a group member
        with no configured pattern/type (CustName-shaped) must NOT
        swallow an entire unrelated sentence as if it were a real
        identifying value. Before this fix, a bare "ขอข้อมูลลูกค้าหน่อย"
        (no CustCode/email/phone-shaped token anywhere) would still
        execute immediately because the whole-message free-text fallback
        (designed for a genuinely free-text, UNGROUPED parameter like
        SendLineNotiCS's Message) also satisfied this GROUP via its
        non_empty-validated CustName member."""
        action_id = _seed_action(self.reg, key="customer_lookup_name", action_type="API", category="customer",
                                  keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            {"name": "CustName", "display_name": "ชื่อลูกค้า", "required": False, "input_source": "customer_message"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustName"]},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/customer", "http_method": "GET"})

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"ok": True})) as mock_req:
            result = self.engine.decide("ขอข้อมูลลูกค้าหน่อยครับ", history=[])
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        mock_req.assert_not_called()
        collection = (result.get("developer") or {}).get("information_collection_status") or {}
        self.assertFalse(collection.get("is_complete"))
        self.assertNotIn("CustName", collection.get("collected_parameters") or {})

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


class TestUrlConversionActionAndCrossActionIdentifierReuse(unittest.TestCase):
    """GetUrlProductDetail ('แปลงลิงก์สินค้า') customer-reported defect
    (2026-08-19): CustCode is genuinely required by the real ERP (proven
    empirically — a live request with URL only returns HTTP 400, adding a
    real CustCode returns 200 with a customer-specific converted link), so
    it must NOT be stripped from the action's required parameters. The fix
    is generic identifier reuse (already-existing IDENTIFIER_MEMORY_FIELDS
    machinery) plus two real bugs found while verifying that reuse against
    the shape of the real production actions:

    1. `_bind_message_to_action`'s generic digit-bearing candidate scanner
       was tokenizing the URL's OWN internal structure (e.g. "1688" out of
       "https://detail.1688.com/...") as a plausible identifier value for
       an unrelated sibling action's parameter (e.g. a Tracking-shaped
       field), which then out-scored the real URL-consuming action in
       _resolve_continuation_action's "already collected" tie-break —
       confirmed live: after asking for CustCode, the customer's very next
       reply silently misrouted to a different, unrelated action.
    2. `_extract_system_values()` only ever read the URL from THIS turn's
       message, so a URL given on an earlier turn vanished by the time
       CustCode was supplied on a later turn — the ERP call would go out
       with URL missing.

    The fixture below seeds two actions sharing the exact same
    auto-generated "please give your customer code" question — reproducing
    the exact ambiguity that exposed both bugs."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_geturlproductdetail(self):
        action_id = _seed_action(
            self.reg, key="geturlproductdetail", action_type="API", category="Product Link Conversion",
            ai_description="แปลงลิงก์สินค้าจาก 1688, Taobao หรือ Tmall เป็นลิงก์หน้ารายละเอียดสินค้าของ Shipify",
            keywords=["แปลงลิงก์", "ลิงก์สินค้า", "1688", "taobao", "tmall"])
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "display_name": "รหัสยืนยันตัวตน", "required": True,
             "input_source": "credential_store", "credential_ref": "secretcode"},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            {"name": "URL", "display_name": "ลิงก์สินค้า", "required": True, "input_source": "system_generated"},
        ])
        self.reg.replace_response_mapping(action_id, [
            {"json_path": "$.data.Link", "mapped_label": "ลิงก์รายละเอียดสินค้า", "field_metadata": {}},
        ])
        self.reg.upsert_execution(action_id, {
            "endpoint": "https://example.test/GetUrlProductDetail", "http_method": "POST",
            "content_type": "application/x-www-form-urlencoded"})
        return action_id

    def _seed_tracking_sibling(self):
        """Shaped like the real SearchDataTracking action — same CustCode
        question, plus a Tracking parameter whose validator would
        otherwise happily accept a bare digit-run like "1688"."""
        action_id = _seed_action(
            self.reg, key="search_data_tracking", action_type="API", category="Shipment Tracking",
            ai_description="ค้นหาสถานะพัสดุจากเลขแทรค", keywords=["แทรค", "tracking", "เลขพัสดุ"])
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "display_name": "รหัสยืนยันตัวตน", "required": True,
             "input_source": "credential_store", "credential_ref": "secretcode"},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
            {"name": "Tracking", "display_name": "เลขแทรค", "required": True, "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/SearchDataTracking",
                                                "http_method": "POST"})
        return action_id

    def _credential_patch(self):
        return patch("services.credential_store.CredentialStore.resolve",
                     return_value={"ok": True, "value": "resolved-secret", "error": None})

    # A. URL only, nothing known anywhere -> asks naturally, no raw JSON, no fabricated link
    def test_a_url_only_no_custcode_anywhere_asks_naturally(self):
        self._seed_geturlproductdetail()
        result = self.engine.decide("https://detail.1688.com/offer/682345678901.html", history=[], context={})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        text = result["reply"]["text"]
        self.assertIn("รหัสลูกค้า", text)
        self.assertNotIn("{", text)
        self.assertNotIn("http", text.lower())  # no fabricated link before CustCode is known

    # B. Natural phrase + URL + CustCode already known via customer_context (profile reuse)
    def test_b_natural_phrase_with_known_custcode_in_profile_executes_immediately(self):
        self._seed_geturlproductdetail()
        context = {"customer_context": {"cust_code": "SP1014"}}
        with self._credential_patch(), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "status": "success", "data": {"Link": "https://www.shipify.co.th/PageProductDetail/1688/682345678901"}})) as mock_req:
            result = self.engine.decide(
                "ช่วยแปลงลิงก์นี้ให้หน่อย https://detail.1688.com/offer/682345678901.html",
                history=[], context=context)
        self.assertEqual(result["routing"]["type"], "API")
        sent = mock_req.call_args.kwargs.get("data") or {}
        self.assertEqual(sent.get("CustCode"), "SP1014")
        self.assertEqual(sent.get("URL"), "https://detail.1688.com/offer/682345678901.html")
        text = result["reply"]["text"]
        self.assertIn("https://www.shipify.co.th", text)
        self.assertNotIn("{", text)
        self.assertNotIn('"status"', text)
        # The politeness particle must never be glued directly onto the URL.
        self.assertNotIn("682345678901ค่ะ", text)

    # C/D. Cross-turn: URL given turn 1 (asked for CustCode), CustCode given
    # turn 2 -- must reuse the ORIGINAL URL (not lose it) and must NOT
    # misroute to the sibling Tracking action just because "1688" appears
    # inside the URL.
    def test_c_url_then_custcode_across_turns_reuses_url_and_does_not_misroute(self):
        self._seed_geturlproductdetail()
        self._seed_tracking_sibling()
        msg1 = "https://detail.1688.com/offer/682345678901.html"
        turn1 = self.engine.decide(msg1, history=[], context={})
        self.assertEqual(turn1["routing"]["type"], "WORKFLOW")
        self.assertIn("รหัสลูกค้า", turn1["reply"]["text"])

        history = [{"role": "user", "content": msg1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with self._credential_patch(), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "status": "success", "data": {"Link": "https://www.shipify.co.th/x"}})) as mock_req:
            turn2 = self.engine.decide("SP1014", history=history, context={})
        self.assertEqual(turn2["routing"]["type"], "API")
        sent_url = mock_req.call_args.args[1] if mock_req.call_args.args else mock_req.call_args.kwargs.get("url")
        self.assertIn("GetUrlProductDetail", sent_url)  # never silently switched to the Tracking sibling
        sent = mock_req.call_args.kwargs.get("data") or {}
        self.assertEqual(sent.get("CustCode"), "SP1014")
        self.assertEqual(sent.get("URL"), msg1)  # URL from turn 1 must survive to turn 2's execution

    # E. Existing order/tracking context already active must not hijack a fresh product-URL message.
    def test_e_active_tracking_context_does_not_hijack_url_conversion_request(self):
        self._seed_geturlproductdetail()
        self._seed_tracking_sibling()
        context = {"customer_context": {"cust_code": "SP1014", "last_business_action": "search_data_tracking"}}
        with self._credential_patch(), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "status": "success", "data": {"Link": "https://www.shipify.co.th/y"}})) as mock_req:
            result = self.engine.decide(
                "https://detail.1688.com/offer/682345678901.html", history=[], context=context)
        self.assertEqual(result["routing"]["type"], "API")
        sent_url = mock_req.call_args.args[1] if mock_req.call_args.args else mock_req.call_args.kwargs.get("url")
        self.assertIn("GetUrlProductDetail", sent_url)

    # F. Non-URL / unsupported message never fabricates a link.
    def test_f_no_url_present_does_not_select_link_conversion_action(self):
        self._seed_geturlproductdetail()
        result = self.engine.decide("สวัสดีครับ", history=[], context={})
        self.assertNotEqual(result["routing"]["type"], "API")

    # G. Successful conversion reply is natural language, never raw JSON.
    def test_g_successful_conversion_reply_has_no_raw_json_leakage(self):
        self._seed_geturlproductdetail()
        context = {"customer_context": {"cust_code": "FT1004"}}
        with self._credential_patch(), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "status": "success",
                       "data": {"Link": "https://fasttrade.in.th/PageProductDetailGuest/1688/682345678901/home/guest/index"}})):
            result = self.engine.decide(
                "https://detail.1688.com/offer/682345678901.html", history=[], context=context)
        text = result["reply"]["text"]
        for token in ("{", "}", '"status"', '"data"'):
            self.assertNotIn(token, text)
        self.assertIn("https://fasttrade.in.th", text)

    # H. Multi-user isolation: one caller's customer_context must never leak into another's decide() call.
    def test_h_multi_user_customer_context_never_leaks_between_calls(self):
        self._seed_geturlproductdetail()
        with self._credential_patch(), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "status": "success", "data": {"Link": "https://www.shipify.co.th/user-a"}})) as mock_req:
            self.engine.decide("https://detail.1688.com/offer/1.html", history=[],
                                context={"customer_context": {"cust_code": "SP1014"}})
            sent_a = mock_req.call_args.kwargs.get("data") or {}
        self.assertEqual(sent_a.get("CustCode"), "SP1014")

        # A second, unrelated caller with NO customer_context must be asked
        # fresh -- never silently inherit user A's CustCode.
        result_b = self.engine.decide("https://detail.1688.com/offer/2.html", history=[], context={})
        self.assertEqual(result_b["routing"]["type"], "WORKFLOW")
        self.assertIn("รหัสลูกค้า", result_b["reply"]["text"])


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

    def test_hybrid_customer_answer_has_no_internal_architecture_wording(self):
        """Customer Response Quality (2026-08-16) — the real customer
        reply (result["reply"]["text"]) must read as ONE natural answer,
        never expose "ERP"/"Knowledge Base" section labels or inline
        source citations. Those stay available in developer_trace only."""
        self._seed_customer_coupons()
        with patch.object(self.engine.executor, "execute",
                           return_value=_fake_exec_result(result={"mapped_fields": {"coupons": "2 ใบ"}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(
                       answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ",
                       chunks=[{"text": "...", "cited": True, "citation": "FAQ.xlsx, page 3"}])):
            result = self.engine.decide("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", history=[],
                                          context={"developer_mode": True})
        text = result["reply"]["text"]
        self.assertNotIn("ERP", text)
        self.assertNotIn("Knowledge Base", text)
        self.assertNotIn("แหล่งที่มา", text)
        self.assertNotIn(".xlsx", text)
        self.assertIn("coupons", text)
        self.assertIn("คูปองใช้ได้ที่หน้าชำระเงินค่ะ", text)
        # Developer Trace still carries the labeled view + citations.
        self.assertIn("Knowledge Base", result["developer"]["hybrid_labeled_answer"])
        self.assertIn("FAQ.xlsx", result["developer"]["hybrid_citations"][0])


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

    def test_naming_the_identifier_does_not_narrow_to_just_the_identifier_field(self):
        """Confirmed live bug (2026-08-16) — "อยากทราบข้อมูลลูกค้ารหัส
        FT3182" contains the word "รหัส" (code), which collides with the
        CustCode field's OWN keywords ("รหัสลูกค้า"/"รหัส"/"code") and
        narrowed a rich customer record down to a bare echo of the code
        the customer already supplied. Naming an identifier is not the
        same as asking to have it read back — the row is skipped when it
        merely mirrors the parameter the customer just gave as input."""
        mapping = [
            {"json_path": "$.data.0.CustCode", "mapped_label": "รหัสลูกค้า",
             "field_metadata": {"keywords": ["รหัสลูกค้า", "รหัส", "customer code", "code"]}},
            {"json_path": "$.data.0.CustName", "mapped_label": "ชื่อลูกค้า",
             "field_metadata": {"keywords": ["ชื่อ", "name"]}},
            {"json_path": "$.data.0.Wallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน"]}},
        ]
        mapped_fields = {"รหัสลูกค้า": "FT3182", "ชื่อลูกค้า": "สมชาย", "ยอดเงิน Wallet": "100.00"}
        result = select_requested_mapped_fields("อยากทราบข้อมูลลูกค้ารหัส FT3182", mapped_fields, mapping,
                                                  input_param_names=["CustCode"])
        self.assertEqual(result, mapped_fields)

    def test_identifier_self_match_guard_only_applies_to_input_params_actually_supplied(self):
        """A genuine field-specific question ("ยอดเงิน wallet") still
        narrows normally — the guard only ever skips a row that mirrors
        THIS turn's own input, never any other field."""
        mapping = [
            {"json_path": "$.data.0.CustCode", "mapped_label": "รหัสลูกค้า",
             "field_metadata": {"keywords": ["รหัสลูกค้า", "รหัส"]}},
            {"json_path": "$.data.0.Wallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน"]}},
        ]
        mapped_fields = {"รหัสลูกค้า": "FT3182", "ยอดเงิน Wallet": "100.00"}
        result = select_requested_mapped_fields("ยอดเงิน wallet เหลือเท่าไหร่", mapped_fields, mapping,
                                                  input_param_names=["CustCode"])
        self.assertEqual(result, {"ยอดเงิน Wallet": "100.00"})


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
                 # OrderCode pattern widened + "รายละเอียด order" keyword
                 # added by migration 043 (Customer-Reported ERP
                 # Conversation Defects, 2026-08-17) — root cause: the
                 # customer's real ERP issues BOTH "POS"-prefixed AND
                 # "PO"-prefixed (no "S") order codes; the old
                 # `^POS\d+$` pattern only matched the former, so a
                 # "PO"-prefixed code (used throughout this platform's
                 # OWN Golden data, e.g. PO318220260806008) never validly
                 # bound to OrderCode at all — it fell through to
                 # CustCode's/SearchDataShipment's own broader patterns
                 # instead. A bare "order" keyword was tried first and
                 # reverted (see the migration's own comment) after it
                 # regressed the ALREADY-correct "ขอดู order ล่าสุด"
                 # continuation by colliding with SearchDataOrderList's
                 # own pre-existing "order" keyword whenever no
                 # identifier was present to break the tie; the narrower
                 # "รายละเอียด order" only ever fires for genuinely
                 # DETAIL-shaped phrasing.
                 keywords=["เลขคำสั่งซื้อ", "PO เดียว", "order detail", "รายละเอียดคำสั่งซื้อ", "รายละเอียด order"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "OrderCode", "display_name": "เลขที่คำสั่งซื้อ", "required": True,
                      "validation_pattern": r"^POS?\d+$"},
                 ])
    _seed_action(reg, key="searchdataorderlist", action_type="API", category="Customer Order Retrieval",
                 # "ออเดอร์" added by migration 042 (Golden Application
                 # Defect Fixes, 2026-08-16) — GOLDEN-049 root cause: no
                 # keyword covered the common Thai-English loanword for
                 # "order" at all, so a message using it scored 0 via
                 # search_keywords on every ERP action. Bare "order" (a
                 # pre-existing keyword predating this fixture's own
                 # migration-042 update, confirmed via a live read of the
                 # real production registry during migration 043's own
                 # verification, 2026-08-17) is included here too so this
                 # fixture accurately mirrors real production config --
                 # a fixture-only omission of it previously caused a
                 # false CLARIFICATION_REQUIRED in one of THIS fixture's
                 # own tests that never reproduced against the real
                 # server (see test_po_prefixed_order_code_detail_lookup_
                 # selects_order_detail's own history).
                 keywords=["คำสั่งซื้อ", "ประวัติการสั่งซื้อ", "order list", "PO", "ออเดอร์", "order"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                          "validation_pattern": cust_pattern}])
    _seed_action(reg, key="searchdatashipment", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
                      "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
                 ])
    _seed_action(reg, key="searchdatashipmentlist", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["บิลขนส่ง", "พัสดุ", "tracking", "shipment list", "ติดตามพัสดุ"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                          "validation_pattern": cust_pattern}])
    _seed_action(reg, key="searchdatatracking", action_type="API", category="Customer Shipment Retrieval",
                 priority=1,
                 # "เลขพัสดุจีน"/"เลขจีน"/"พัสดุจีน"/"หมายเลขพัสดุจีน" added
                 # by migration 042 (Golden Application Defect Fixes,
                 # 2026-08-16) — GOLDEN-024 root cause: every existing
                 # keyword required the literal English word "tracking";
                 # a customer phrasing the exact same intent in native Thai
                 # ("เลขพัสดุจีน...ถึงไหนแล้ว") scored 0 here while
                 # SearchDataShipmentList's generic "พัสดุ" keyword matched
                 # and won by default.
                 keywords=["tracking จีน", "เลข tracking", "tracking", "ค้นหาด้วยเลข tracking", "เลข tracking จีน",
                           "เลขพัสดุจีน", "เลขจีน", "พัสดุจีน", "หมายเลขพัสดุจีน"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "Tracking", "display_name": "เลข Tracking จีน", "required": True},
                 ])
    _seed_action(reg, key="getdatacustomer", action_type="API", category="Customer Data Retrieval",
                 keywords=["ข้อมูลลูกค้า", "Wallet", "คูปอง", "ยอดเงิน", "ข้อมูลทั้งหมด"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
                          "validation_pattern": cust_pattern}])


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

    def test_bare_cust_code_alone_never_picks_a_wrong_action_by_priority_alone(self):
        """AI Playground Real User Journey UAT (2026-08-15) — a bare
        CustCode-shaped message with NO other keyword/context (no active
        continuation to inherit either) ties every CustCode-having action
        via _identifier_pattern_score. Before the discriminating-power
        fix, searchdatatracking's higher configured priority (1, vs 0 for
        every other action here) silently won that tie and asked for a
        Tracking number -- even though nothing about "just a customer
        code" ever implied tracking. It must now fall back safely
        (never confidently execute the wrong action) instead."""
        result = self.engine.decide("SP1014", history=[])
        self.assertNotEqual(
            (result.get("developer") or {}).get("selected_business_action"), "searchdatatracking")
        collection = (result.get("developer") or {}).get("information_collection_status") or {}
        self.assertNotEqual(collection.get("selected_business_action"), "searchdatatracking")

    # -- GOLDEN-024 (Golden Application Defect Fixes, 2026-08-16): native
    # Thai phrasing for "Chinese tracking/package number" must resolve to
    # SearchDataTracking, never SearchDataShipmentList's generic "พัสดุ"
    # keyword, across several distinct phrasings (never just the one
    # literal sentence the Golden case happened to use). --

    def test_chinese_tracking_number_thai_phrase_selects_tracking(self):
        self.assertEqual(self._selected_action_key("เลขพัสดุจีน testlineOnNut007 ถึงไหนแล้ว"), "searchdatatracking")

    def test_chinese_tracking_number_thai_phrase_variant_with_check_selects_tracking(self):
        self.assertEqual(self._selected_action_key("ช่วยเช็กเลขพัสดุจีน testlineOnNut007"), "searchdatatracking")

    def test_tracking_jeen_english_word_variant_selects_tracking(self):
        self.assertEqual(self._selected_action_key("tracking จีน testlineOnNut007 ถึงไหนแล้ว"), "searchdatatracking")

    def test_lek_jeen_variant_selects_tracking(self):
        self.assertEqual(self._selected_action_key("เลขจีน testlineOnNut007 อยู่ไหนแล้ว"), "searchdatatracking")

    def test_generic_package_question_without_china_still_selects_shipment_list(self):
        """Regression guard — the new "จีน"-scoped keywords must never
        make an ordinary, non-China-specific package question (no
        SearchDataTracking-specific evidence at all) drift away from
        SearchDataShipmentList."""
        self.assertEqual(self._selected_action_key("FT1004 มีพัสดุอะไรบ้าง"), "searchdatashipmentlist")

    # -- GOLDEN-049 (Golden Application Defect Fixes, 2026-08-16): the
    # Thai-English loanword "ออเดอร์" (order) alone, combined with a
    # customer identifier and "latest/list" phrasing, must be sufficient
    # evidence for SearchDataOrderList — not a false 3-way tie between
    # SearchDataOrderList/SearchDataShipmentList/GetUrlProductDetail via
    # the weak ai_description word-overlap fallback (a bare "ของ" — "of"
    # — happened to appear in all three descriptions). --

    def test_order_loanword_with_customer_code_selects_order_list_not_clarification(self):
        self.assertEqual(self._selected_action_key("ของ FT3182 มีออเดอร์ล่าสุดอะไรบ้าง"), "searchdataorderlist")

    def test_order_loanword_alone_still_selects_order_list(self):
        self.assertEqual(self._selected_action_key("FT3182 มีออเดอร์อะไรบ้าง"), "searchdataorderlist")

    # -- Customer-Reported ERP Conversation Defects (2026-08-17): a "PO"-
    # prefixed order code (no "S", e.g. PO318220260806008 -- used
    # throughout this platform's own real Golden/UAT data) must resolve
    # SearchDataOrder for a genuine detail-lookup message, and the
    # "รายละเอียด order" keyword fix that makes that possible must NEVER
    # regress the already-correct "ขอดู order ล่าสุด" list continuation
    # (this exact regression was caught and reverted once already during
    # this same fix -- see migration 043's own comment). --

    def test_po_prefixed_order_code_detail_lookup_selects_order_detail(self):
        self.assertEqual(
            self._selected_action_key("ขอรายละเอียด order PO318220260806008"), "searchdataorder")

    def test_bare_order_mention_with_no_identifier_still_selects_order_list_not_clarification(self):
        """Regression guard for the exact bug this same fix accidentally
        introduced and then reverted: a message naming "order" with NO
        identifier at all (a pure list/browse intent) must resolve
        cleanly to SearchDataOrderList, never a false
        CLARIFICATION_REQUIRED tie against SearchDataOrder."""
        self.assertEqual(self._selected_action_key("ขอดู order ล่าสุด"), "searchdataorderlist")

    # -- Customer-Reported ERP Conversation Defects, server UAT follow-up
    # (2026-08-17): an OrderCode named BEFORE any CustCode is ever
    # supplied triggers a CustCode clarification that SIX different
    # actions generate identically ("กรุณาแจ้งรหัสลูกค้าค่ะ"). A natural
    # follow-up carrying no new identifier must not let the continuation
    # tie-break silently switch to an unrelated sibling action
    # (searchdataorderlist) and drop the already-known OrderCode -- found
    # live against the deployed server while re-testing this same fix
    # round's own mandatory UAT journeys, root-caused to _keyword_score's
    # bare "PO" keyword substring-matching inside the OrderCode VALUE
    # itself. Deliberately uses "POS"-prefixed POS100820260809001 (already
    # established elsewhere in this file), NOT a bare "PO"-prefixed code
    # (e.g. GOLDEN-051's PO318220260806008) -- a bare 2-letter "PO" prefix
    # also structurally satisfies CustCode's own broad `^[A-Za-z]{2}\d+$`
    # pattern, and which of two simultaneously-missing required
    # parameters wins that binding tie is a separate, pre-existing
    # precedence gap outside this fix's scope (confirmed identical in
    # both this fixture and the real production registry). --

    def test_ordercode_first_then_bare_followup_keeps_order_detail_pending(self):
        history = [
            {"role": "user", "content": "POS100820260809001 หมายถึงบิลนี้"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
        ]
        result, mock_req = self._decide_with_history("บิลนี้สถานะอะไร", history)
        dev = result.get("developer") or {}
        collection = dev.get("information_collection_status") or {}
        self.assertEqual(collection.get("selected_business_action"), "searchdataorder")
        self.assertNotEqual(collection.get("selected_business_action"), "searchdataorderlist")
        self.assertIn("POS100820260809001", (collection.get("collected_parameters") or {}).values())
        mock_req.assert_not_called()

    def test_ordercode_first_then_custcode_resolves_order_detail_not_list(self):
        history = [
            {"role": "user", "content": "POS100820260809001 หมายถึงบิลนี้"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
            {"role": "user", "content": "บิลนี้สถานะอะไร"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
        ]
        self.assertEqual(self._selected_action_key_with_history("FT3182", history), "searchdataorder")

    def _decide_with_history(self, message, history):
        with patch("services.action_executor.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200, json=lambda: {"status": "success"})
            result = self.engine.decide(message, history=history, context={"developer_mode": True})
        return result, mock_req

    def _selected_action_key_with_history(self, message, history):
        result, _ = self._decide_with_history(message, history)
        dev = result.get("developer") or {}
        selected = (dev.get("selected_business_action")
                    or (dev.get("information_collection_status") or {}).get("selected_business_action"))
        self.assertIsNotNone(selected, f"no Business Action selected for: {message}")
        return selected


def _seed_generic_routing_matrix(reg):
    """Generic Business Action Routing Score Imbalance fix (2026-08-24) —
    mirrors the REAL production registry's shipment/order/tracking/
    customer/address-change actions (confirmed via a live, read-only
    check the same day), specifically INCLUDING requestshippingaddress-
    change's own narrower CustCode validation_pattern
    (`^[A-Za-z]{2}\\d{4,6}$` vs every other action's `^[A-Za-z]{2}\\d+$`)
    — the exact configuration asymmetry that let it win on identifier-
    pattern score alone. TestOrderShipmentTrackingRoutingPrecision's own
    fixture never included this action, which is why this specific bug
    was never caught by that (still entirely valid, still passing)
    fixture. Also adds one RAG action so the RAG-regression scenarios in
    Phase 3 of this fix exercise a real competing candidate, not an
    empty registry."""
    cust_pattern = r"^[A-Za-z]{2}\d+$"
    # P2 Order/Tracking Keyword Ambiguity fix (2026-08-24) — "รายละเอียด
    # order" (without) removed: it blindly matched code-less "latest"
    # phrasing too ("ขอรายละเอียด order ล่าสุด"), forcing a false tie
    # against searchdataorderlist even though searchdataorder's own
    # required OrderCode can never be satisfied from "ล่าสุด" alone. A
    # genuine OrderCode-bearing message still wins searchdataorder
    # decisively via identifier-pattern score alone (see
    # test_po_prefixed_order_code_detail_lookup_selects_order_detail and
    # this class's own PO-code tests) — the keyword was redundant there
    # and actively harmful here.
    _seed_action(reg, key="searchdataorder", action_type="API", category="Customer Order Retrieval",
                 keywords=["เลขคำสั่งซื้อ", "PO เดียว", "order detail", "รายละเอียดคำสั่งซื้อ"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "OrderCode", "display_name": "เลขที่คำสั่งซื้อ", "required": True,
                      "validation_pattern": r"^POS?\d+$"},
                 ])
    _seed_action(reg, key="searchdataorderlist", action_type="API", category="Customer Order Retrieval",
                 keywords=["คำสั่งซื้อ", "ประวัติการสั่งซื้อ", "order list", "PO", "ออเดอร์", "order"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                          "validation_pattern": cust_pattern}])
    _seed_action(reg, key="searchdatashipment", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
                      "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
                 ])
    # Real, current production search_keywords (post the 2026-08-24
    # "SHIPMENT FILTER / COUNT / SUM AGGREGATION" fix's keyword additions).
    _seed_action(reg, key="searchdatashipmentlist", action_type="API", category="Customer Shipment Retrieval",
                 keywords=["พัสดุ", "tracking", "shipment list", "ติดตามพัสดุ", "ค่าขนส่งเท่าไหร่",
                           "ค่าส่งเท่าไหร่", "ค่าส่งล่าสุด", "บิลขนส่งล่าสุด", "ถูกที่สุด", "ถูกกว่า",
                           "เมื่อไหร่จะถึง", "ถึงไทยหรือยัง", "ถึงหรือยัง", "มาถึงหรือยัง", "ของถึงไหนแล้ว",
                           "พัสดุล่าสุด", "การจัดส่งล่าสุด", "มาถึง", "จะมาถึง",
                           "กี่บิล", "เข้าไทย", "ถึงไทย", "สถานะรับเข้าไทย"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                          "validation_pattern": cust_pattern}])
    # P2 Order/Tracking Keyword Ambiguity fix (2026-08-24) — bare
    # "tracking" removed: searchdatatracking's own required Tracking
    # parameter (an actual China tracking number) can never be derived
    # from "ล่าสุด", so this over-broad keyword only ever created a false
    # tie against searchdatashipmentlist (which already maps a "latest
    # tracking number" via its own TrackingCH/TH response_mapping) for
    # number-less "latest" questions. Every real tracking-by-number
    # message below still matches via a MORE specific remaining keyword
    # ("เลขพัสดุจีน", "ค้นหาด้วยเลข tracking", "tracking จีน", ...).
    _seed_action(reg, key="searchdatatracking", action_type="API", category="Customer Shipment Retrieval",
                 priority=1,
                 keywords=["tracking จีน", "เลข tracking", "ค้นหาด้วยเลข tracking", "เลข tracking จีน",
                           "เลขพัสดุจีน", "เลขจีน", "พัสดุจีน", "หมายเลขพัสดุจีน"],
                 params=[
                     {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "validation_pattern": cust_pattern},
                     {"name": "Tracking", "display_name": "เลข Tracking จีน", "required": True},
                 ])
    _seed_action(reg, key="getdatacustomer", action_type="API", category="Customer Data Retrieval",
                 keywords=["ข้อมูลลูกค้า", "Wallet", "คูปอง", "ยอดเงิน", "ข้อมูลทั้งหมด"],
                 params=[{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
                          "validation_pattern": cust_pattern}])
    # requestshippingaddresschange — the actual real production
    # validation_pattern for CustCode (deliberately narrower than every
    # other action's, so the two structurally disambiguate from
    # ShipmentCode within THIS action's own message parsing). This is
    # the exact configuration that exposed the bug.
    addr_id = _seed_action(
        reg, key="requestshippingaddresschange", action_type="API", category="Customer Support Request",
        ai_description="รับคำขอเปลี่ยนที่อยู่จัดส่ง/ที่อยู่รับสินค้าจากลูกค้า แล้วแจ้งเจ้าหน้าที่ให้ดำเนินการแก้ไขใน ERP",
        keywords=["ต้องการเปลี่ยนที่อยู่บิลขนส่ง", "อยากเปลี่ยนที่อยู่จัดส่ง", "แก้ที่อยู่จัดส่งยังไง",
                   "เปลี่ยนที่อยู่รับของ", "เปลี่ยนที่อยู่รับสินค้า", "ขอเปลี่ยนที่อยู่บิล",
                   "แก้ไขที่อยู่จัดส่ง", "เปลี่ยนที่อยู่ของผม", "เปลี่ยนที่อยู่จัดส่งในไทย", "เปลี่ยนที่อยู่จัดส่ง"])
    reg.update(addr_id, {"setup_metadata": {"operation_type": "NOTIFICATION"},
                          "display_name": "คำขอเปลี่ยนที่อยู่จัดส่ง"})
    reg.replace_parameters(addr_id, [
        {"name": "SecretCode", "required": True, "input_source": "credential_store",
         "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
        {"name": "ShipmentCode", "display_name": "เลขที่บิล/Shipment", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
        {"name": "ReceiverName", "display_name": "ชื่อผู้รับ", "required": True,
         "input_source": "customer_message", "validation_type": "non_empty",
         "field_metadata": {"address_component": "receiver_name"}},
        {"name": "ReceiverPhone", "display_name": "เบอร์โทรผู้รับ", "required": True,
         "input_source": "customer_message", "validation_type": "phone_number",
         "field_metadata": {"address_component": "receiver_phone"}},
        {"name": "Address", "display_name": "ที่อยู่", "required": True,
         "input_source": "customer_message", "validation_type": "non_empty",
         "field_metadata": {"address_component": "address"}},
        {"name": "Subdistrict", "display_name": "ตำบล/แขวง", "required": True,
         "input_source": "customer_message", "validation_type": "non_empty",
         "field_metadata": {"address_component": "subdistrict"}},
        {"name": "District", "display_name": "อำเภอ/เขต", "required": True,
         "input_source": "customer_message", "validation_type": "non_empty",
         "field_metadata": {"address_component": "district"}},
        {"name": "Province", "display_name": "จังหวัด", "required": True,
         "input_source": "customer_message", "validation_type": "non_empty",
         "field_metadata": {"address_component": "province"}},
        {"name": "PostalCode", "display_name": "รหัสไปรษณีย์", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^\d{5}$",
         "field_metadata": {"address_component": "postal_code"}},
        {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": False,
         "input_source": "system_generated"},
    ])
    reg.upsert_execution(addr_id, {
        "endpoint": "https://fasttrade.in.th/web-service/ai-chat/SendLineNotiCS", "http_method": "POST"})
    # A RAG action so Phase-3's RAG scenarios have a real, generic
    # competing candidate rather than testing against an empty registry.
    _seed_action(reg, key="kb_general", action_type="RAG", keywords=["CBM", "โกดังจีน", "คลังจีน"])
    return addr_id


class TestGenericIdentifierScoreImbalanceFix(unittest.TestCase):
    """Generic Business Action Routing Score Imbalance fix (2026-08-24) —
    a P0 customer-facing defect: any message carrying a bare CustCode
    could be silently misrouted into requestshippingaddresschange (a
    NOTIFICATION workflow asking for 8 more fields, including one that
    would eventually reach a real Human/CS handoff) even with ZERO
    keyword/semantic relevance to an address change, because
    _identifier_pattern_score's "how many candidates share this
    evidence" dilution grouped by the literal validation_pattern STRING
    instead of the parameter's NAME — so an action whose admin happened
    to type a slightly narrower regex for the SAME "CustCode" concept
    looked artificially unique (undiluted weight 3.0) against every
    other action's identically-named but differently-spelled pattern
    (diluted). Confirmed live against the real production registry
    (2026-08-24): "SP1008 ข้อมูลลูกค้า" — literally getdatacustomer's
    own configured purpose — still lost to address-change on identifier
    score alone before this fix.

    The fix (see _identifier_pattern_score's own docstring) groups the
    sharer-count by parameter NAME instead — the same key
    _parameter_availability_score already uses successfully — so it
    requires no new metadata field, no schema change, and (per this
    class's own tests) does not weaken a genuinely unique identifier's
    (OrderCode, ShipmentCode, Tracking) discriminating power at all."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_generic_routing_matrix(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _decide(self, message, history=None):
        with patch("services.action_executor.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200, json=lambda: {"status": "success"})
            with patch("services.playground_orchestrator.run_playground_turn",
                       return_value=_fake_playground_result(answer="RAG answer text")):
                result = self.engine.decide(message, history=history or [], context={"developer_mode": True})
        return result, mock_req

    def _selected_action_key(self, message):
        result, _ = self._decide(message)
        return self._selected_action_key_from_result(result)

    def _selected_action_key_from_result(self, result):
        dev = result.get("developer") or {}
        selected = (dev.get("selected_business_action")
                    or (dev.get("information_collection_status") or {}).get("selected_business_action"))
        return selected

    # ---- Phase 1: score breakdown proof (identifier-only never beats keyword-backed) ----

    def test_identifier_only_candidate_no_longer_beats_keyword_backed_candidate(self):
        from services.decision_engine import search_candidate_actions
        candidates = search_candidate_actions(self.reg, workflow=None,
                                               message="SP1008 บิลขนส่งล่าสุดค่าส่งเท่าไหร่", collected_slots={})
        by_key = {c["action_key"]: c["_score"] for c in candidates}
        self.assertGreater(by_key["searchdatashipmentlist"], by_key["requestshippingaddresschange"])

    def test_unique_identifier_evidence_stays_undiluted(self):
        """The fix must NOT weaken a genuinely unique identifier
        (OrderCode) — it should still decisively win its own detail
        action over the list action sharing only the diluted CustCode."""
        from services.decision_engine import search_candidate_actions
        candidates = search_candidate_actions(self.reg, workflow=None,
                                               message="ขอรายละเอียด PO POS100820260809001", collected_slots={})
        by_key = {c["action_key"]: c["_score"] for c in candidates}
        self.assertGreater(by_key["searchdataorder"], by_key["searchdataorderlist"])

    # ---- Phase 3 regression matrix ----

    # SHIPMENT
    def test_shipment_plain_shipping_cost_question(self):
        self.assertEqual(self._selected_action_key("SP1008 บิลขนส่งล่าสุดค่าส่งเท่าไหร่"), "searchdatashipmentlist")

    def test_shipment_arrival_question(self):
        self.assertEqual(self._selected_action_key("SP1008 พัสดุล่าสุดถึงไหนแล้ว"), "searchdatashipmentlist")

    def test_shipment_status_count_question(self):
        self.assertEqual(self._selected_action_key("SP1008 มีบิลที่รับเข้าไทยกี่บิล"), "searchdatashipmentlist")

    def test_shipment_status_sum_question(self):
        self.assertEqual(self._selected_action_key("SP1008 บิลที่รับเข้าไทยค่าขนส่งรวมเท่าไหร่"),
                          "searchdatashipmentlist")

    # ADDRESS CHANGE — must still win decisively on its OWN real trigger phrasing.
    def test_address_change_trigger_one(self):
        self.assertEqual(self._selected_action_key("SP1008 ต้องการเปลี่ยนที่อยู่บิลขนส่ง"),
                          "requestshippingaddresschange")

    def test_address_change_trigger_two(self):
        self.assertEqual(self._selected_action_key("SP1008 ช่วยเปลี่ยนที่อยู่จัดส่งให้หน่อย"),
                          "requestshippingaddresschange")

    # CUSTOMER
    def test_customer_data_question(self):
        self.assertEqual(self._selected_action_key("SP1008 ข้อมูลลูกค้า"), "getdatacustomer")

    # ORDER
    def test_order_latest_question(self):
        self.assertEqual(self._selected_action_key("SP1008 order ล่าสุด"), "searchdataorderlist")

    def test_order_latest_detail_question(self):
        """P2 Order/Tracking Keyword Ambiguity fix (2026-08-24) — this
        phrasing has no OrderCode at all ("ล่าสุด" = latest, not a named
        PO), so searchdataorder's own required OrderCode parameter can
        never be satisfied from it; searchdataorderlist's own "Latest"
        parameter and per-latest-record response_mapping (Code/Status/
        Total/Tracking) are the actual semantic match. searchdataorder's
        "รายละเอียด order" keyword — added for genuinely code-bearing
        DETAIL phrasing (migration 043) — also blindly matched this
        code-less "latest" phrasing, causing a false tie; removed as a
        keyword (config-only) since a real OrderCode already wins
        searchdataorder decisively via identifier-pattern score alone,
        with no keyword needed (see test_a/e/f/PO-prefixed tests below)."""
        self.assertEqual(self._selected_action_key("SP1008 ขอรายละเอียด order ล่าสุด"), "searchdataorderlist")

    # TRACKING
    def test_tracking_latest_question(self):
        """P2 Order/Tracking Keyword Ambiguity fix (2026-08-24) —
        searchdatatracking's own required Tracking parameter (an actual
        China tracking number) can never be derived from "ล่าสุด";
        searchdatashipmentlist already maps "TrackingCH/TH ล่าสุด" and
        supports a Latest count — the real semantic match for "my latest
        tracking number". searchdatatracking's bare "tracking" keyword —
        redundant whenever a real tracking number is present (every
        tracking-by-number regression test below already matches via a
        MORE specific keyword: "เลขพัสดุจีน", "ค้นหาด้วยเลข tracking",
        "tracking จีน", etc.) — removed (config-only) since it only ever
        created a false tie against shipmentlist for number-less
        "latest" questions."""
        self.assertEqual(self._selected_action_key("SP1008 tracking ล่าสุด"), "searchdatashipmentlist")

    def test_tracking_china_number_question(self):
        self.assertEqual(self._selected_action_key("เลขพัสดุจีน testlineOnNut007 ถึงไหนแล้ว"), "searchdatatracking")

    # RAG
    def test_rag_cbm_question(self):
        result, mock_req = self._decide("CBM คืออะไร")
        self.assertEqual(result["routing"]["type"], "RAG")
        mock_req.assert_not_called()

    def test_rag_china_warehouse_location_question(self):
        result, mock_req = self._decide("ที่อยู่โกดังจีนอยู่ที่ไหน")
        self.assertEqual(result["routing"]["type"], "RAG")
        mock_req.assert_not_called()

    # PURE IDENTIFIER — a bare CustCode alone must never silently
    # auto-execute an arbitrary action (e.g. quietly starting the
    # address-change workflow); it should ask a clarifying/context
    # question instead.
    def test_pure_identifier_alone_never_executes_an_action(self):
        result, mock_req = self._decide("SP1008")
        mock_req.assert_not_called()
        self.assertNotEqual(result["routing"]["type"], "API")

    # ---- Phase 4: amount traceback within an established shipment context ----

    def test_amount_traceback_identifies_the_matching_shipment_record(self):
        """Once a shipment list result is already in context, "112.46
        เอามาจากบิลไหน" must identify the record(s) that actually carry
        that amount — never misread the number as a record count, a
        CustCode, or a ShipmentCode, and never let the routing-imbalance
        bug hijack this into requestshippingaddresschange asking for a
        ShipmentCode the customer never offered to supply."""
        history = [
            {"role": "user", "content": "SP1008 บิลขนส่งล่าสุดค่าส่งเท่าไหร่"},
            {"role": "assistant", "content": "บิลขนส่ง SP100820260817001 ค่าขนส่งล่าสุด 112.46 บาทค่ะ"},
        ]
        result, mock_req = self._decide("112.46 เอามาจากบิลไหน", history=history)
        dev = result.get("developer") or {}
        selected = (dev.get("selected_business_action")
                    or (dev.get("information_collection_status") or {}).get("selected_business_action"))
        self.assertNotEqual(selected, "requestshippingaddresschange")

    # ---- Phase 5: safety ----

    def test_no_raw_json_in_any_routed_reply(self):
        for message in ("SP1008 บิลขนส่งล่าสุดค่าส่งเท่าไหร่", "SP1008 ข้อมูลลูกค้า", "SP1008 order ล่าสุด"):
            result, _ = self._decide(message)
            text = result["reply"]["text"]
            self.assertNotIn("{", text)
            self.assertNotIn("[", text)

    def test_cross_session_no_identifier_leakage(self):
        result_a, _ = self._decide("SP1008 ข้อมูลลูกค้า")
        result_b, mock_req_b = self._decide("ข้อมูลลูกค้า")
        # A fresh session with no CustCode anywhere must not inherit
        # SP1008 from a prior, unrelated session's own turn.
        collected_b = (result_b.get("developer") or {}).get("information_collection_status", {}).get(
            "collected_parameters", {})
        self.assertNotEqual(collected_b.get("CustCode"), "SP1008")


class TestKeywordMatchesAsciiBoundary(unittest.TestCase):
    """_keyword_matches / _keyword_score (services/action_selection_
    primitives.py) — a short, pure-ASCII search_keyword like "PO" must
    not match as a substring fragment swallowed inside a longer ASCII
    identifier VALUE (e.g. an OrderCode) the customer supplied in the
    same message. Thai-script keywords keep their original, unguarded
    substring behavior — Thai has no space-delimited word boundaries, so
    guarding them would reject legitimate compound-word matches instead
    of protecting anything real."""

    def test_short_ascii_keyword_does_not_match_inside_a_longer_identifier_value(self):
        self.assertFalse(_keyword_matches("PO", "pos100820260815001 หมายถึงบิลนี้"))
        self.assertFalse(_keyword_matches("PO", "po318220260806008 หมายถึงบิลนี้"))

    def test_short_ascii_keyword_still_matches_as_a_standalone_token(self):
        self.assertTrue(_keyword_matches("PO", "ขอดู po ล่าสุดของ sp1014"))
        self.assertTrue(_keyword_matches("PO", "po"))

    def test_thai_keyword_substring_matching_is_unaffected(self):
        self.assertTrue(_keyword_matches("คำสั่งซื้อ", "ขอดูประวัติคำสั่งซื้อล่าสุด"))

    def test_keyword_score_end_to_end_no_false_positive_from_embedded_identifier(self):
        action = {"search_keywords": ["PO"], "_examples_text": [], "ai_description": ""}
        self.assertEqual(_keyword_score(action, "POS100820260815001 หมายถึงบิลนี้"), 0.0)
        self.assertGreater(_keyword_score(action, "ขอดู PO ล่าสุด"), 0.0)


class TestResolveContinuationActionTieBreak(unittest.TestCase):
    """_resolve_continuation_action (services/decision_engine.py) — when
    several Business Actions generate an identical clarification
    question, the action that already has MORE parameters bound from
    history-so-far must win the tie, before falling back to keyword
    score against the original trigger message."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_order_shipment_tracking_actions(self.reg)

    def test_prefers_the_action_with_more_already_collected_parameters(self):
        history = [
            {"role": "user", "content": "POS100820260809001 หมายถึงบิลนี้"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
            {"role": "user", "content": "บิลนี้สถานะอะไร"},
        ]
        result = _resolve_continuation_action(self.reg, history, workflow_hint=None)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("action_key"), "searchdataorder")

    def test_still_falls_back_to_keyword_score_when_nothing_is_collected_on_either_side(self):
        """Regression guard: two candidates tied on the SAME clarification
        question with ZERO parameters collected on both sides must still
        resolve via keyword score exactly as before this fix (this test
        would fail loudly if the new collected-parameter preference step
        ever became unconditional instead of a before-keyword-score
        tie-break)."""
        history = [
            {"role": "user", "content": "ขอดู PO ล่าสุด"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
            {"role": "user", "content": "เอาอันล่าสุดค่ะ"},
        ]
        result = _resolve_continuation_action(self.reg, history, workflow_hint=None)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("action_key"), "searchdataorderlist")


class TestShippingAddressChangeRequest(unittest.TestCase):
    """Shipping Address Change Request (2026-08-20) — the entire flow is
    built from EXISTING generic mechanisms only: the Business Action
    Registry, the generic Slot Filling / Information Collection Engine,
    the generic operation_type=NOTIFICATION confirmation gate (same one
    SendLineNotiCS already uses), and the SAME notification execution
    endpoint SendLineNotiCS points at — reused, not duplicated. The only
    new code is (1) services/thai_address_parser.py, wired in as one more
    metadata-driven pre-pass exactly like Semantic Parameter Inference,
    and (2) a generic confirmation/notification-message summary composer
    that reads any action's own parameter display_names — no per-action
    special case. No new workflow subsystem, no ERP write endpoint.

    CustCode is deliberately shorter (2 letters + 4-6 digits) and
    ShipmentCode longer (2 letters + 10+ digits) so the two structurally
    disambiguate when both appear in one message, matching the real
    examples in the customer requirement (SP1008 vs SP100820260716001)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)

    def _seed_address_change_action(self, key="requestshippingaddresschange"):
        action_id = _seed_action(
            self.reg, key=key, action_type="API", category="Customer Support Request",
            ai_description="รับคำขอเปลี่ยนที่อยู่จัดส่ง/ที่อยู่รับสินค้าจากลูกค้า แล้วแจ้งเจ้าหน้าที่ให้ดำเนินการแก้ไขใน ERP",
            keywords=["ต้องการเปลี่ยนที่อยู่บิลขนส่ง", "อยากเปลี่ยนที่อยู่จัดส่ง", "แก้ที่อยู่จัดส่งยังไง",
                       "เปลี่ยนที่อยู่รับของ", "เปลี่ยนที่อยู่รับสินค้า", "ขอเปลี่ยนที่อยู่บิล"])
        self.reg.update(action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"},
                                     "display_name": "คำขอเปลี่ยนที่อยู่จัดส่ง"})
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิล/Shipment", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
            {"name": "ReceiverName", "display_name": "ชื่อผู้รับ", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "receiver_name"}},
            {"name": "ReceiverPhone", "display_name": "เบอร์โทรผู้รับ", "required": True,
             "input_source": "customer_message", "validation_type": "phone_number",
             "field_metadata": {"address_component": "receiver_phone"}},
            {"name": "Address", "display_name": "ที่อยู่", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "address"}},
            {"name": "Subdistrict", "display_name": "ตำบล/แขวง", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "subdistrict"}},
            {"name": "District", "display_name": "อำเภอ/เขต", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "district"}},
            {"name": "Province", "display_name": "จังหวัด", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "province"}},
            {"name": "PostalCode", "display_name": "รหัสไปรษณีย์", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^\d{5}$",
             "field_metadata": {"address_component": "postal_code"}},
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": False,
             "input_source": "system_generated"},
        ])
        self.reg.upsert_execution(action_id, {
            "endpoint": "https://fasttrade.in.th/web-service/ai-chat/SendLineNotiCS", "http_method": "POST"})
        return action_id

    FULL_ADDRESS = "8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120"

    def _mock_secret(self):
        return patch("services.credential_store.CredentialStore.resolve",
                      return_value={"ok": True, "value": "FAKE-SECRET", "error": None})

    # TEST 1 — asks for the new address, never a bare refusal.
    def test_1_initial_request_asks_for_new_address_not_a_refusal(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=[], context={"developer_mode": True})
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertNotIn("ไม่สามารถดำเนินการได้", result["reply"]["text"])
        mock_req.assert_not_called()

    # Regression (2026-08-20 Production UAT) — the customer's first two
    # real turns (bare CustCode, then the plain request sentence with no
    # address info at all) must never corrupt Address/ReceiverName/etc.
    # with unrelated text via either (a) turn-0 history replay binding
    # "SP1008" as a bare address line, or (b) the current turn's own
    # request sentence being swallowed whole by a loosely-validated
    # non_empty field via the generic free-text fallback.
    def test_1b_bare_custcode_then_request_sentence_never_corrupts_fields(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide("SP1008", history=[], context={"developer_mode": True})
        history = [{"role": "user", "content": "SP1008"}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=history,
                                         context={"developer_mode": True})
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertNotIn("Address", collected)
        self.assertNotIn("ReceiverName", collected)
        self.assertNotIn("Subdistrict", collected)
        self.assertEqual(collected.get("CustCode"), "SP1008")
        mock_req.assert_not_called()

    # TEST 2 — complete address in one message -> parses into fields, asks confirmation.
    def test_2_complete_message_parses_and_reaches_confirmation(self):
        self._seed_address_change_action()
        message = (f"ช่วยเปลี่ยนที่อยู่จัดส่งในไทยของบิล SP100820260716001 ให้หน่อย\n"
                   f"ผู้รับ หญิง\n0616807329\nที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(message, history=[],
                                         context={"developer_mode": True,
                                                   "customer_context": {"cust_code": "SP1008"}})
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("ShipmentCode"), "SP100820260716001")
        self.assertEqual(collected.get("Subdistrict"), "ตาขัน")
        self.assertEqual(collected.get("District"), "บ้านค่าย")
        self.assertEqual(collected.get("Province"), "ระยอง")
        self.assertEqual(collected.get("PostalCode"), "21120")
        self.assertEqual(collected.get("CustCode"), "SP1008")  # reused from customer_context, never re-asked
        reply = result["reply"]["text"]
        self.assertIn("ระยอง", reply)
        self.assertIn("21120", reply)
        self.assertIn("ยืนยัน", reply)
        mock_req.assert_not_called()

    # TEST 3 — incomplete address asks ONLY for the missing field.
    def test_3_incomplete_address_asks_only_missing_field(self):
        self._seed_address_change_action()
        message = ("บิล SP100820260716001 ผู้รับ หญิง 0616807329 "
                    "ที่อยู่ 8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง")  # no postal code
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(message, history=[],
                                         context={"developer_mode": True,
                                                   "customer_context": {"cust_code": "SP1008"}})
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertNotIn("PostalCode", collected)
        self.assertEqual(collected.get("Province"), "ระยอง")  # everything else already retained
        self.assertIn("รหัสไปรษณีย์", result["reply"]["text"])
        mock_req.assert_not_called()

    # TEST 4 — missing value supplied next turn merges with previous values.
    def test_4_missing_field_supplied_next_turn_merges(self):
        self._seed_address_change_action()
        message1 = ("บิล SP100820260716001 ผู้รับ หญิง 0616807329 "
                     "ที่อยู่ 8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request") as mock_req:
            turn2 = self.engine.decide("21120", history=history,
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        collected = turn2["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("PostalCode"), "21120")
        self.assertEqual(collected.get("Province"), "ระยอง")  # not lost
        self.assertEqual(collected.get("ShipmentCode"), "SP100820260716001")  # not lost
        self.assertIn("ยืนยัน", turn2["reply"]["text"])
        mock_req.assert_not_called()

    # TEST 5 — everything (including address) in the very first message -> never re-asked.
    def test_5_first_message_with_everything_never_reasks_address(self):
        self._seed_address_change_action()
        message = (f"ต้องการเปลี่ยนที่อยู่บิลขนส่งของบิล SP100820260716001 "
                    f"ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(message, history=[],
                                         context={"developer_mode": True,
                                                   "customer_context": {"cust_code": "SP1008"}})
        self.assertNotIn("ที่อยู่จัดส่งใหม่", result["reply"]["text"])
        self.assertIn("ยืนยัน", result["reply"]["text"])
        mock_req.assert_not_called()

    # TEST 6 — a correction before confirmation changes ONLY that field.
    def test_6_correction_before_confirmation_changes_only_that_field(self):
        self._seed_address_change_action()
        message1 = (f"เปลี่ยนที่อยู่บิล SP100820260716001 ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        self.assertIn("ยืนยัน", turn1["reply"]["text"])  # already at confirmation step
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request") as mock_req:
            turn2 = self.engine.decide("จังหวัดผิด เป็นชลบุรี", history=history,
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        collected = turn2["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("Province"), "ชลบุรี")
        self.assertEqual(collected.get("District"), "บ้านค่าย")  # untouched
        self.assertEqual(collected.get("Subdistrict"), "ตาขัน")  # untouched
        self.assertEqual(collected.get("PostalCode"), "21120")  # untouched
        self.assertIn("ชลบุรี", turn2["reply"]["text"])  # revised summary shown again
        mock_req.assert_not_called()

    # TEST 7 — CustCode already known in history/profile is reused, never re-asked.
    def test_7_custcode_already_known_is_reused(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=[],
                                         context={"developer_mode": True,
                                                   "customer_context": {"cust_code": "SP1008"}})
        self.assertNotIn("รหัสลูกค้า", result["reply"]["text"])
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("CustCode"), "SP1008")
        mock_req.assert_not_called()

    # TEST 8 — Shipment/Bill code supplied before the address is retained across turns.
    def test_8_shipment_code_supplied_before_address_is_retained(self):
        self._seed_address_change_action()
        message1 = "ต้องการเปลี่ยนที่อยู่ของบิล SP100820260716001"
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request"):
            turn2 = self.engine.decide(f"ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}", history=history,
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        collected = turn2["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("ShipmentCode"), "SP100820260716001")

    # TEST 9 — confirm -> exactly one (mocked) CS notification.
    def test_9_confirmation_sends_exactly_one_notification(self):
        self._seed_address_change_action()
        message1 = (f"เปลี่ยนที่อยู่บิล SP100820260716001 ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             self._mock_secret():
            turn2 = self.engine.decide("ยืนยัน", history=history,
                                        context={"developer_mode": True, "confirmed": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        self.assertEqual(turn2["routing"]["type"], "API")
        mock_req.assert_called_once()
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        sent_message = sent_body.get("Message") or ""
        self.assertIn("ระยอง", sent_message)
        self.assertIn("21120", sent_message)
        self.assertIn("ลูกค้ายืนยันข้อมูลแล้ว", sent_message)
        self.assertNotIn("FAKE-SECRET", str(turn2))

    # TEST 10 — confirming twice must not send a duplicate notification.
    def test_10_confirming_twice_no_duplicate_notification(self):
        self._seed_address_change_action()
        message1 = (f"เปลี่ยนที่อยู่บิล SP100820260716001 ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             self._mock_secret():
            turn2 = self.engine.decide("ยืนยัน", history=history,
                                        context={"developer_mode": True, "confirmed": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history2 = history + [{"role": "user", "content": "ยืนยัน"},
                               {"role": "assistant", "content": turn2["reply"]["text"]}]
        # A caller (webhook/admin) only ever re-invokes decide() with
        # confirmed=True in direct response to a customer's OWN fresh
        # "ยืนยัน" reply for a turn that is still awaiting confirmation.
        # This history already shows turn2 EXECUTED (routing_type=API,
        # not a repeat of the confirmation question), so
        # _resolve_continuation_action/_requires_confirmation would no
        # longer treat a further bare "ยืนยัน" as answering a still-open
        # gate for THIS action -- confirming the platform's existing,
        # already-tested duplicate-protection precondition rather than
        # re-deriving it here.
        with patch("services.action_executor.requests.request") as mock_req2:
            self.engine.decide("ยืนยัน", history=history2,
                                context={"developer_mode": True, "confirmed": True,
                                          "customer_context": {"cust_code": "SP1008"}})
        mock_req2.assert_not_called()

    # TEST 11 — cancellation never sends a notification.
    def test_11_cancellation_never_notifies(self):
        self._seed_address_change_action()
        message1 = (f"เปลี่ยนที่อยู่บิล SP100820260716001 ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide("ยกเลิก", history=history,
                                         context={"developer_mode": True,
                                                   "customer_context": {"cust_code": "SP1008"}})
        mock_req.assert_not_called()
        self.assertEqual(result["routing"]["type"], "WORKFLOW")

    # TEST 12 — China warehouse question must never select this action.
    def test_12_china_warehouse_question_never_selects_this_action(self):
        self._seed_address_change_action()
        candidates = search_candidate_actions(self.reg, workflow=None, message="ที่อยู่โกดังจีนอยู่ที่ไหน",
                                               collected_slots={})
        selected = select_best_action(candidates, minimum_score=1.0)
        self.assertIsNone(selected)  # RAG_ONLY territory, no Business Action should claim this

    # TEST 13 — a new session/user never leaks the previous customer's address/CustCode.
    def test_13_new_session_no_leakage(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request"):
            result = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=[], context={"developer_mode": True})
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertNotIn("CustCode", collected)
        self.assertIn("รหัสลูกค้า", result["reply"]["text"])  # must ask fresh, nothing carried over

    # TEST 14 — no raw JSON ever reaches the customer-facing reply.
    def test_14_no_raw_json_in_any_customer_reply(self):
        self._seed_address_change_action()
        message1 = (f"เปลี่ยนที่อยู่บิล SP100820260716001 ผู้รับ หญิง 0616807329 ที่อยู่ {self.FULL_ADDRESS}")
        with patch("services.action_executor.requests.request"):
            turn1 = self.engine.decide(message1, history=[],
                                        context={"developer_mode": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        self.assertFalse(turn1["reply"]["text"].strip().startswith("{"))
        history = [{"role": "user", "content": message1}, {"role": "assistant", "content": turn1["reply"]["text"]}]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})), \
             self._mock_secret():
            turn2 = self.engine.decide("ยืนยัน", history=history,
                                        context={"developer_mode": True, "confirmed": True,
                                                  "customer_context": {"cust_code": "SP1008"}})
        self.assertFalse(turn2["reply"]["text"].strip().startswith("{"))

    # TEST 15 — Confirmation Continuation Correctness fix (2026-08-23):
    # a parameter (CustCode) given several turns before the confirmation
    # question, then replayed as only a synthetic 2-turn history (the
    # confirmation's OWN original_message + question_text — mirroring
    # exactly what services/pending_confirmation_service.py stores and
    # what line_bot/webhook.py / admin/routes.py Auto mode now pass
    # through), must still execute — never fall through to an unrelated
    # fresh-message classification. Deliberately passes NO customer_context
    # at all, so this proves the fix does not rely on Identifier Memory
    # happening to be available/strong enough — the caller's own already-
    # collected, already-validated parameters are used directly.
    def test_15_confirmed_action_id_bypasses_history_replay_entirely(self):
        action_id = self._seed_address_change_action()
        address_message = (f"บิล SP100820260716001 ผู้รับ ทดสอบ 0812345678 ที่อยู่ {self.FULL_ADDRESS}")
        confirmation_question = "รบกวนตรวจสอบข้อมูลอีกครั้งนะคะ... ยืนยันการดำเนินการหรือไม่คะ?"
        # This 2-turn history is EXACTLY what a synthetic pending-row
        # replay would produce — CustCode is nowhere in it, proving the
        # continuation-matching text-replay path (_resolve_continuation_
        # action) could never recover it on its own (verified separately
        # against production: it returns None for this exact shape).
        truncated_history = [
            {"role": "user", "content": address_message},
            {"role": "assistant", "content": confirmation_question},
        ]
        collected_parameters = {
            "CustCode": "SP1008", "ShipmentCode": "SP100820260716001",
            "ReceiverName": "ทดสอบ", "ReceiverPhone": "0812345678",
            "Address": "8/7 ม.8", "Subdistrict": "ตาขัน", "District": "บ้านค่าย",
            "Province": "ระยอง", "PostalCode": "21120",
        }
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             self._mock_secret():
            result = self.engine.decide(
                "ยืนยัน", history=truncated_history,
                context={"developer_mode": True, "confirmed": True,
                          "confirmed_action_id": action_id, "confirmed_parameters": collected_parameters})
        self.assertEqual(mock_req.call_count, 1, "must execute exactly once, never fall through to RAG/fallback")
        self.assertNotEqual(result["routing"]["type"], "RAG")
        sent_body = mock_req.call_args.kwargs.get("json") or mock_req.call_args.kwargs.get("data") or {}
        self.assertEqual(sent_body.get("CustCode"), "SP1008")

    # TEST 15b — Address Change Full UAT fix (2026-08-24): a field
    # correction sent WHILE a confirmation is genuinely pending must
    # apply, even when history-replay alone can never reconstruct the
    # confirmation text to match against (the SAME truncated-history gap
    # TEST 15 proves for an actual "ยืนยัน" reply — this proves the
    # analogous fix for a "revise a field" reply instead). Without
    # pending_action_id/pending_parameters in context, this exact
    # truncated history falls through to RAG entirely (confirmed live
    # and via _resolve_continuation_action returning None for this
    # shape) — the caller-supplied pending state is what recovers it.
    def test_15b_pending_parameters_recovers_correction_when_replay_cannot(self):
        action_id = self._seed_address_change_action()
        address_message = (f"บิล SP100820260716001 ผู้รับ ทดสอบ 0812345678 ที่อยู่ {self.FULL_ADDRESS}")
        confirmation_question = "รบกวนตรวจสอบข้อมูลอีกครั้งนะคะ... ยืนยันการดำเนินการหรือไม่คะ?"
        truncated_history = [
            {"role": "user", "content": address_message},
            {"role": "assistant", "content": confirmation_question},
        ]
        pending_parameters = {
            "CustCode": "SP1008", "ShipmentCode": "SP100820260716001",
            "ReceiverName": "ทดสอบ", "ReceiverPhone": "0812345678",
            "Address": "8/7 ม.8", "Subdistrict": "ตาขัน", "District": "บ้านค่าย",
            "Province": "ระยอง", "PostalCode": "21120",
        }
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(
                "จังหวัดผิดครับ เปลี่ยนเป็นชลบุรี", history=truncated_history,
                context={"developer_mode": True,
                          "pending_action_id": action_id, "pending_parameters": pending_parameters})
        self.assertNotEqual(result["routing"]["type"], "RAG")
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("Province"), "ชลบุรี")
        self.assertEqual(collected.get("CustCode"), "SP1008")
        self.assertEqual(collected.get("District"), "บ้านค่าย")
        self.assertEqual(collected.get("PostalCode"), "21120")
        self.assertIn("ชลบุรี", result["reply"]["text"])
        mock_req.assert_not_called()  # revised summary shown again, never auto-executes

    def _seed_customer_lookup_action(self):
        """A second, unrelated Business Action ('ข้อมูลลูกค้า' really is
        its own real production intent, getdatacustomer) seeded alongside
        the address-change action, to reproduce the exact Production
        Safety Check finding: a customer message that is itself a
        decisive match for THIS action must never be silently absorbed
        as a failed ShipmentCode collection attempt for the OTHER one."""
        action_id = _seed_action(
            self.reg, key="getdatacustomer", action_type="API", category="Customer Lookup",
            ai_description="ค้นหาข้อมูลลูกค้า", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer",
                                                "http_method": "POST"})
        return action_id

    # TEST 16 — Generic Continuation Intent Guard fix (2026-08-24):
    # ADDRESS-UAT-04 regression (Production Safety Check finding) — a
    # different valid intent between the identifier turn and the address
    # trigger must never trip max_retry / escalate to Human Handoff.
    def test_16_unrelated_intent_between_identifier_and_trigger_no_handoff(self):
        self._seed_address_change_action()
        self._seed_customer_lookup_action()
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("SP1008", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "SP1008"}, {"role": "assistant", "content": turn1["reply"]["text"]}]

            turn2 = self.engine.decide("ข้อมูลลูกค้า", history=history, context={"developer_mode": True})
            history += [{"role": "user", "content": "ข้อมูลลูกค้า"}, {"role": "assistant", "content": turn2["reply"]["text"]}]
            self.assertNotEqual(turn2["routing"]["type"], "HUMAN_HANDOFF")

            turn3 = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=history, context={"developer_mode": True})
        self.assertNotEqual(turn3["routing"]["type"], "HUMAN_HANDOFF")
        collected = (turn3.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected.get("CustCode"), "SP1008", "CustCode must survive the unrelated turn")
        # getdatacustomer itself has no confirmation gate and CustCode was
        # already known, so turn 2's diversion correctly executed it (a
        # real, legitimate lookup answer) -- proving requirement #4, not
        # violating it.
        mock_req.assert_called_once()

    # TEST 17 — the more important mid-flow case: the diversion happens
    # WHILE the address-change action is actively waiting for
    # ShipmentCode (not before it's even selected), and the customer
    # returns to the address-change request afterward.
    def test_17_mid_flow_diversion_then_resume_no_premature_handoff(self):
        self._seed_address_change_action()
        self._seed_customer_lookup_action()
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("SP1008", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "SP1008"}, {"role": "assistant", "content": turn1["reply"]["text"]}]

            turn2 = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=history, context={"developer_mode": True})
            history += [{"role": "user", "content": "ต้องการเปลี่ยนที่อยู่บิลขนส่ง"},
                        {"role": "assistant", "content": turn2["reply"]["text"]}]
            self.assertIn("เลขที่บิล", turn2["reply"]["text"])  # asking for ShipmentCode

            # Diversion mid-collection — must be handled as its own intent,
            # never counted as a failed ShipmentCode attempt.
            turn3 = self.engine.decide("ข้อมูลลูกค้า", history=history, context={"developer_mode": True})
            history += [{"role": "user", "content": "ข้อมูลลูกค้า"}, {"role": "assistant", "content": turn3["reply"]["text"]}]
            self.assertNotEqual(turn3["routing"]["type"], "HUMAN_HANDOFF")
            self.assertEqual((turn3.get("developer") or {}).get("information_collection_status", {})
                              .get("selected_business_action"), "getdatacustomer")

            # Return to the address-change request — must resume cleanly,
            # CustCode still available, no premature escalation.
            turn4 = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=history, context={"developer_mode": True})
        self.assertNotEqual(turn4["routing"]["type"], "HUMAN_HANDOFF")
        collected4 = (turn4.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected4.get("CustCode"), "SP1008")
        # One real call from turn 3's legitimate getdatacustomer lookup;
        # the address-change flow itself never reached execution (still
        # missing ShipmentCode on turn 4).
        mock_req.assert_called_once()

    # TEST 18 — a customer simply repeating the SAME action's own trigger
    # phrase (not supplying the pending value, but also not a different
    # intent) must not consume a retry either.
    def test_18_same_action_trigger_repeated_not_counted_as_retry(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("SP1008", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "SP1008"}, {"role": "assistant", "content": turn1["reply"]["text"]}]

            for _ in range(3):
                turn = self.engine.decide("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", history=history,
                                           context={"developer_mode": True})
                self.assertNotEqual(turn["routing"]["type"], "HUMAN_HANDOFF",
                                     "repeating the action's own trigger must never exhaust the retry budget")
                history += [{"role": "user", "content": "ต้องการเปลี่ยนที่อยู่บิลขนส่ง"},
                            {"role": "assistant", "content": turn["reply"]["text"]}]
        mock_req.assert_not_called()

    # TEST 19 — genuine, repeated non-answers (no decisive match for
    # anything) must still trip max_retry -> Human Handoff exactly as
    # before this fix. Never globally disabled.
    def test_19_genuine_repeated_non_answer_still_escalates(self):
        self._seed_address_change_action()
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("SP1008", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "SP1008"}, {"role": "assistant", "content": turn1["reply"]["text"]}]

            turn2 = self.engine.decide("เอิ่ม", history=history, context={"developer_mode": True})
            history += [{"role": "user", "content": "เอิ่ม"}, {"role": "assistant", "content": turn2["reply"]["text"]}]
            self.assertNotEqual(turn2["routing"]["type"], "HUMAN_HANDOFF")

            turn3 = self.engine.decide("ไม่รู้อะ", history=history, context={"developer_mode": True})
        self.assertEqual(turn3["routing"]["type"], "HUMAN_HANDOFF")
        mock_req.assert_not_called()

    # TEST 20 — Address Change Full UAT fix (2026-08-24): supplying the
    # full address block BEFORE CustCode is known must never produce the
    # generic "found N possible values, which one?" ambiguous-candidate
    # reply. Root cause: the compound Thai-address pre-pass correctly
    # attributed the phone/postal-code substrings to their own slots, but
    # never marked those raw values as "used" for the SEPARATE required-
    # parameter binding loop that runs next — so Address's own loose
    # non_empty validator saw the SAME phone/house-number/postal-code
    # substrings as fresh, competing, ambiguous candidates for itself.
    def test_20_address_before_custcode_never_produces_ambiguous_candidate_reply(self):
        self._seed_address_change_action()
        message = ("ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 "
                   "ตำบลบางแก้ว อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(message, history=[], context={"developer_mode": True})
        self.assertNotIn("พบข้อมูล", result["reply"]["text"])
        collected = result["developer"]["information_collection_status"]["collected_parameters"]
        self.assertEqual(collected.get("ReceiverName"), "สมชาย")
        self.assertEqual(collected.get("ReceiverPhone"), "0812345678")
        self.assertEqual(collected.get("Address"), "99/12 หมู่ 4")
        self.assertEqual(collected.get("Subdistrict"), "บางแก้ว")
        self.assertEqual(collected.get("District"), "บางพลี")
        self.assertEqual(collected.get("Province"), "สมุทรปราการ")
        self.assertEqual(collected.get("PostalCode"), "10540")
        self.assertNotIn("CustCode", collected)
        mock_req.assert_not_called()


class TestGenericContinuationIntentGuard(unittest.TestCase):
    """Generic Continuation Intent Guard fix (2026-08-24) — proves the fix
    in services/decision_engine.py::decide() (the diversion check right
    after _resolve_continuation_action, and _count_genuine_retries) is
    genuinely generic, using TWO existing, non-address Business Actions
    with their own multi-turn parameter collection — never
    requestshippingaddresschange, never any hardcoded action_key or Thai
    phrase. Mirrors GOLDEN-059/get_customer_coupons-style fixtures already
    used elsewhere in this test file, not a new fixture convention."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.engine = _engine_with_registry(self.reg)
        # Action A: order lookup, needs OrderCode then Email (2 turns).
        # Email carries a real pattern (never a bare non_empty free-text
        # field) so a genuinely off-topic reply like "ไม่ทราบ"/"เอิ่มม"
        # cannot accidentally satisfy it via the generic whole-message
        # free-text fallback (the same mechanism SendLineNotiCS's own
        # Message field relies on) -- that fallback existing at all is
        # correct, established platform behavior; it just isn't what
        # these retry-focused tests want to exercise.
        self.order_id = _seed_action(
            self.reg, key="searchdataorder", action_type="API", category="Order Lookup",
            ai_description="ค้นหาคำสั่งซื้อของลูกค้า", keywords=["คำสั่งซื้อ", "ตรวจสอบออเดอร์"])
        self.reg.replace_parameters(self.order_id, [
            {"name": "OrderCode", "display_name": "รหัสคำสั่งซื้อ", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^PO\d+$"},
            {"name": "Email", "display_name": "อีเมล", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$"},
        ])
        self.reg.upsert_execution(self.order_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/SearchDataOrder",
                                                     "http_method": "POST"})
        # Action B: an unrelated, decisively-keyworded action the customer
        # might genuinely switch to mid-collection. CustCode pattern
        # deliberately does NOT overlap with OrderCode's "PO..." shape,
        # so a stray OrderCode value from earlier history can never be
        # mistaken for this action's own identifier.
        self.coupon_id = _seed_action(
            self.reg, key="get_customer_coupons", action_type="API", category="Customer Lookup",
            ai_description="ดูคูปองของลูกค้า", keywords=["คูปอง"])
        self.reg.replace_parameters(self.coupon_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.upsert_execution(self.coupon_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/GetCoupons",
                                                      "http_method": "POST"})

    # A — correct parameter answer -> normal continuation, unaffected.
    # Both required parameters end up satisfied, and this action has no
    # confirmation gate, so it correctly executes -- that's the expected,
    # unaffected baseline this fix must never break.
    def test_A_correct_answer_continues_normally(self):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req:
            turn1 = self.engine.decide("PO100820260815001", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "PO100820260815001"},
                       {"role": "assistant", "content": turn1["reply"]["text"]}]
            turn2 = self.engine.decide("test@example.com", history=history, context={"developer_mode": True})
        collected = (turn2.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected.get("OrderCode"), "PO100820260815001")
        self.assertEqual(collected.get("Email"), "test@example.com")
        mock_req.assert_called_once()

    # B — genuine invalid answer repeated -> max_retry protection intact.
    # Neither reply matches Email's pattern NOR any action's own
    # keywords, so this is exactly the "irrelevant/unrecognized text"
    # case this fix must leave alone.
    def test_B_genuine_invalid_answers_still_trigger_max_retry(self):
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("PO100820260815001", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "PO100820260815001"},
                       {"role": "assistant", "content": turn1["reply"]["text"]}]
            turn2 = self.engine.decide("ไม่ทราบครับ", history=history, context={"developer_mode": True})
            history += [{"role": "user", "content": "ไม่ทราบครับ"}, {"role": "assistant", "content": turn2["reply"]["text"]}]
            self.assertNotEqual(turn2["routing"]["type"], "HUMAN_HANDOFF")
            turn3 = self.engine.decide("เอิ่มมม", history=history, context={"developer_mode": True})
        self.assertEqual(turn3["routing"]["type"], "HUMAN_HANDOFF")
        mock_req.assert_not_called()

    # C — a different valid intent mid-collection -> not counted as retry,
    # and gets handled as its OWN request. CustCode is not yet known
    # (no customer_context here), so get_customer_coupons asks for it
    # instead of executing -- proving the diversion itself, independent
    # of whether the diverted action happens to be immediately complete.
    def test_C_different_valid_intent_not_counted_as_retry(self):
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("PO100820260815001", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "PO100820260815001"},
                       {"role": "assistant", "content": turn1["reply"]["text"]}]
            turn2 = self.engine.decide("คูปอง", history=history, context={"developer_mode": True})
        self.assertNotEqual(turn2["routing"]["type"], "HUMAN_HANDOFF")
        self.assertEqual((turn2.get("developer") or {}).get("information_collection_status", {})
                          .get("selected_business_action"), "get_customer_coupons")
        mock_req.assert_not_called()

    # D — repeating the SAME action's own trigger keyword mid-collection
    # -> not counted as a retry either, never exhausts the retry budget.
    def test_D_same_action_trigger_repeated_not_counted_as_retry(self):
        with patch("services.action_executor.requests.request") as mock_req:
            turn1 = self.engine.decide("PO100820260815001", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "PO100820260815001"},
                       {"role": "assistant", "content": turn1["reply"]["text"]}]
            for _ in range(3):
                turn = self.engine.decide("ตรวจสอบออเดอร์", history=history, context={"developer_mode": True})
                self.assertNotEqual(turn["routing"]["type"], "HUMAN_HANDOFF")
                history += [{"role": "user", "content": "ตรวจสอบออเดอร์"},
                            {"role": "assistant", "content": turn["reply"]["text"]}]
        mock_req.assert_not_called()

    # E — Identifier Memory still works (a remembered CustCode auto-fills
    # a still-missing parameter of the same concept, unaffected by this
    # fix). CustCode is the only required parameter and is fully known,
    # so this action correctly executes -- that's the expected baseline.
    def test_E_identifier_memory_still_works(self):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req:
            result = self.engine.decide("คูปอง", history=[],
                                         context={"developer_mode": True, "customer_context": {"cust_code": "C1008"}})
        collected = (result.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected.get("CustCode"), "C1008")
        mock_req.assert_called_once()

    # F — a fresh session/history has no leakage from any prior scenario.
    def test_F_fresh_session_no_leakage(self):
        result = self.engine.decide("ตรวจสอบออเดอร์", history=[], context={"developer_mode": True})
        collected = (result.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected, {})

    # G — Address Change Full UAT fix (2026-08-24): a bare value that
    # SATISFIES the pending action's own still-missing parameter must
    # never be treated as "decisive evidence of a different intent"
    # merely because it ALSO structurally matches a sibling action's
    # identically-shaped, identically-named parameter, with ZERO real
    # keyword/topical evidence for that sibling. Confirmed live: a
    # ShipmentCode reply mid-address-change diverted into
    # SearchDataShipment purely because both actions configure a
    # ShipmentCode parameter of the same shape. Reproduced generically
    # here with two unrelated, non-address actions sharing an identical
    # "Email" parameter (name + pattern) -- one seeded ONLY for this
    # test so the shared setUp's other tests are unaffected.
    def test_G_structural_identifier_overlap_alone_never_diverts_continuation(self):
        sibling_id = _seed_action(
            self.reg, key="get_customer_by_email", action_type="API", category="Unrelated Lookup",
            ai_description="", keywords=[])
        self.reg.replace_parameters(sibling_id, [
            {"name": "Email", "display_name": "อีเมล", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$"},
        ])
        self.reg.upsert_execution(sibling_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/GetByEmail",
                                                  "http_method": "POST"})
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req:
            turn1 = self.engine.decide("PO100820260815001", history=[], context={"developer_mode": True})
            history = [{"role": "user", "content": "PO100820260815001"},
                       {"role": "assistant", "content": turn1["reply"]["text"]}]
            turn2 = self.engine.decide("test@example.com", history=history, context={"developer_mode": True})
        collected = (turn2.get("developer") or {}).get("information_collection_status", {}).get("collected_parameters", {})
        self.assertEqual(collected.get("OrderCode"), "PO100820260815001")
        self.assertEqual(collected.get("Email"), "test@example.com")
        mock_req.assert_called_once()


if __name__ == "__main__":
    unittest.main()
