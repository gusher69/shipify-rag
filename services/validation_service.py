"""AI Production Validation Center — a thin ORCHESTRATION layer over the
existing AI Evaluation framework (services/benchmark_service.py). Never
re-implements retrieval, conversation intelligence, or benchmark
scoring — it only sequences the 4 existing benchmark modes
(QUERY_UNDERSTANDING -> RETRIEVAL_ONLY -> CONVERSATION_SCENARIO ->
FULL_RAG), reuses services.benchmark_service.compare_runs() against the
frozen Production Baseline v1.0 run IDs (baseline_v1.0.json, produced by
the AI Engine freeze), and assembles the results into one scorecard +
markdown/JSON report.

Execution model mirrors benchmark_service.py's own in-memory progress
dict + threading.Thread pattern (`_VALIDATION_STATE`/`_VALIDATION_LOCK`)
rather than introducing a second job-queue mechanism.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services import benchmark_service as bs
from services import benchmark_metrics as metrics

_BASELINE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseline_v1.0.json")

_STEPS = [
    ("query_understanding", "Query Understanding", bs.QUERY_UNDERSTANDING),
    ("retrieval_only", "Retrieval Only", bs.RETRIEVAL_ONLY),
    ("conversation_scenario", "Conversation Scenario", bs.CONVERSATION_SCENARIO),
    ("full_rag", "Full RAG", bs.FULL_RAG),
]

_VALIDATION_STATE: Dict[str, Dict] = {}
_VALIDATION_LOCK = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_baseline() -> Optional[Dict]:
    """Reads the frozen Production Baseline v1.0 snapshot (created by
    the AI Engine freeze task) — never regenerates or mutates it. Returns
    None if it doesn't exist yet (a validation can still run; Step 5/6
    simply report "no baseline to compare against")."""
    if not os.path.exists(_BASELINE_FILE):
        return None
    try:
        with open(_BASELINE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_validation_progress(validation_id: str) -> Optional[Dict]:
    with _VALIDATION_LOCK:
        st = _VALIDATION_STATE.get(validation_id)
        return dict(st) if st else None


def cancel_validation(validation_id: str) -> bool:
    with _VALIDATION_LOCK:
        if validation_id in _VALIDATION_STATE:
            _VALIDATION_STATE[validation_id]["cancelled"] = True
            return True
    return False


def _wait_for_run(run_id: str, poll_interval: float = 1.0, cancel_check=None) -> Dict:
    """Blocks until a benchmark_service run reaches a terminal status,
    reusing benchmark_service's own progress tracking — never a second
    run-status mechanism."""
    while True:
        progress = bs.get_run_progress(run_id) or {}
        if progress.get("status") in ("completed", "cancelled") or cancel_check and cancel_check():
            return progress
        time.sleep(poll_interval)


def _run_summary(sb, run_id: str) -> Dict:
    rows = sb.table("rag_benchmark_runs").select("*").eq("id", run_id).execute().data
    return rows[0] if rows else {}


def _aggregate_case_metric(sb, run_id: str, jsonb_column: str, extractor) -> Optional[float]:
    """Reads every result row for `run_id` and averages whatever
    `extractor(row)` returns (None values are skipped) — a thin
    read-only aggregation over columns benchmark_service.py already
    populates; never a new metric computation."""
    rows = sb.table("rag_benchmark_results").select(jsonb_column).eq("run_id", run_id).execute().data or []
    values = [v for v in (extractor(r) for r in rows) if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _scorecard(sb, run_ids: Dict[str, str]) -> Dict:
    """Builds Part 5's scorecard from the 4 completed runs' own stored
    results — Conversation % (Conversation Scenario turn_pass_rate),
    Retrieval % (Retrieval Only pass rate), Grounding % (Full RAG
    grounding status), Critical Facts % (Full RAG critical_fact pass
    rate). Every number is read directly from rows benchmark_service.py
    already wrote — nothing here recomputes a metric."""
    card: Dict[str, Optional[float]] = {}

    ro_run = _run_summary(sb, run_ids["retrieval_only"])
    card["retrieval"] = round(100 * (ro_run.get("passed_cases") or 0) / ro_run["total_cases"], 1) if ro_run.get("total_cases") else None

    conv_avg = _aggregate_case_metric(
        sb, run_ids["conversation_scenario"], "conversation_metrics",
        lambda r: (r.get("conversation_metrics") or {}).get("turn_pass_rate"),
    )
    card["conversation"] = round(conv_avg * 100, 1) if conv_avg is not None else None

    # Grounding/Critical Facts are read from CONVERSATION_SCENARIO, not
    # FULL_RAG — services.benchmark_service.run_full_rag_case() doesn't
    # currently populate grounding_metrics/critical_fact_metrics (only
    # run_conversation_scenario() does; a pre-existing benchmark-logic
    # gap, out of scope to fix in this orchestration-only task). Reading
    # from the run that actually has the data avoids a misleading
    # "n/a" scorecard without touching benchmark_service.py at all.
    ground_rate = _aggregate_case_metric(
        sb, run_ids["conversation_scenario"], "grounding_metrics",
        lambda r: (1.0 if (r.get("grounding_metrics") or {}).get("status") in ("supported", "partially_supported")
                   else (0.0 if (r.get("grounding_metrics") or {}).get("status") not in (None, "not_applicable") else None)),
    )
    card["grounding"] = round(ground_rate * 100, 1) if ground_rate is not None else None

    crit_rate = _aggregate_case_metric(
        sb, run_ids["conversation_scenario"], "critical_fact_metrics",
        lambda r: (1.0 if (r.get("critical_fact_metrics") or {}).get("pass")
                   else (0.0 if (r.get("critical_fact_metrics") or {}).get("total_count") else None)),
    )
    card["critical_facts"] = round(crit_rate * 100, 1) if crit_rate is not None else None

    return card


def _regression_summary(sb, run_ids: Dict[str, str], baseline: Optional[Dict]) -> Dict:
    """Reuses services.benchmark_service.compare_runs() for every mode
    that has a recorded baseline run — never a second comparison
    implementation."""
    if not baseline:
        return {"available": False, "reason": "No Production Baseline found (baseline_v1.0.json missing) — "
                                                 "run the AI Engine freeze first to enable regression checking."}

    baseline_runs = baseline.get("runs", {})
    mode_key_by_step = {"query_understanding": "QUERY_UNDERSTANDING", "retrieval_only": "RETRIEVAL_ONLY",
                         "conversation_scenario": "CONVERSATION_SCENARIO", "full_rag": "FULL_RAG"}
    result = {"available": True, "per_mode": {}}
    any_regressed = False
    for step_key, mode_key in mode_key_by_step.items():
        baseline_run_id = (baseline_runs.get(mode_key) or {}).get("run_id")
        candidate_run_id = run_ids.get(step_key)
        if not baseline_run_id or not candidate_run_id:
            result["per_mode"][step_key] = {"comparable": False}
            continue
        try:
            diff = bs.compare_runs(sb, baseline_run_id, candidate_run_id)
        except Exception as e:
            result["per_mode"][step_key] = {"comparable": False, "error": str(e)}
            continue
        regressed = len(diff.get("cases_regressed") or [])
        if regressed > 0:
            any_regressed = True
        result["per_mode"][step_key] = {
            "comparable": True, "pass_rate_change": diff["pass_rate_change"],
            "improved": len(diff["cases_improved"]), "regressed": regressed,
            "unchanged": diff["cases_unchanged_count"], "metric_comparison": diff.get("metric_comparison"),
            "latency_change_ms": diff["latency_change_ms"], "cost_change": diff["cost_change"],
        }
    result["overall"] = "REGRESSED" if any_regressed else "NONE"
    return result


def _failure_summary(sb, run_ids: Dict[str, str], max_per_mode: int = 10) -> List[Dict]:
    """Every failed/error result row across all 4 runs, in the exact
    shape Part 7 wants shown (no raw JSON forced on the developer) —
    reads columns benchmark_service.py already populated."""
    failures = []
    for step_key, run_id in run_ids.items():
        rows = sb.table("rag_benchmark_results").select("*").eq("run_id", run_id).neq("status", "pass") \
            .limit(max_per_mode).execute().data or []
        for r in rows:
            failures.append({
                "mode": step_key, "question": r.get("question"),
                "expected_answer": r.get("expected_answer"), "actual_answer": r.get("actual_answer"),
                "conversation_state": ((r.get("query_understanding_metrics") or {}).get("conversation_state") or {}).get("actual"),
                "entities": ((r.get("query_understanding_metrics") or {}).get("entities") or {}).get("actual"),
                "canonical_query": ((r.get("query_understanding_metrics") or {}).get("canonical_query") or {}).get("actual"),
                "retrieved_chunks": [c.get("file_name") for c in (r.get("retrieved_chunks") or [])][:5],
                "grounding": (r.get("grounding_metrics") or {}).get("status"),
                "critical_facts": (r.get("critical_fact_metrics") or {}).get("facts"),
                "failure_reason": r.get("failure_reason"), "failure_type": r.get("failure_type"),
            })
    return failures


def _render_markdown_report(validation_id: str, run_ids: Dict[str, str], scorecard: Dict,
                             regression: Dict, failures: List[Dict], baseline: Optional[Dict],
                             config_snapshot: Dict, overall_status: str, tests_status: str) -> str:
    lines = [
        "# AI Production Validation Report", "",
        f"**Validation ID**: `{validation_id}`  ",
        f"**Generated**: {_now_iso()}  ",
        f"**Overall Status**: **{overall_status}**", "",
        "## Scorecard", "",
        "| Dimension | Score |", "|---|---|",
    ]
    for label, key in [("Conversation", "conversation"), ("Retrieval", "retrieval"),
                        ("Grounding", "grounding"), ("Critical Facts", "critical_facts")]:
        val = scorecard.get(key)
        lines.append(f"| {label} | {val}% |" if val is not None else f"| {label} | n/a |")
    lines += [
        f"| Regression | {regression.get('overall', 'N/A')} |",
        f"| Benchmark | {'PASS' if overall_status == 'PASS' else 'FAIL'} |",
        f"| Tests | {tests_status} |",
        "", "## Run IDs", "",
    ]
    for step_key, run_id in run_ids.items():
        lines.append(f"- **{step_key}**: `{run_id}`")
    lines += ["", "## Configuration", ""]
    for k, v in (config_snapshot or {}).items():
        lines.append(f"- **{k}**: {v}")
    lines += ["", "## Regression Summary", ""]
    if not regression.get("available"):
        lines.append(regression.get("reason", "No baseline available."))
    else:
        for step_key, m in regression.get("per_mode", {}).items():
            if not m.get("comparable"):
                lines.append(f"- **{step_key}**: not comparable")
                continue
            lines.append(f"- **{step_key}**: pass_rate_change={m['pass_rate_change']:+.2%}, "
                          f"improved={m['improved']}, regressed={m['regressed']}, unchanged={m['unchanged']}, "
                          f"latency_change={m['latency_change_ms']:+.1f}ms, cost_change=${m['cost_change']:+.4f}")
    lines += ["", "## Failure Summary", ""]
    if not failures:
        lines.append("No failures.")
    else:
        for f in failures[:30]:
            lines.append(f"- **[{f['mode']}]** {f['question']!r} — {f.get('failure_type') or 'fail'}: "
                          f"{f.get('failure_reason') or ''}")
    lines += ["", f"**Production Ready**: {'YES' if overall_status == 'PASS' else 'NO'}"]
    return "\n".join(lines)


def _execute_validation(sb, validation_id: str, dataset_id: str, top_k: int = 3):
    baseline = load_baseline()
    started_at = time.time()
    run_ids: Dict[str, str] = {}

    with _VALIDATION_LOCK:
        _VALIDATION_STATE[validation_id]["dataset_id"] = dataset_id

    for step_key, step_label, mode in _STEPS:
        with _VALIDATION_LOCK:
            if _VALIDATION_STATE[validation_id]["cancelled"]:
                _VALIDATION_STATE[validation_id]["status"] = "cancelled"
                return
            _VALIDATION_STATE[validation_id]["current_step"] = step_key
            _VALIDATION_STATE[validation_id]["current_step_label"] = step_label
            _VALIDATION_STATE[validation_id]["current_mode"] = mode

        try:
            run_id = bs.start_run(sb, dataset_id=dataset_id,
                                   run_name=f"Production Validation {validation_id[:8]} - {step_label}",
                                   mode=mode, top_k=top_k)
        except Exception as e:
            with _VALIDATION_LOCK:
                _VALIDATION_STATE[validation_id]["status"] = "failed"
                _VALIDATION_STATE[validation_id]["error"] = f"{step_label} failed to start: {e}"
            return

        run_ids[step_key] = run_id
        with _VALIDATION_LOCK:
            _VALIDATION_STATE[validation_id]["run_ids"] = dict(run_ids)

        def _cancelled():
            with _VALIDATION_LOCK:
                return _VALIDATION_STATE[validation_id]["cancelled"]

        _wait_for_run(run_id, cancel_check=_cancelled)

        with _VALIDATION_LOCK:
            _VALIDATION_STATE[validation_id]["completed_steps"] = _VALIDATION_STATE[validation_id].get("completed_steps", []) + [step_key]
            if _VALIDATION_STATE[validation_id]["cancelled"]:
                _VALIDATION_STATE[validation_id]["status"] = "cancelled"
                return

    # Step 5 + 6 — Compare with Production Baseline + Regression Analysis
    with _VALIDATION_LOCK:
        _VALIDATION_STATE[validation_id]["current_step"] = "compare"
        _VALIDATION_STATE[validation_id]["current_step_label"] = "Compare with Production Baseline"
    regression = _regression_summary(sb, run_ids, baseline)

    scorecard = _scorecard(sb, run_ids)
    failures = _failure_summary(sb, run_ids)

    overall_status = "PASS"
    if regression.get("overall") == "REGRESSED":
        overall_status = "FAIL"
    for v in scorecard.values():
        if v is not None and v < 50:
            overall_status = "FAIL"

    from services.embedding_service import get_embedding_provider
    provider = get_embedding_provider()
    config_snapshot = {
        "embedding_model": provider.model_name(), "embedding_dimensions": provider.dimensions(),
        "chat_model": __import__("config").OPENAI_CHAT_MODEL,
        "benchmark_version": "AI Evaluation Phase 2",
        "engine_version": "AI Engine v1.0.0",
    }

    # Step 7 — Generate Report
    with _VALIDATION_LOCK:
        _VALIDATION_STATE[validation_id]["current_step"] = "report"
        _VALIDATION_STATE[validation_id]["current_step_label"] = "Generate Report"

    report_markdown = _render_markdown_report(
        validation_id, run_ids, scorecard, regression, failures, baseline, config_snapshot,
        overall_status, tests_status="NOT_RUN (run `python -m unittest discover -s tests` separately)",
    )
    report_json = {
        "validation_id": validation_id, "generated_at": _now_iso(), "overall_status": overall_status,
        "run_ids": run_ids, "scorecard": scorecard, "regression": regression, "failures": failures,
        "configuration": config_snapshot,
    }

    elapsed = round(time.time() - started_at, 1)
    with _VALIDATION_LOCK:
        _VALIDATION_STATE[validation_id].update({
            "status": "completed", "current_step": "done", "current_step_label": "Completed",
            "scorecard": scorecard, "regression": regression, "failures": failures,
            "report_markdown": report_markdown, "report_json": report_json,
            "elapsed_seconds": elapsed, "overall_status": overall_status,
        })


def start_validation(sb, *, dataset_id: str, top_k: int = 3) -> str:
    """Kicks off the full 7-step Production Validation in a background
    thread — mirrors benchmark_service.start_run()'s own
    "return immediately, poll for progress" contract."""
    import uuid
    validation_id = str(uuid.uuid4())
    with _VALIDATION_LOCK:
        _VALIDATION_STATE[validation_id] = {
            "status": "running", "current_step": _STEPS[0][0], "current_step_label": _STEPS[0][1],
            "current_mode": _STEPS[0][2], "completed_steps": [], "run_ids": {}, "cancelled": False,
            "started_at": time.time(), "total_steps": len(_STEPS) + 2,  # + compare + report
        }
    thread = threading.Thread(target=_execute_validation, args=(sb, validation_id, dataset_id, top_k), daemon=True)
    thread.start()
    return validation_id
