# -*- coding: utf-8 -*-
"""REGRESSION-GATE-1 — the ONE repeatable Regression Gate command.

    python -m tests.run_regression_gate

Composes EXISTING unittest runners and the existing `run_baseline.py`
harness — no new test-running infrastructure. Four layers, run in order:

  1. PROTECTED / FOCUSED SUITES — every unittest module the locked
     checkpoint (`0702e64216fcc75401920cf3f6780dc9fe179b0f`) depends on
     (decision engine, semantics, calculator, charter/TC19, coupon,
     invoice, ERP-read, authorization, the SYSTEM-STATE-EMERGENCY-1
     journey, business-action registry, ...).
  2. CUSTOMER MASTER — `tests/customer_uat/run_baseline.py` in its
     default SCRATCH mode (never touches the tracked baseline files),
     diffed case-by-case against `tests/customer_uat/
     known_baseline_case_status.json` (the locked-checkpoint snapshot).
     A case that flips PASS -> FAIL is a NEW REGRESSION and BLOCKS the
     gate. A case that was already FAIL and stays FAIL is a pre-existing,
     already-classified failure (see `docs/customer_uat_sources/
     CUSTOMER_MASTER_FAILURE_INVENTORY.md`) and does NOT block the gate.
  3. KNOWN REAL-LINE REGRESSION — `tests.test_known_real_line_cases`
     (the RL-* named registry).
  4. CROSS-FLOW MATRIX — `tests.test_cross_flow_matrix` (CF-01..CF-14).

Prints two DIFFERENT verdicts, deliberately not conflated (per
REGRESSION-GATE-1's explicit BASELINE vs GATE distinction):

  - "REGRESSION GATE" — has anything that used to pass at the locked
    checkpoint started failing? This is the deploy/no-deploy gate.
  - "RELEASE READINESS" — of all 69 customer-master logical cases, how
    many pass RIGHT NOW (exact numbers, never "PASS" when some fail).
    This is a separate, honest measurement — it does not gate a
    test/infra-only change, and it is not claimed to be 69/69 just
    because the gate is green.

Exit code: 0 only if the REGRESSION GATE layer (1+3+4, plus "no NEW
Customer Master regression") is entirely clean. A non-zero exit means
BLOCK DEPLOYMENT.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Layer 1 — protected / focused suites (the locked-checkpoint dependency set).
PROTECTED_SUITES = [
    "tests.test_decision_engine",
    "tests.test_conversation_semantics",
    "tests.test_semantic_first_1",
    "tests.test_semantic_first_2",
    "tests.test_semantic_first_2_1",
    "tests.test_customer_calc1",
    "tests.test_customer_calc11_conversation_state",
    "tests.test_customer_rag2_1_charter_slots",
    "tests.test_customer_action1",
    "tests.test_customer_erp_read1",
    "tests.test_customer_uat_fix1_public_private_boundary",
    "tests.test_customer_uat_fix2_unsupported_fact_handoff",
    "tests.test_invoice_product_regression2",
    "tests.test_customer_rag_audit",
    "tests.test_customer_rag1_pickup_location",
    "tests.test_task06_authorization",
    "tests.test_slot_filling_engine",
    "tests.test_sem1_private_state_routing",
    "tests.test_ppc1_referentless_clarification",
    "tests.test_fix23_product_interest_not_no_info",
    "tests.test_hybrid_question_classifier",
    "tests.test_synonym_service",
    "tests.test_system_state_emergency_1",
    "tests.test_calculator_regression_2",
    "tests.test_business_action_registry",
    "tests.test_customer_link1",
    "tests.test_customer_link_real2",
    "tests.test_customer_track_th1",
]

KNOWN_REAL_LINE_SUITE = "tests.test_known_real_line_cases"
CROSS_FLOW_SUITE = "tests.test_cross_flow_matrix"

_SNAPSHOT_PATH = _ROOT / "tests" / "customer_uat" / "known_baseline_case_status.json"


def _run_unittest_modules(module_names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    return result


def _run_customer_master():
    """Layer 2 — Customer Master, via run_baseline.py's default SCRATCH
    mode (no --commit-baseline: the tracked baseline files are never
    touched by the gate)."""
    from tests.customer_uat import run_baseline
    from tests.test_business_action_registry import reset_real_registry
    # TEST-ISOLATION (REGRESSION-GATE-1) — force a fresh real-registry read
    # for this layer too, bounding any transient-timeout blast radius the
    # same way the protected suites' setUpClass already does.
    reset_real_registry()
    run_baseline.main([])   # writes tests/customer_uat/.gate_scratch/*
    fresh = json.loads(run_baseline._SCRATCH_OUT_JSON.read_text(encoding="utf-8"))
    return fresh


def _diff_against_locked_snapshot(fresh):
    if not _SNAPSHOT_PATH.exists():
        return {"snapshot_missing": True, "new_regressions": [], "still_failing": [],
                "newly_fixed": [], "cases_passed": 0, "cases_failed": 0, "total": 0}
    locked = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))["cases"]
    new_regressions, still_failing, newly_fixed = [], [], []
    passed = failed = 0
    for row in fresh["per_case"]:
        cid = row["case_id"]
        now_failed = row["failed"]
        was_failed = locked.get(cid, {}).get("failed")
        if now_failed:
            failed += 1
        else:
            passed += 1
        if was_failed is None:
            continue   # case not in the locked snapshot (new case) — not a regression signal
        if now_failed and not was_failed:
            new_regressions.append(cid)
        elif now_failed and was_failed:
            still_failing.append(cid)
        elif (not now_failed) and was_failed:
            newly_fixed.append(cid)
    return {"snapshot_missing": False, "new_regressions": new_regressions,
            "still_failing": still_failing, "newly_fixed": newly_fixed,
            "cases_passed": passed, "cases_failed": failed, "total": passed + failed}


def main():
    print("=" * 70)
    print("REGRESSION-GATE-1 — repeatable gate run")
    print("=" * 70)

    print("\n[1/4] PROTECTED / FOCUSED SUITES\n")
    protected_result = _run_unittest_modules(PROTECTED_SUITES)

    print("\n[2/4] CUSTOMER MASTER (scratch mode — tracked baseline files untouched)\n")
    fresh_master = _run_customer_master()
    diff = _diff_against_locked_snapshot(fresh_master)

    print("\n[3/4] KNOWN REAL-LINE REGRESSION SUITE\n")
    rl_result = _run_unittest_modules([KNOWN_REAL_LINE_SUITE])

    print("\n[4/4] CROSS-FLOW MATRIX\n")
    cf_result = _run_unittest_modules([CROSS_FLOW_SUITE])

    protected_ok = protected_result.wasSuccessful()
    # expectedFailure/unexpectedSuccess: wasSuccessful() already treats a
    # documented expectedFailure as OK and an unexpectedSuccess as NOT ok
    # (exactly the "loud when it's accidentally fixed" behaviour wanted).
    rl_ok = rl_result.wasSuccessful()
    cf_ok = cf_result.wasSuccessful()
    no_new_master_regression = not diff["new_regressions"]

    gate_clean = protected_ok and rl_ok and cf_ok and no_new_master_regression

    print("\n" + "=" * 70)
    print("REGRESSION GATE  (deploy / no-deploy signal)")
    print("=" * 70)
    print(f"  Protected/focused suites : {'PASS' if protected_ok else 'FAIL'} "
          f"({protected_result.testsRun} tests, {len(protected_result.failures)} failures, "
          f"{len(protected_result.errors)} errors)")
    print(f"  Known REAL-LINE suite    : {'PASS' if rl_ok else 'FAIL'} "
          f"({rl_result.testsRun} tests, {len(rl_result.failures)} failures, "
          f"{len(rl_result.errors)} errors)")
    print(f"  Cross-Flow matrix        : {'PASS' if cf_ok else 'FAIL'} "
          f"({cf_result.testsRun} tests, {len(cf_result.failures)} failures, "
          f"{len(cf_result.errors)} errors)")
    if diff["snapshot_missing"]:
        print("  Customer Master vs locked checkpoint: NO SNAPSHOT FOUND "
              "(tests/customer_uat/known_baseline_case_status.json) — cannot verify zero-new-regression")
    else:
        print(f"  Customer Master vs locked checkpoint: "
              f"{len(diff['new_regressions'])} NEW regressions, "
              f"{len(diff['still_failing'])} pre-existing (unchanged), "
              f"{len(diff['newly_fixed'])} newly fixed")
        if diff["new_regressions"]:
            print(f"    NEW REGRESSIONS: {', '.join(diff['new_regressions'])}")
        if diff["newly_fixed"]:
            print(f"    newly fixed (informational): {', '.join(diff['newly_fixed'])}")
    print()
    if gate_clean:
        print(f"  RESULT: ZERO NEW REGRESSIONS vs locked checkpoint 0702e64216fcc75401920cf3f6780dc9fe179b0f")
    else:
        print(f"  RESULT: REGRESSIONS DETECTED — BLOCK DEPLOYMENT")

    print("\n" + "=" * 70)
    print("RELEASE READINESS  (exact Customer Master numbers — separate from the gate above)")
    print("=" * 70)
    total = diff["total"] or fresh_master["aggregates"]["cases_passed"] + fresh_master["aggregates"]["cases_failed"]
    passed = diff["cases_passed"] or fresh_master["aggregates"]["cases_passed"]
    print(f"  Customer Master: {passed}/{total} logical cases pass "
          f"({fresh_master['aggregates']['overall_case_pass_pct']}%)")
    print(f"  NOT all customer-required cases pass. See "
          f"docs/customer_uat_sources/CUSTOMER_MASTER_FAILURE_INVENTORY.md for the full "
          f"per-case classification of every failing case.")
    print("  REAL LINE TESTED BY CLAUDE: NO — every case remains REAL LINE REQUIRED for final acceptance.")

    return 0 if gate_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
