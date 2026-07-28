"""Regression tests for services/validation_service.py — the AI
Production Validation Center orchestration layer. Verifies it correctly
SEQUENCES the existing benchmark_service modes (never reimplements
retrieval/scoring) using a mocked Supabase client and mocked
benchmark_service calls.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import validation_service as vs
from services import benchmark_service as bs


def _mock_table(store, name):
    class _Query:
        def __init__(self):
            self._filters = {}
            self._op = None
            self._neq = None

        def select(self, *_a, **_k):
            self._op = "select"
            return self

        def eq(self, k, v):
            self._filters[k] = v
            return self

        def neq(self, k, v):
            self._neq = (k, v)
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            rows = store.get(name, [])
            matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
            if self._neq:
                matched = [r for r in matched if r.get(self._neq[0]) != self._neq[1]]
            return MagicMock(data=matched)

    return _Query()


class _FakeSupabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _mock_table(self.store, name)


class TestStartValidationSequencing(unittest.TestCase):
    def test_runs_all_four_modes_in_order_then_completes(self):
        sb = _FakeSupabase({"rag_benchmark_runs": [], "rag_benchmark_results": []})
        started_modes = []

        def fake_start_run(sb_, *, dataset_id, run_name, mode, top_k=3, resume_run_id=None):
            started_modes.append(mode)
            return f"run-{mode}"

        def fake_get_run_progress(run_id):
            return {"status": "completed"}

        with patch("services.validation_service.bs.start_run", side_effect=fake_start_run), \
             patch("services.validation_service.bs.get_run_progress", side_effect=fake_get_run_progress), \
             patch("services.validation_service.load_baseline", return_value=None), \
             patch("services.validation_service._scorecard", return_value={"conversation": 90, "retrieval": 95,
                                                                            "grounding": 80, "critical_facts": 100}):
            validation_id = vs.start_validation(sb, dataset_id="ds1", top_k=3)
            for _ in range(50):
                progress = vs.get_validation_progress(validation_id)
                if progress["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)

        self.assertEqual(started_modes, [bs.QUERY_UNDERSTANDING, bs.RETRIEVAL_ONLY,
                                          bs.CONVERSATION_SCENARIO, bs.FULL_RAG])
        self.assertEqual(progress["status"], "completed")
        self.assertIn("report_markdown", progress)
        self.assertIn("report_json", progress)

    def test_cancellation_stops_before_remaining_steps(self):
        sb = _FakeSupabase({"rag_benchmark_runs": [], "rag_benchmark_results": []})
        call_count = {"n": 0}

        def fake_start_run(sb_, *, dataset_id, run_name, mode, top_k=3, resume_run_id=None):
            call_count["n"] += 1
            return f"run-{mode}"

        def fake_get_run_progress(run_id):
            time.sleep(0.05)  # gives the test's cancel_validation() call a window to land mid-run
            return {"status": "completed"}

        with patch("services.validation_service.bs.start_run", side_effect=fake_start_run), \
             patch("services.validation_service.bs.get_run_progress", side_effect=fake_get_run_progress), \
             patch("services.validation_service.load_baseline", return_value=None):
            validation_id = vs.start_validation(sb, dataset_id="ds1")
            vs.cancel_validation(validation_id)
            for _ in range(50):
                progress = vs.get_validation_progress(validation_id)
                if progress["status"] in ("completed", "cancelled", "failed"):
                    break
                time.sleep(0.05)

        self.assertEqual(progress["status"], "cancelled")
        self.assertLess(call_count["n"], 4)


class TestScorecard(unittest.TestCase):
    def test_reads_aggregate_metrics_from_stored_results(self):
        store = {
            "rag_benchmark_runs": [{"id": "ro-run", "total_cases": 10, "passed_cases": 9}],
            "rag_benchmark_results": [
                {"run_id": "conv-run", "conversation_metrics": {"turn_pass_rate": 0.8}},
                {"run_id": "conv-run", "conversation_metrics": {"turn_pass_rate": 1.0}},
                {"run_id": "conv-run", "grounding_metrics": {"status": "supported"}, "critical_fact_metrics": {"pass": True, "total_count": 1}},
                {"run_id": "conv-run", "grounding_metrics": {"status": "unsupported"}, "critical_fact_metrics": {"pass": False, "total_count": 1}},
            ],
        }
        sb = _FakeSupabase(store)
        card = vs._scorecard(sb, {"retrieval_only": "ro-run", "conversation_scenario": "conv-run", "full_rag": "fr-run"})
        self.assertEqual(card["retrieval"], 90.0)
        self.assertEqual(card["conversation"], 90.0)
        self.assertEqual(card["grounding"], 50.0)
        self.assertEqual(card["critical_facts"], 50.0)


class TestRegressionSummary(unittest.TestCase):
    def test_no_baseline_reports_unavailable(self):
        sb = _FakeSupabase({})
        result = vs._regression_summary(sb, {"retrieval_only": "r1"}, None)
        self.assertFalse(result["available"])

    def test_reuses_compare_runs_and_flags_overall_regression(self):
        sb = _FakeSupabase({})
        baseline = {"runs": {"RETRIEVAL_ONLY": {"run_id": "baseline-ro"}}}
        run_ids = {"retrieval_only": "candidate-ro"}

        def fake_compare(sb_, a, b):
            self.assertEqual(a, "baseline-ro")
            self.assertEqual(b, "candidate-ro")
            return {"pass_rate_change": -0.1, "cases_improved": [], "cases_regressed": ["c1"],
                    "cases_unchanged_count": 5, "metric_comparison": {}, "latency_change_ms": 10, "cost_change": 0.01}

        with patch("services.validation_service.bs.compare_runs", side_effect=fake_compare):
            result = vs._regression_summary(sb, run_ids, baseline)

        self.assertTrue(result["available"])
        self.assertEqual(result["overall"], "REGRESSED")
        self.assertEqual(result["per_mode"]["retrieval_only"]["regressed"], 1)

    def test_no_regressed_cases_reports_none(self):
        sb = _FakeSupabase({})
        baseline = {"runs": {"RETRIEVAL_ONLY": {"run_id": "baseline-ro"}}}
        run_ids = {"retrieval_only": "candidate-ro"}

        def fake_compare(sb_, a, b):
            return {"pass_rate_change": 0.1, "cases_improved": ["c1"], "cases_regressed": [],
                    "cases_unchanged_count": 5, "metric_comparison": {}, "latency_change_ms": -5, "cost_change": 0.0}

        with patch("services.validation_service.bs.compare_runs", side_effect=fake_compare):
            result = vs._regression_summary(sb, run_ids, baseline)
        self.assertEqual(result["overall"], "NONE")


class TestMarkdownReport(unittest.TestCase):
    def test_report_contains_key_sections(self):
        md = vs._render_markdown_report(
            "vid-123", {"retrieval_only": "run-1"}, {"conversation": 98, "retrieval": 99},
            {"available": True, "overall": "NONE", "per_mode": {}}, [], None,
            {"engine_version": "v1.0.0"}, "PASS", "PASS",
        )
        self.assertIn("AI Production Validation Report", md)
        self.assertIn("PASS", md)
        self.assertIn("run-1", md)
        self.assertIn("Production Ready", md)


if __name__ == "__main__":
    unittest.main()
