"""Regression tests for services/benchmark_service.py — persistence,
cancellation, resume-skip, and run comparison, all against a mocked
Supabase client (never a real DB, never a real LLM/embedding call)."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import benchmark_service as bs


def _mock_table(store, name):
    """Minimal fluent-chain fake for one Supabase table backed by a
    plain Python list, enough for insert/select/update/eq/execute."""
    class _Query:
        def __init__(self):
            self._filters = {}
            self._op = None
            self._payload = None

        def select(self, *_a, **_k):
            self._op = "select"
            return self

        def insert(self, payload):
            self._op = "insert"
            self._payload = payload
            return self

        def update(self, payload):
            self._op = "update"
            self._payload = payload
            return self

        def eq(self, key, value):
            self._filters[key] = value
            return self

        def execute(self):
            rows = store[name]
            if self._op == "select":
                matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
                return MagicMock(data=matched)
            if self._op == "insert":
                import uuid
                row = dict(self._payload)
                row.setdefault("id", str(uuid.uuid4()))
                rows.append(row)
                return MagicMock(data=[row])
            if self._op == "update":
                matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
                for r in matched:
                    r.update(self._payload)
                return MagicMock(data=matched)
            return MagicMock(data=[])

    return _Query()


class _FakeSupabase:
    def __init__(self):
        self.store = {"rag_benchmark_runs": [], "rag_benchmark_results": [], "rag_benchmark_cases": []}

    def table(self, name):
        return _mock_table(self.store, name)


def _case(id_, question, expected_file="a.md", expected_section=None):
    return {"id": id_, "question": question, "expected_file": expected_file,
            "expected_section": expected_section, "expected_answer": None,
            "must_include": [], "must_not_include": [], "prohibited_files": [], "language": None,
            "expected_answerability": None}


class TestRetrievalOnlyExecution(unittest.TestCase):
    def test_execute_run_persists_one_result_per_case(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "RETRIEVAL_ONLY", "status": "queued"}).execute().data[0]
        cases = [_case("c1", "q1"), _case("c2", "q2")]

        fake_chunks = [{"file_name": "a.md", "section_title": "S", "score": 0.9, "hybrid_score": 0.8, "classification": "direct_evidence"}]
        with patch("services.benchmark_service.get_rag_service") as mock_rag:
            mock_rag.return_value.retrieve.return_value = fake_chunks
            bs.execute_run(sb, run["id"], cases, bs.RETRIEVAL_ONLY, top_k=3)

        results = sb.store["rag_benchmark_results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] == "pass" for r in results))
        updated_run = sb.store["rag_benchmark_runs"][0]
        self.assertEqual(updated_run["status"], "completed")
        self.assertEqual(updated_run["total_cases"], 2)
        self.assertEqual(updated_run["passed_cases"], 2)

    def test_never_calls_llm_in_retrieval_only_mode(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "RETRIEVAL_ONLY"}).execute().data[0]
        cases = [_case("c1", "q1")]
        with patch("services.benchmark_service.get_rag_service") as mock_rag, \
             patch("services.benchmark_service.get_llm_service") as mock_llm:
            mock_rag.return_value.retrieve.return_value = []
            bs.execute_run(sb, run["id"], cases, bs.RETRIEVAL_ONLY)
        mock_llm.assert_not_called()


class TestCancellation(unittest.TestCase):
    def test_cancel_run_stops_before_remaining_cases(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "RETRIEVAL_ONLY"}).execute().data[0]
        cases = [_case(f"c{i}", f"q{i}") for i in range(5)]

        with bs._RUN_LOCK:
            bs._RUN_STATE[run["id"]] = {"status": "running", "completed": 0, "failed": 0, "total": 5,
                                         "current_case": None, "cancelled": False, "started_at": time.time()}

        call_count = {"n": 0}

        def fake_retrieve(question, top_k=3):
            call_count["n"] += 1
            if call_count["n"] == 2:
                bs.cancel_run(run["id"])
            return []

        with patch("services.benchmark_service.get_rag_service") as mock_rag:
            mock_rag.return_value.retrieve.side_effect = fake_retrieve
            bs.execute_run(sb, run["id"], cases, bs.RETRIEVAL_ONLY)

        results = sb.store["rag_benchmark_results"]
        self.assertLess(len(results), 5, "cancellation must stop processing remaining cases")
        updated_run = sb.store["rag_benchmark_runs"][0]
        self.assertEqual(updated_run["status"], "cancelled")

    def test_resume_skips_already_completed_cases(self):
        sb = _FakeSupabase()
        run = sb.table("rag_benchmark_runs").insert({"run_name": "t", "mode": "RETRIEVAL_ONLY"}).execute().data[0]
        sb.store["rag_benchmark_results"].append({"run_id": run["id"], "case_id": "c1"})
        cases = [_case("c1", "q1"), _case("c2", "q2")]

        with patch("services.benchmark_service.get_rag_service") as mock_rag:
            mock_rag.return_value.retrieve.return_value = []
            bs.execute_run(sb, run["id"], cases, bs.RETRIEVAL_ONLY, resume=True)

        case_ids_processed = [r["case_id"] for r in sb.store["rag_benchmark_results"] if r.get("question")]
        self.assertNotIn("c1", case_ids_processed)
        self.assertIn("c2", case_ids_processed)


class TestCompareRuns(unittest.TestCase):
    def test_compare_runs_identifies_improved_and_regressed_cases(self):
        sb = _FakeSupabase()
        run_a = {"id": "runA", "run_name": "Baseline", "passed_cases": 1, "total_cases": 2,
                 "average_latency_ms": 100, "estimated_cost": 0.01}
        run_b = {"id": "runB", "run_name": "Candidate", "passed_cases": 1, "total_cases": 2,
                 "average_latency_ms": 90, "estimated_cost": 0.02}
        sb.store["rag_benchmark_runs"] = [run_a, run_b]
        sb.store["rag_benchmark_results"] = [
            {"run_id": "runA", "case_id": "c1", "question": "q1", "status": "fail", "actual_answer": "wrong",
             "retrieved_chunks": [{"file_name": "wrong.md"}]},
            {"run_id": "runA", "case_id": "c2", "question": "q2", "status": "pass", "actual_answer": "ok",
             "retrieved_chunks": [{"file_name": "a.md"}]},
            {"run_id": "runB", "case_id": "c1", "question": "q1", "status": "pass", "actual_answer": "correct",
             "retrieved_chunks": [{"file_name": "a.md"}]},
            {"run_id": "runB", "case_id": "c2", "question": "q2", "status": "fail", "actual_answer": "broke",
             "retrieved_chunks": [{"file_name": "wrong2.md"}]},
        ]

        diff = bs.compare_runs(sb, "runA", "runB")
        self.assertIn("c1", diff["cases_improved"])
        self.assertIn("c2", diff["cases_regressed"])
        self.assertEqual(diff["cases_unchanged_count"], 0)
        self.assertEqual(len(diff["case_diffs"]), 2)


if __name__ == "__main__":
    unittest.main()
