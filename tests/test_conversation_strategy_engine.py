"""Tests for services/conversation_strategy_engine.py.

FIXTURE-ONLY NOTE: reuses tests/test_conversation_form_generator.py's
_shipment_search_action() fixture (in-memory _FakeSupabase test double —
no real "Shipment Search" action exists in the live DB).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_conversation_form_generator import _shipment_search_action, _contract_for
from services.conversation_form_generator import ConversationState, generate_conversation_form
from services.conversation_strategy_engine import (
    analyze_conversation_strategy, apply_strategy_to_state, DEFAULT_CONFIDENCE_THRESHOLD,
)


class TestEnumFieldDetection(unittest.TestCase):
    def test_detects_bill_status_from_thai_option_label(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        strategy = analyze_conversation_strategy(contract, "ขอดูของที่ออกจากจีน", state)
        self.assertEqual(strategy["ready_fields"].get("BillStatus"), "2")
        self.assertGreaterEqual(strategy["confidence"]["BillStatus"], DEFAULT_CONFIDENCE_THRESHOLD)
        self.assertIn("BillStatus", strategy["auto_detected_fields"])
        self.assertEqual(strategy["reasoning"]["BillStatus"]["matched_via"], "option_alias")


class TestBooleanFlagDetection(unittest.TestCase):
    def test_detects_latest_flag_from_thai_phrase(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        strategy = analyze_conversation_strategy(contract, "ล่าสุด", state)
        self.assertEqual(strategy["ready_fields"].get("Latest"), True)


class TestQuestionReduction(unittest.TestCase):
    """Part 7 — the overall goal: BillStatus + Latest both detected in the
    SAME message -> the AT_LEAST_ONE group is satisfied and zero
    criterion-selection questions remain once the strategy is applied to
    state and fed into generate_conversation_form()."""

    def test_single_message_satisfies_group_zero_questions(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)

        # BEFORE: no strategy applied — plain form generator asks the
        # criterion-selection question.
        state_before = ConversationState(collected_values={"CustCode": "C00001"})
        form_before = generate_conversation_form(contract, state_before, language="th")
        criterion_steps_before = [s for s in form_before["steps"] if s["kind"] == "criterion_selection"]
        self.assertEqual(len(criterion_steps_before), 1)

        # AFTER: strategy detects BillStatus AND Latest from one message.
        state_after = ConversationState(collected_values={"CustCode": "C00001"})
        strategy = analyze_conversation_strategy(contract, "ขอดูของที่ออกจากจีนล่าสุด", state_after)
        self.assertIn("BillStatus", strategy["auto_detected_fields"])
        self.assertIn("Latest", strategy["auto_detected_fields"])
        self.assertIn("search_criterion", strategy["group_satisfaction"])

        apply_strategy_to_state(state_after, strategy)
        form_after = generate_conversation_form(contract, state_after, language="th")
        criterion_steps_after = [s for s in form_after["steps"] if s["kind"] == "criterion_selection"]
        self.assertEqual(len(criterion_steps_after), 0)
        self.assertTrue(form_after["ready_to_execute"])

    def test_questions_to_ask_shrinks_as_more_fields_detected(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)

        state_a = ConversationState(collected_values={"CustCode": "C00001"})
        strategy_a = analyze_conversation_strategy(contract, "สวัสดีครับ", state_a)

        state_b = ConversationState(collected_values={"CustCode": "C00001"})
        strategy_b = analyze_conversation_strategy(contract, "ขอดูของที่ออกจากจีนล่าสุด", state_b)

        self.assertGreater(len(strategy_a["questions_to_ask"]), len(strategy_b["questions_to_ask"]))
        self.assertEqual(len(strategy_b["questions_to_ask"]), 0)


class TestBelowThresholdNeverAutoFills(unittest.TestCase):
    def test_generic_weak_match_does_not_auto_fill(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        # No recognizable phrase for any field at all.
        strategy = analyze_conversation_strategy(contract, "hello there, nothing relevant", state)
        self.assertEqual(strategy["ready_fields"], {})
        self.assertEqual(strategy["auto_detected_fields"], [])


class TestRelativeDateDetection(unittest.TestCase):
    def test_detects_yesterday_thai_phrase_for_date_field(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        strategy = analyze_conversation_strategy(contract, "ได้รับเมื่อวาน", state)
        self.assertIn("ReceivedDate", strategy["ready_fields"])


class TestRankingFormula(unittest.TestCase):
    def test_ranking_prefers_higher_priority_when_confidence_tied(self):
        from services.conversation_strategy_engine import _rank_score
        self.assertGreater(_rank_score(0.8, 10), _rank_score(0.8, 0))
        self.assertGreater(_rank_score(0.9, 0), _rank_score(0.6, 10))


if __name__ == "__main__":
    unittest.main()
