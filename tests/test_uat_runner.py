"""Unit tests for the UAT/Regression Suite orchestrator
(services/uat_runner.py) — 2026-07-29 reliability sprint.

Tests the RUNNER's OWN logic only: report structure, regression
comparison against a fake previous run, and that a per-case exception
never crashes the whole suite. Never hits the real DB or a real LLM —
uses the same _FakeSupabase mock as the rest of this project's Business
Action tests (tests/test_business_action_registry.py) plus
unittest.mock.patch over the RAG/ERP entry points, so this file is fast
and deterministic, unlike an actual `python scripts/run_uat_suite.py`
run (which legitimately calls the real LLM and is exercised separately,
by hand, not by this automated test file).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase, _sample_payload
from services.business_action_registry import BusinessActionRegistry
import services.uat_runner as uat_runner
from services.uat_runner import (
    _case_result, _run_case_safely, _eval_rag_checks, _compute_regressions,
    run_rag_suite, run_erp_suite,
)


def _fake_playground_result(**overrides):
    defaults = dict(
        answer="ตัวอย่างคำตอบ", chunks=[{"cited": True, "citation": "Source: foo.xlsx", "source": "foo.xlsx"}],
        confidence=0.9, confidence_label="High", answerability="direct_answer",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestCaseResultHelpers(unittest.TestCase):
    def test_case_result_shape(self):
        r = _case_result("id1", "cat1", "pass", expected={"a": 1}, actual={"a": 1}, reason="ok", latency_ms=12.3)
        self.assertEqual(r["id"], "id1")
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["expected"], {"a": 1})
        self.assertEqual(r["actual"], {"a": 1})
        self.assertIsNone(r["traceback"])

    def test_run_case_safely_catches_exception_as_fail_with_traceback(self):
        def _boom():
            raise ValueError("simulated failure inside a case")

        result = _run_case_safely("case_x", "Category X", _boom)
        self.assertEqual(result["status"], "fail")
        self.assertIn("simulated failure inside a case", result["reason"])
        self.assertIsNotNone(result["traceback"])
        self.assertIn("ValueError", result["traceback"])

    def test_run_case_safely_passes_through_normal_result(self):
        def _ok():
            return _case_result("case_y", "Category Y", "pass", reason="fine")

        result = _run_case_safely("case_y", "Category Y", _ok)
        self.assertEqual(result["status"], "pass")


class TestEvalRagChecks(unittest.TestCase):
    def test_chunks_retrieved_check_fails_when_empty(self):
        result = _fake_playground_result(chunks=[])
        failures = _eval_rag_checks(["chunks_retrieved"], result)
        self.assertTrue(failures)

    def test_chunks_retrieved_check_passes_when_present(self):
        result = _fake_playground_result()
        failures = _eval_rag_checks(["chunks_retrieved"], result)
        self.assertEqual(failures, [])

    def test_citation_present_check(self):
        no_citation = _fake_playground_result(chunks=[{"cited": False}])
        self.assertTrue(_eval_rag_checks(["citation_present"], no_citation))
        with_citation = _fake_playground_result()
        self.assertEqual(_eval_rag_checks(["citation_present"], with_citation), [])

    def test_confidence_min_and_max(self):
        result = _fake_playground_result(confidence=0.9)
        self.assertEqual(_eval_rag_checks(["confidence_min:0.5"], result), [])
        self.assertTrue(_eval_rag_checks(["confidence_min:0.95"], result))
        self.assertTrue(_eval_rag_checks(["confidence_max:0.5"], result))

    def test_confidence_range(self):
        result = _fake_playground_result(confidence=0.6)
        self.assertEqual(_eval_rag_checks(["confidence_range:0.3,0.75"], result), [])
        self.assertTrue(_eval_rag_checks(["confidence_range:0.7,0.9"], result))

    def test_answerability_is_and_in(self):
        result = _fake_playground_result(answerability="no_information")
        self.assertEqual(_eval_rag_checks(["answerability_is:no_information"], result), [])
        self.assertTrue(_eval_rag_checks(["answerability_is:direct_answer"], result))
        self.assertEqual(_eval_rag_checks(["answerability_in:no_information,partial_answer"], result), [])

    def test_no_information_phrase_present(self):
        result = _fake_playground_result(answer="ยังไม่มีข้อมูลเกี่ยวกับเรื่องนี้ค่ะ")
        self.assertEqual(_eval_rag_checks(["no_information_phrase_present"], result), [])
        result2 = _fake_playground_result(answer="คำตอบทั่วไปที่ไม่มีวลีที่คาดหวัง")
        self.assertTrue(_eval_rag_checks(["no_information_phrase_present"], result2))

    def test_unknown_check_is_reported_not_silently_ignored(self):
        result = _fake_playground_result()
        failures = _eval_rag_checks(["not_a_real_check"], result)
        self.assertTrue(any("unknown check" in f for f in failures))


class TestRunRagSuiteNeverCrashesOnException(unittest.TestCase):
    def test_one_broken_case_does_not_abort_the_whole_suite(self):
        """If run_playground_turn raises for one case, every OTHER case
        must still run and the broken one must be recorded as a FAIL
        with a real traceback — never an unhandled exception that kills
        the whole suite (Part 5's core requirement)."""
        call_count = {"n": 0}

        def _side_effect(question, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated LLM outage")
            return _fake_playground_result()

        with patch("services.playground_orchestrator.run_playground_turn", side_effect=_side_effect):
            results = run_rag_suite()

        self.assertGreater(len(results), 1)
        self.assertEqual(results[0]["status"], "fail")
        self.assertIn("simulated LLM outage", results[0]["reason"])
        self.assertIsNotNone(results[0]["traceback"])
        # every subsequent case still ran (not aborted)
        self.assertTrue(any(r["status"] != "fail" or "simulated LLM outage" not in (r["reason"] or "") for r in results[1:]))


class TestRunErpSuiteWithFakeRegistry(unittest.TestCase):
    def _make_registry_with_one_action(self):
        sb = _FakeSupabase()
        reg = BusinessActionRegistry(sb)
        action = reg.create(_sample_payload("uat_fixture_action"))
        reg.replace_parameters(action["id"], [
            {"name": "OrderNo", "display_name": "Order Number", "required": True, "input_source": "customer_message"},
        ])
        reg.upsert_execution(action["id"], {"endpoint": "https://fixture.internal/order/lookup", "http_method": "GET"})
        reg.replace_response_mapping(action["id"], [{"json_path": "data.status", "mapped_label": "Order Status"}])
        return sb, action

    def test_erp_suite_runs_generic_checks_against_fake_action(self):
        sb, action = self._make_registry_with_one_action()

        def _fake_run_erp_test(*, sb, action_id, message, mode, **kwargs):
            if "OrderNo" in message or "12345" in message:
                return {"ok": True, "summary": {"overall": "pass", "total_latency_ms": 5.0},
                         "trace": [], "answer": "สถานะ: กำลังจัดส่ง", "collected_params": {"OrderNo": "12345"},
                         "missing_parameters": [], "clarification_question": None,
                         "normalized_result": {"order.status": {"value": "shipped"}}}
            return {"ok": True, "summary": {"overall": "warning", "total_latency_ms": 3.0},
                     "trace": [], "answer": None, "missing_parameters": ["OrderNo"],
                     "clarification_question": "กรุณาระบุเลขที่ออเดอร์ค่ะ"}

        with patch("services.erp_test_harness.run_erp_test", side_effect=_fake_run_erp_test):
            results = run_erp_suite(sb)

        ids = [r["id"] for r in results]
        self.assertIn("erp_uat_fixture_action_success", ids)
        self.assertIn("erp_uat_fixture_action_missing_param", ids)
        self.assertIn("erp_uat_fixture_action_classification", ids)
        self.assertIn("erp_bogus_action_id", ids)
        # No validation_pattern / no parameter group configured on this fixture -> both must be SKIPPED, never faked.
        invalid = next(r for r in results if r["id"] == "erp_uat_fixture_action_invalid_param")
        self.assertEqual(invalid["status"], "skipped")
        group = next(r for r in results if r["id"] == "erp_uat_fixture_action_at_least_one_group")
        self.assertEqual(group["status"], "skipped")

    def test_erp_suite_handles_exception_from_harness_without_crashing(self):
        sb, action = self._make_registry_with_one_action()

        with patch("services.erp_test_harness.run_erp_test", side_effect=RuntimeError("simulated harness crash")):
            results = run_erp_suite(sb)

        self.assertGreater(len(results), 0)
        failed = [r for r in results if r["status"] == "fail"]
        self.assertTrue(any("simulated harness crash" in (r["reason"] or "") for r in failed))


class TestComputeRegressions(unittest.TestCase):
    def test_no_previous_run(self):
        current = {"rag_results": [], "erp_results": [], "hybrid_results": [], "overall": {"avg_latency_ms": 100}}
        reg = _compute_regressions(None, current)
        self.assertFalse(reg["has_previous_run"])
        self.assertEqual(reg["new_failures"], [])
        self.assertEqual(reg["fixed_failures"], [])

    def test_new_failure_and_fixed_failure_detected(self):
        previous = {
            "run_id": "run_prev",
            "rag_results": [
                _case_result("case_a", "Cat A", "pass"),
                _case_result("case_b", "Cat B", "fail", reason="was broken"),
            ],
            "erp_results": [], "hybrid_results": [],
            "overall": {"avg_latency_ms": 100.0},
        }
        current = {
            "rag_results": [
                _case_result("case_a", "Cat A", "fail", reason="newly broken"),
                _case_result("case_b", "Cat B", "pass"),
            ],
            "erp_results": [], "hybrid_results": [],
            "overall": {"avg_latency_ms": 150.0},
        }
        reg = _compute_regressions(previous, current)
        self.assertTrue(reg["has_previous_run"])
        self.assertEqual(len(reg["new_failures"]), 1)
        self.assertEqual(reg["new_failures"][0]["id"], "case_a")
        self.assertEqual(len(reg["fixed_failures"]), 1)
        self.assertEqual(reg["fixed_failures"][0]["id"], "case_b")
        self.assertEqual(reg["latency_delta_ms"], 50.0)

    def test_case_missing_in_previous_run_is_not_reported_as_regression(self):
        previous = {"rag_results": [_case_result("case_a", "Cat A", "pass")], "erp_results": [], "hybrid_results": [],
                    "overall": {"avg_latency_ms": 100.0}}
        current = {"rag_results": [_case_result("case_a", "Cat A", "pass"),
                                    _case_result("case_new", "Cat New", "fail", reason="brand new case, not a regression")],
                   "erp_results": [], "hybrid_results": [], "overall": {"avg_latency_ms": 100.0}}
        reg = _compute_regressions(previous, current)
        self.assertEqual(reg["new_failures"], [])


class TestRunFullUatSuiteWritesRunFile(unittest.TestCase):
    def test_run_saved_to_disk_and_regression_computed_on_second_run(self):
        sb, _ = TestRunErpSuiteWithFakeRegistry()._make_registry_with_one_action()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uat_runner, "RUNS_DIR", tmp), \
                 patch("services.playground_orchestrator.run_playground_turn", return_value=_fake_playground_result()), \
                 patch("services.erp_test_harness.run_erp_test", return_value={
                     "ok": True, "summary": {"overall": "pass", "total_latency_ms": 5.0}, "trace": [],
                     "answer": "ok", "missing_parameters": [], "clarification_question": None,
                     "normalized_result": {}}):
                report1 = uat_runner.run_full_uat_suite(sb=sb)
                saved_files = list(Path(tmp).glob("run_*.json"))
                self.assertEqual(len(saved_files), 1)
                with open(saved_files[0], encoding="utf-8") as f:
                    on_disk = json.load(f)
                self.assertEqual(on_disk["run_id"], report1["run_id"])
                self.assertFalse(report1["regressions"]["has_previous_run"])

                report2 = uat_runner.run_full_uat_suite(sb=sb)
                self.assertTrue(report2["regressions"]["has_previous_run"])
                self.assertEqual(report2["regressions"]["previous_run_id"], report1["run_id"])


if __name__ == "__main__":
    unittest.main()
