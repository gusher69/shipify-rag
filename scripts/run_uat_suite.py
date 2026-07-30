"""One-command CLI entry point for the UAT / Regression Test Suite
(2026-07-29 reliability sprint). Calls the exact same
services/uat_runner.py::run_full_uat_suite() the Developer Page
(admin/templates/uat_dashboard.html + admin/routes.py's
/admin/api/uat/run) uses — no separate logic, just a readable console
summary + a nonzero exit code on any real FAIL, for CI/manual use.

Usage:
    python scripts/run_uat_suite.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _print_section(title, results):
    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_skip = sum(1 for r in results if r["status"] == "skipped")
    print(f"\n{title}: {n_pass} pass, {n_fail} fail, {n_skip} skipped ({len(results)} total)")
    for r in results:
        marker = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}[r["status"]]
        print(f"  [{marker:4}] {r['id']:38} {r['category']}")
        if r["status"] == "fail":
            print(f"           reason: {r['reason']}")
        elif r["status"] == "skipped" and r["reason"]:
            print(f"           reason: {r['reason']}")


def main():
    from admin.routes import get_sb
    from services.uat_runner import run_full_uat_suite

    def _progress(module, case_name):
        print(f"[running] {module}: {case_name}", flush=True)

    print("Starting full UAT suite (RAG -> ERP -> Hybrid). Real LLM/embedding calls are made for RAG/ERP/Hybrid "
          "cases — this can take a minute or more.\n")

    report = run_full_uat_suite(sb=get_sb(), progress_cb=_progress)

    _print_section("RAG", report["rag_results"])
    _print_section("ERP", report["erp_results"])
    _print_section("Hybrid", report["hybrid_results"])

    o = report["overall"]
    print("\n" + "=" * 60)
    print(f"Run ID: {report['run_id']}")
    print(f"Overall: {o['pass']} pass, {o['fail']} fail, {o['skipped']} skipped ({o['total']} total)")
    print(f"Duration: {o['duration_seconds']}s, avg case latency: {o['avg_latency_ms']}ms")

    reg = report["regressions"]
    print("\nRegression comparison:")
    if not reg.get("has_previous_run"):
        print(f"  {reg.get('note')}")
    else:
        print(f"  Compared against run {reg['previous_run_id']}")
        print(f"  New failures: {len(reg['new_failures'])}")
        for f in reg["new_failures"]:
            print(f"    - {f['id']}: {f['reason']}")
        print(f"  Fixed since last run: {len(reg['fixed_failures'])}")
        for f in reg["fixed_failures"]:
            print(f"    - {f['id']}")
        print(f"  Avg latency: {reg['avg_latency_ms_previous']}ms -> {reg['avg_latency_ms_current']}ms "
              f"(delta {reg['latency_delta_ms']}ms)")

    print("=" * 60)

    if o["fail"] > 0:
        print(f"\nRESULT: FAIL ({o['fail']} failing case(s))")
        sys.exit(1)
    print("\nRESULT: PASS (no failing cases; see SKIPPED above for anything not executed)")
    sys.exit(0)


if __name__ == "__main__":
    main()
