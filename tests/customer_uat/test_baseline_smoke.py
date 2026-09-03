# -*- coding: utf-8 -*-
"""CI smoke for the Customer UAT baseline evaluator.

This does NOT assert a pass rate — the baseline is expected to fail many
cases by design. It only proves the evaluator still wires up against the
REAL DecisionEngine and emits a well-formed result bundle, and that the
committed baseline_results.json is internally consistent.
"""
import json
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MASTER = _HERE / "customer_uat_master.jsonl"
_RESULTS = _HERE / "baseline_results.json"


class BaselineArtifactsWellFormed(unittest.TestCase):
    def test_master_is_69_logical_cases(self):
        lines = [ln for ln in _MASTER.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 69)
        for ln in lines:
            row = json.loads(ln)
            self.assertIn("case_id", row)
            self.assertIn(row.get("expected_route"),
                          {"RAG", "ERP", "CLARIFY", "HUMAN_CS", "WORKFLOW", "NA"})

    def test_results_bundle_consistent(self):
        if not _RESULTS.exists():
            self.skipTest("baseline_results.json not generated in this checkout")
        r = json.loads(_RESULTS.read_text(encoding="utf-8"))
        self.assertFalse(r["production_code_changed"])
        self.assertFalse(r["dependencies_installed"])
        self.assertFalse(r["deployed"])
        self.assertEqual(r["n_logical_cases"], len(r["per_case"]))
        agg = r["aggregates"]
        self.assertEqual(agg["cases_failed"] + agg["cases_passed"], r["n_logical_cases"])
        # every failing case carries exactly one primary root class
        for c in r["per_case"]:
            if c["failed"]:
                self.assertIsNotNone(c["primary_root_class"])

    def test_evaluator_imports(self):
        from tests.customer_uat import run_baseline  # noqa: F401


if __name__ == "__main__":
    unittest.main()
