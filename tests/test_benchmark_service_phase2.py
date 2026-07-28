"""Regression tests for AI Evaluation Phase 2 run modes
(services/benchmark_service.py) — Query Understanding Only (no
retrieval/LLM) and Conversation Scenario (real multi-turn session via
services.playground_orchestrator), against a mocked Supabase client.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import benchmark_service as bs
from tests.test_benchmark_service import _FakeSupabase, _mock_table  # reuse existing fake, no duplication


class TestQueryUnderstandingCase(unittest.TestCase):
    def test_never_touches_retrieval_or_llm(self):
        case = {"id": "c1", "question": "มีโกดังจีนไหม", "expected_topic": "warehouse"}
        with patch("services.benchmark_service.get_rag_service") as mock_rag, \
             patch("services.benchmark_service.get_llm_service") as mock_llm:
            result = bs.run_query_understanding_case(case)
        mock_rag.assert_not_called()
        mock_llm.assert_not_called()
        self.assertEqual(result["retrieved_chunks"], [])
        self.assertIsNone(result["actual_answer"])

    def test_topic_and_entity_fields_populated(self):
        case = {"id": "c1", "question": "ขอที่อยู่โกดังไทย", "expected_topic": "warehouse",
                "expected_entities": {"location": "ไทย"}}
        result = bs.run_query_understanding_case(case)
        qu = result["query_understanding_metrics"]
        self.assertEqual(qu["topic"]["actual"], "warehouse")
        self.assertTrue(qu["entities"]["exact_match"])
        self.assertEqual(result["status"], "pass")

    def test_wrong_expected_topic_fails_case(self):
        case = {"id": "c1", "question": "ขอที่อยู่โกดังไทย", "expected_topic": "payment"}
        result = bs.run_query_understanding_case(case)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failure_type"], "query_understanding_mismatch")

    def test_conversation_history_resolves_followup(self):
        case = {"id": "c2", "question": "แล้วจีนล่ะ", "expected_topic": "warehouse",
                "expected_canonical_query": "ขอที่อยู่และแผนที่โกดังจีน",
                "previous_messages": [{"role": "user", "content": "ขอที่อยู่โกดังไทย"},
                                      {"role": "assistant", "content": "..."}]}
        result = bs.run_query_understanding_case(case)
        self.assertEqual(result["query_understanding_metrics"]["canonical_query"]["actual"], "ขอที่อยู่และแผนที่โกดังจีน")
        self.assertEqual(result["status"], "pass")


class TestExecuteRunQueryUnderstandingMode(unittest.TestCase):
    def test_query_understanding_mode_persists_results_without_llm_or_retrieval(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "QUERY_UNDERSTANDING"}).execute().data[0]
        cases = [{"id": "c1", "question": "ขอที่อยู่โกดังไทย", "expected_topic": "warehouse"}]
        with patch("services.benchmark_service.get_rag_service") as mock_rag, \
             patch("services.benchmark_service.get_llm_service") as mock_llm:
            bs.execute_run(sb, run["id"], cases, bs.QUERY_UNDERSTANDING)
        mock_rag.assert_not_called()
        mock_llm.assert_not_called()
        results = sb.store["rag_benchmark_results"]
        self.assertEqual(len(results), 1)
        self.assertIn("query_understanding_metrics", results[0])
        self.assertTrue(results[0]["query_understanding_metrics"])


class _FakePlaygroundResult:
    def __init__(self, answer, chunks, conversation_state):
        self.answer = answer
        self.chunks = chunks
        self.conversation_state = conversation_state
        self.input_tokens = 10
        self.output_tokens = 20
        self.estimated_cost_usd = 0.001


class TestConversationScenario(unittest.TestCase):
    def test_runs_sequentially_with_accumulating_history(self):
        seen_histories = []

        def fake_turn(question, top_k=3, history=None):
            seen_histories.append(list(history or []))
            return _FakePlaygroundResult(
                answer=f"answer to {question}", chunks=[{"file_name": "a.md", "cited": True}],
                conversation_state={"topic": "warehouse", "subtopic": None, "intent": "warehouse_location",
                                     "location": "จีน", "transport": None, "transition": "same_topic",
                                     "excluded_entities": {"location": [], "transport": []}, "state_changes": {}},
            )

        cases = [
            {"id": "c1", "turn_index": 0, "question": "โกดังจีน", "expected_topic": "warehouse"},
            {"id": "c2", "turn_index": 1, "question": "มีแผนที่ไหม", "expected_topic": "warehouse"},
        ]
        with patch("services.playground_orchestrator.run_playground_turn", side_effect=fake_turn):
            turn_results = bs.run_conversation_scenario(cases)

        self.assertEqual(len(turn_results), 2)
        self.assertEqual(seen_histories[0], [])  # first turn has no prior history
        self.assertEqual(len(seen_histories[1]), 2)  # second turn sees turn 1's user+assistant messages
        self.assertTrue(all(t["topic_ok"] for t in turn_results))

    def test_scenario_fails_if_any_turn_fails(self):
        def fake_turn(question, top_k=3, history=None):
            return _FakePlaygroundResult(
                answer="answer", chunks=[],
                conversation_state={"topic": "promotion", "subtopic": None, "intent": "unknown",
                                     "location": None, "transport": None, "transition": "switch_topic",
                                     "excluded_entities": {"location": [], "transport": []}, "state_changes": {}},
            )

        cases = [{"id": "c1", "turn_index": 0, "question": "โกดังจีน", "expected_topic": "warehouse"}]
        with patch("services.playground_orchestrator.run_playground_turn", side_effect=fake_turn):
            turn_results = bs.run_conversation_scenario(cases)
        self.assertFalse(turn_results[0]["turn_pass"])
        self.assertEqual(turn_results[0]["status"], "fail")

    def test_system_error_isolated_to_one_turn(self):
        def fake_turn(question, top_k=3, history=None):
            raise RuntimeError("boom")

        cases = [{"id": "c1", "turn_index": 0, "question": "q1"}]
        with patch("services.playground_orchestrator.run_playground_turn", side_effect=fake_turn):
            turn_results = bs.run_conversation_scenario(cases)
        self.assertEqual(turn_results[0]["status"], "error")
        self.assertEqual(turn_results[0]["failure_type"], "system_error")


class TestExecuteRunConversationScenarioMode(unittest.TestCase):
    def test_groups_by_scenario_key_and_persists_one_row_per_turn(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "CONVERSATION_SCENARIO"}).execute().data[0]
        cases = [
            {"id": "c1", "scenario_key": "s1", "turn_index": 0, "question": "โกดังจีน", "expected_topic": "warehouse"},
            {"id": "c2", "scenario_key": "s1", "turn_index": 1, "question": "มีแผนที่ไหม", "expected_topic": "warehouse"},
        ]

        def fake_turn(question, top_k=3, history=None):
            return _FakePlaygroundResult(
                answer="ok", chunks=[],
                conversation_state={"topic": "warehouse", "subtopic": None, "intent": "warehouse_location",
                                     "location": "จีน", "transport": None, "transition": "same_topic",
                                     "excluded_entities": {"location": [], "transport": []}, "state_changes": {}},
            )

        with patch("services.playground_orchestrator.run_playground_turn", side_effect=fake_turn):
            bs.execute_run(sb, run["id"], cases, bs.CONVERSATION_SCENARIO)

        results = sb.store["rag_benchmark_results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["scenario_key"] == "s1" for r in results))
        updated_run = sb.store["rag_benchmark_runs"][0]
        self.assertEqual(updated_run["status"], "completed")
        self.assertEqual(updated_run["total_cases"], 2)


class TestCompareRunsPhase2Metrics(unittest.TestCase):
    def test_metric_comparison_reports_not_comparable_when_missing(self):
        sb = _FakeSupabase()
        run_a = {"id": "runA", "run_name": "A", "passed_cases": 1, "total_cases": 1}
        run_b = {"id": "runB", "run_name": "B", "passed_cases": 1, "total_cases": 1}
        sb.store["rag_benchmark_runs"] = [run_a, run_b]
        sb.store["rag_benchmark_results"] = [
            {"run_id": "runA", "case_id": "c1", "question": "q1", "status": "pass"},
            {"run_id": "runB", "case_id": "c1", "question": "q1", "status": "pass"},
        ]
        diff = bs.compare_runs(sb, "runA", "runB")
        self.assertEqual(diff["metric_comparison"]["query_understanding_accuracy"]["direction"], "not_comparable")

    def test_metric_comparison_detects_improvement(self):
        sb = _FakeSupabase()
        run_a = {"id": "runA", "run_name": "A", "passed_cases": 1, "total_cases": 1}
        run_b = {"id": "runB", "run_name": "B", "passed_cases": 1, "total_cases": 1}
        sb.store["rag_benchmark_runs"] = [run_a, run_b]
        sb.store["rag_benchmark_results"] = [
            {"run_id": "runA", "case_id": "c1", "question": "q1", "status": "fail",
             "query_understanding_metrics": {"topic": {"pass": False}}},
            {"run_id": "runB", "case_id": "c1", "question": "q1", "status": "pass",
             "query_understanding_metrics": {"topic": {"pass": True}}},
        ]
        diff = bs.compare_runs(sb, "runA", "runB")
        mc = diff["metric_comparison"]["query_understanding_accuracy"]
        self.assertEqual(mc["direction"], "improved")


if __name__ == "__main__":
    unittest.main()
