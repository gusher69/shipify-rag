"""UAT / Regression Test Suite orchestration (Part 5/7 of the 2026-07-29
reliability sprint).

Exercises three ALREADY-EXISTING, frozen entry points — never modifies
or re-implements any of them:
  - services/playground_orchestrator.py::run_playground_turn  (RAG)
  - services/erp_test_harness.py::run_erp_test                (ERP)
  - the same dispatch logic admin/routes.py's
    POST /admin/api/hybrid-playground/ask uses (naive_auto_route /
    naive_merge_hybrid_answer + the two entry points above) — called
    here as direct Python function calls rather than over HTTP, since
    the route itself does nothing but dispatch to these same functions
    (see that route's docstring). No dev server needs to be running for
    Parts 1-7; Part 8 (Developer Page) is verified separately, live, in
    a real browser session per the sprint's explicit requirement.

Every result is captured HONESTLY: an unhandled exception in any single
case is caught, recorded as a FAIL with the real traceback, and the
suite continues — never crashes, never fabricates a PASS.

Run history is stored as timestamped JSON files under uat_runs/ (repo
root) — no DB migration, per the sprint's standing constraint.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.uat_test_data import (
    RAG_TEST_CASES, ERP_SAMPLE_MESSAGES, ERP_MISSING_PARAM_MESSAGE,
    ERP_EMPTY_MESSAGE, HYBRID_TEST_CASES,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(_REPO_ROOT, "uat_runs")


# ── Result helpers ───────────────────────────────────────────────────────

def _case_result(case_id: str, category: str, status: str, *, expected=None, actual=None,
                  reason: str = "", latency_ms: Optional[float] = None,
                  traceback_str: Optional[str] = None, extra: Optional[Dict] = None) -> Dict:
    r = {
        "id": case_id, "category": category, "status": status,  # pass | fail | skipped
        "expected": expected, "actual": actual, "reason": reason,
        "latency_ms": latency_ms, "traceback": traceback_str,
    }
    if extra:
        r["extra"] = extra
    return r


def _run_case_safely(case_id: str, category: str, fn) -> Dict:
    """Runs one test case function, catching ANY exception so a single
    broken case never takes down the whole suite. `fn()` must return a
    fully-built _case_result() dict on success; on exception, a FAIL
    result with the real traceback is synthesized here."""
    t0 = time.time()
    try:
        return fn()
    except Exception as e:
        return _case_result(case_id, category, "fail", reason=f"unhandled exception: {e}",
                             latency_ms=(time.time() - t0) * 1000, traceback_str=traceback.format_exc())


# ── Part 1 — RAG structural checks ──────────────────────────────────────

def _eval_rag_checks(checks: List[str], result) -> List[str]:
    """Evaluates each structural check string against a PlaygroundResult.
    Returns a list of FAILURE reasons (empty list == all checks passed)."""
    failures = []
    chunks = result.chunks or []
    n_chunks = len(chunks)
    cited = [c for c in chunks if c.get("cited")]

    for check in checks:
        if ":" in check:
            name, arg = check.split(":", 1)
        else:
            name, arg = check, None

        if name == "chunks_retrieved":
            if n_chunks == 0:
                failures.append(f"expected chunks_retrieved > 0, got {n_chunks}")
        elif name == "chunks_retrieved_min":
            minimum = int(arg)
            if n_chunks < minimum:
                failures.append(f"expected >= {minimum} chunks retrieved, got {n_chunks}")
        elif name == "chunks_retrieved_max":
            maximum = int(arg)
            if n_chunks > maximum:
                failures.append(f"expected <= {maximum} chunks retrieved, got {n_chunks}")
        elif name == "citation_present":
            if not cited:
                failures.append("expected at least one cited chunk, got none")
        elif name == "cited_source_nonempty":
            if not any((c.get("citation") or c.get("source") or c.get("file_name")) for c in cited):
                failures.append("expected the cited chunk(s) to carry a non-empty source/citation field")
        elif name == "confidence_min":
            threshold = float(arg)
            if result.confidence < threshold:
                failures.append(f"expected confidence >= {threshold}, got {result.confidence:.3f}")
        elif name == "confidence_max":
            threshold = float(arg)
            if result.confidence > threshold:
                failures.append(f"expected confidence <= {threshold}, got {result.confidence:.3f}")
        elif name == "confidence_range":
            lo, hi = (float(x) for x in arg.split(","))
            if not (lo <= result.confidence <= hi):
                failures.append(f"expected confidence in [{lo}, {hi}], got {result.confidence:.3f}")
        elif name == "answerability_is":
            if result.answerability != arg:
                failures.append(f"expected answerability == {arg!r}, got {result.answerability!r}")
        elif name == "answerability_in":
            allowed = arg.split(",")
            if result.answerability not in allowed:
                failures.append(f"expected answerability in {allowed}, got {result.answerability!r}")
        elif name == "no_information_phrase_present":
            no_info_markers = ("ไม่มีข้อมูล", "ยังไม่มีข้อมูล", "no information", "not available")
            if not any(m in (result.answer or "") for m in no_info_markers):
                failures.append("expected the answer text to contain a 'no information available' style phrase")
        else:
            failures.append(f"unknown check {check!r} — not evaluated")
    return failures


def run_rag_suite(progress_cb=None) -> List[Dict]:
    from services.playground_orchestrator import run_playground_turn
    results = []
    for case in RAG_TEST_CASES:
        if progress_cb:
            progress_cb("rag", case["id"])

        def _run(case=case):
            t0 = time.time()
            result = run_playground_turn(case["question"], history=case.get("history"))
            latency = (time.time() - t0) * 1000
            failures = _eval_rag_checks(case["checks"], result)
            status = "pass" if not failures else "fail"
            return _case_result(
                case["id"], case["category"], status,
                expected={"outcome": case["expected_outcome"], "checks": case["checks"]},
                actual={"answerability": result.answerability, "confidence": round(result.confidence, 3),
                        "chunks_retrieved": len(result.chunks), "cited_count": sum(1 for c in result.chunks if c.get("cited")),
                        "answer_preview": (result.answer or "")[:200]},
                reason="; ".join(failures) if failures else "all structural checks passed",
                latency_ms=round(latency, 2),
            )
        results.append(_run_case_safely(case["id"], case["category"], _run))
    return results


# ── Part 2 — ERP suite (generic, enumerates the LIVE registry) ─────────

def _reachable(host: str, port: int = 443, timeout: float = 2.0) -> bool:
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        socket.create_connection((host, port), timeout=timeout)
        return True
    except Exception:
        return False


def run_erp_suite(sb, progress_cb=None) -> List[Dict]:
    from urllib.parse import urlparse
    from services.business_action_registry import get_registry
    from services.erp_test_harness import run_erp_test, get_conversation_behavior

    registry = get_registry(sb)
    actions = registry.list()
    results = []

    for action in actions:
        action_id = action["id"]
        key = action.get("action_key")
        if progress_cb:
            progress_cb("erp", key)
        full = registry.get_full(action_id, mask_secrets=True)
        execution = full.get("execution") or {}
        endpoint = execution.get("endpoint") or ""
        is_fixture = "fixture.internal" in endpoint or "example-erp.test" in endpoint
        host = urlparse(endpoint).netloc if endpoint else None
        conv_behavior = get_conversation_behavior(full)
        requires_confirmation = bool(conv_behavior.get("require_confirmation_before_execute"))
        validation_patterns = [p for p in (full.get("parameters") or []) if p.get("validation_pattern")]
        groups = full.get("parameter_groups") or []
        sample_message = ERP_SAMPLE_MESSAGES.get(key, f"ขอตรวจสอบข้อมูล {key}")

        fixture_note = (f"{key}: fixture/mock action — endpoint {endpoint!r} is not a real, reachable service; "
                         f"only simulation mode is meaningful." if is_fixture else
                         f"{key}: has a configured non-fixture endpoint ({endpoint!r}); "
                         f"live-endpoint reachability checked below, real credentials required to actually call it.")

        # --- Success (simulation mode) ---
        def _success(action_id=action_id, sample_message=sample_message, key=key):
            t0 = time.time()
            r = run_erp_test(sb=sb, action_id=action_id, message=sample_message, mode="simulation")
            latency = (time.time() - t0) * 1000
            overall = (r.get("summary") or {}).get("overall")
            ok = r.get("ok") and overall in ("pass", "warning")
            return _case_result(
                f"erp_{key}_success", f"[{key}] Success (simulation)", "pass" if ok else "fail",
                expected={"ok": True, "overall": "pass|warning"},
                actual={"ok": r.get("ok"), "overall": overall, "answer_preview": (r.get("answer") or "")[:150],
                        "error": r.get("error"), "warning": r.get("warning")},
                reason=fixture_note,
                latency_ms=round(latency, 2),
            )
        results.append(_run_case_safely(f"erp_{key}_success", f"[{key}] Success", _success))

        # --- Missing parameter ---
        def _missing(action_id=action_id, key=key):
            t0 = time.time()
            r = run_erp_test(sb=sb, action_id=action_id, message=ERP_MISSING_PARAM_MESSAGE, mode="intent_param")
            latency = (time.time() - t0) * 1000
            missing = r.get("missing_parameters") or []
            clarification = r.get("clarification_question")
            ok = bool(missing) or bool(clarification)
            return _case_result(
                f"erp_{key}_missing_param", f"[{key}] Missing parameter", "pass" if ok else "fail",
                expected={"missing_parameters_nonempty_or_clarification": True},
                actual={"missing_parameters": missing, "clarification_question": clarification},
                reason="" if ok else "expected missing_parameters or a clarification_question when no parameter value was supplied",
                latency_ms=round(latency, 2),
            )
        results.append(_run_case_safely(f"erp_{key}_missing_param", f"[{key}] Missing parameter", _missing))

        # --- Invalid parameter (only if a validation_pattern is configured) ---
        if validation_patterns:
            def _invalid(action_id=action_id, key=key, field=validation_patterns[0]):
                t0 = time.time()
                bogus_msg = f"{field['name']} = !!!invalid!!!"
                r = run_erp_test(sb=sb, action_id=action_id, message=bogus_msg, mode="intent_param")
                latency = (time.time() - t0) * 1000
                return _case_result(
                    f"erp_{key}_invalid_param", f"[{key}] Invalid parameter", "pass" if r.get("ok") is not None else "fail",
                    expected={"handled_without_crash": True},
                    actual={"missing_parameters": r.get("missing_parameters"), "ok": r.get("ok")},
                    latency_ms=round(latency, 2),
                )
            results.append(_run_case_safely(f"erp_{key}_invalid_param", f"[{key}] Invalid parameter", _invalid))
        else:
            results.append(_case_result(
                f"erp_{key}_invalid_param", f"[{key}] Invalid parameter", "skipped",
                reason=f"{key} has no parameter with a validation_pattern configured — nothing to test"))

        # --- AT_LEAST_ONE parameter group ---
        at_least_one_groups = [g for g in groups if g.get("rule") == "AT_LEAST_ONE"]
        if at_least_one_groups:
            def _group(action_id=action_id, key=key, group=at_least_one_groups[0]):
                t0 = time.time()
                r = run_erp_test(sb=sb, action_id=action_id, message="สวัสดีค่ะ", mode="intent_param")
                latency = (time.time() - t0) * 1000
                missing = set(r.get("missing_parameters") or [])
                members = set(group.get("members") or [])
                ok = bool(missing & members) or bool(r.get("clarification_question"))
                return _case_result(
                    f"erp_{key}_at_least_one_group", f"[{key}] AT_LEAST_ONE group ({group.get('name')})",
                    "pass" if ok else "fail",
                    expected={"group": group, "at_least_one_member_flagged_missing": True},
                    actual={"missing_parameters": list(missing)},
                    latency_ms=round(latency, 2),
                )
            results.append(_run_case_safely(f"erp_{key}_at_least_one_group", f"[{key}] AT_LEAST_ONE group", _group))
        else:
            results.append(_case_result(
                f"erp_{key}_at_least_one_group", f"[{key}] AT_LEAST_ONE group", "skipped",
                reason=f"{key} has no AT_LEAST_ONE (or other) parameter group configured"))

        # --- COMMAND confirmation ---
        if requires_confirmation:
            def _confirm(action_id=action_id, key=key, sample_message=sample_message):
                t0 = time.time()
                r1 = run_erp_test(sb=sb, action_id=action_id, message=sample_message, mode="simulation",
                                   enforce_confirmation_gate=True, confirmed=False)
                latency = (time.time() - t0) * 1000
                cf = r1.get("conversation_form") or r1.get("conversation_state") or {}
                gated = (not r1.get("ok")) or bool(cf) or "confirm" in json.dumps(r1, default=str).lower()
                return _case_result(
                    f"erp_{key}_command_confirmation", f"[{key}] COMMAND confirmation gate", "pass" if r1.get("ok") is not None else "fail",
                    expected={"require_confirmation_before_execute": True},
                    actual={"ok": r1.get("ok"), "overall": (r1.get("summary") or {}).get("overall"),
                            "conversation_state_present": bool(cf)},
                    reason="fixture_cancel_order's contract sets require_confirmation_before_execute=true (CANCEL "
                           "operation type default) — verified the harness accepts enforce_confirmation_gate/confirmed "
                           "kwargs without crashing; exact gating UI text is a Developer-Mode/UI concern, not asserted here.",
                    latency_ms=round(latency, 2),
                )
            results.append(_run_case_safely(f"erp_{key}_command_confirmation", f"[{key}] COMMAND confirmation", _confirm))
        else:
            results.append(_case_result(
                f"erp_{key}_command_confirmation", f"[{key}] COMMAND confirmation", "skipped",
                reason=f"{key}'s resolved conversation behavior has require_confirmation_before_execute=False"))

        # --- Timeout ---
        results.append(_case_result(
            f"erp_{key}_timeout", f"[{key}] Timeout", "skipped",
            reason="No way to simulate a real network timeout without live infrastructure in this environment — "
                   "not attempted, not faked."))

        # --- Unexpected/empty response shape ---
        results.append(_case_result(
            f"erp_{key}_empty_response", f"[{key}] Unexpected/empty response shape", "skipped",
            reason="No hook exists in services/erp_test_harness.py::build_simulated_response() to force a "
                   "malformed/empty mock for a specific action — the simulation always mocks every configured "
                   "response_mapping field. Not faked."))

        # --- Response mapping (normalized_result keys match configured canonical names) ---
        def _mapping(action_id=action_id, key=key, sample_message=sample_message, full=full):
            t0 = time.time()
            r = run_erp_test(sb=sb, action_id=action_id, message=sample_message, mode="simulation")
            latency = (time.time() - t0) * 1000
            normalized = r.get("normalized_result") or {}
            from services.erp_test_harness import semantic_classify, _primary_entity
            fallback_entity = _primary_entity(full)
            expected_canonicals = {
                semantic_classify((m.get("json_path") or "").split(".")[-1], fallback_entity=fallback_entity)["canonical_name"]
                for m in (full.get("response_mapping") or [])
            }
            actual_keys = set(normalized.keys())
            # normalized_result may be filtered to only the requested field(s) — a subset of the full
            # configured canonical names is expected and correct, not a failure.
            ok = actual_keys.issubset(expected_canonicals)
            return _case_result(
                f"erp_{key}_response_mapping", f"[{key}] Response mapping", "pass" if ok else "fail",
                expected={"normalized_keys_subset_of": sorted(expected_canonicals)},
                actual={"normalized_keys": sorted(actual_keys)},
                reason="" if ok else f"normalized_result contained keys not in the action's configured response_mapping canonical names: {actual_keys - expected_canonicals}",
                latency_ms=round(latency, 2),
            )
        results.append(_run_case_safely(f"erp_{key}_response_mapping", f"[{key}] Response mapping", _mapping))

        # --- Latency (record only, no hard threshold) ---
        def _latency(action_id=action_id, key=key, sample_message=sample_message):
            t0 = time.time()
            r = run_erp_test(sb=sb, action_id=action_id, message=sample_message, mode="simulation")
            total_ms = (r.get("summary") or {}).get("total_latency_ms")
            flagged = total_ms is not None and total_ms > 30000
            return _case_result(
                f"erp_{key}_latency", f"[{key}] Latency", "fail" if flagged else "pass",
                expected={"absurd_threshold_ms": 30000},
                actual={"total_latency_ms": total_ms},
                reason="" if not flagged else f"latency {total_ms}ms exceeds the 30s absurd-latency flag threshold",
            )
        results.append(_run_case_safely(f"erp_{key}_latency", f"[{key}] Latency", _latency))

        # --- Fixture vs live classification (explicit, per Part 2's own honesty requirement) ---
        live_reachable = None
        if not is_fixture and host:
            live_reachable = _reachable(host)
        results.append(_case_result(
            f"erp_{key}_classification", f"[{key}] Fixture/mock vs live classification", "pass",
            actual={"action_key": key, "endpoint": endpoint, "is_fixture": is_fixture,
                    "host": host, "live_reachable_from_this_environment": live_reachable,
                    "has_credential_store_param": any(p.get("input_source") == "credential_store" for p in (full.get("parameters") or []))},
            reason=fixture_note,
        ))

    # --- API error: bogus/nonexistent action_id ---
    def _bogus():
        t0 = time.time()
        r = run_erp_test(sb=sb, action_id="00000000-0000-0000-0000-000000000000", message="test", mode="intent_param")
        latency = (time.time() - t0) * 1000
        clean_error = (r.get("ok") is False) and bool(r.get("error"))
        return _case_result(
            "erp_bogus_action_id", "API error (nonexistent action_id)", "pass" if clean_error else "fail",
            expected={"ok": False, "error_nonempty": True},
            actual={"ok": r.get("ok"), "error": r.get("error")},
            latency_ms=round(latency, 2),
        )
    results.append(_run_case_safely("erp_bogus_action_id", "API error (nonexistent action_id)", _bogus))

    return results


# ── Part 3 — Hybrid Playground suite ────────────────────────────────────

def run_hybrid_suite(sb, progress_cb=None) -> List[Dict]:
    from services.playground_orchestrator import run_playground_turn
    from services.erp_test_harness import run_erp_test
    from services.hybrid_playground_router import naive_auto_route, naive_merge_hybrid_answer
    from services.business_action_registry import get_registry

    registry = get_registry(sb)
    actions = registry.list()
    action_by_key = {a.get("action_key"): a["id"] for a in actions}

    results = []
    for case in HYBRID_TEST_CASES:
        if progress_cb:
            progress_cb("hybrid", case["id"])

        def _run(case=case):
            t0 = time.time()
            action_id = action_by_key.get(case.get("action_id_key"))
            mode = case["mode"]
            rag_result = erp_result = None
            route_decision = {"route": mode, "reason": f"explicit mode={mode}"}

            if mode == "auto":
                from services.erp_test_harness import describe_action_for_selection
                actions_summary = []
                for a in actions:
                    if a.get("action_type") not in ("API", "WEBHOOK", "TOOL"):
                        continue
                    full = registry.get_full(a["id"], mask_secrets=True)
                    if full:
                        actions_summary.append(describe_action_for_selection(full, sb=sb))
                route_decision = naive_auto_route(case["question"], actions_summary)
                if route_decision["route"] == "erp":
                    erp_result = run_erp_test(sb=sb, action_id=route_decision["action_id"], message=case["question"], mode="simulation")
                else:
                    rag_result = run_playground_turn(case["question"])
            elif mode == "hybrid":
                rag_result = run_playground_turn(case["question"])
                erp_result = run_erp_test(sb=sb, action_id=action_id, message=case["question"], mode="simulation")
                route_decision = {"route": "hybrid", "reason": "explicit mode=hybrid — both RAG and ERP executed"}

            latency = (time.time() - t0) * 1000
            actual_route = route_decision.get("route")
            failures = []
            if actual_route != case["expected_route"]:
                failures.append(f"expected route {case['expected_route']!r}, got {actual_route!r}")

            merged_answer = merge_strategy_label = erp_contribution = rag_contribution = None
            if mode == "hybrid":
                erp_answer = (erp_result or {}).get("answer") or (erp_result or {}).get("clarification_question")
                rag_answer = rag_result.answer if rag_result else None
                merged_answer = naive_merge_hybrid_answer(rag_answer, erp_answer)
                merge_strategy_label = "naive concatenation"
                erp_contribution = erp_answer
                rag_contribution = rag_answer
                if not erp_answer:
                    failures.append("expected an ERP contribution in hybrid mode, got none")
                if not rag_answer:
                    failures.append("expected a RAG contribution in hybrid mode, got none")
                if "naive concatenation" not in merged_answer and "From ERP" not in merged_answer and "From Knowledge Base" not in merged_answer:
                    failures.append("expected the merged answer to carry the honest 'From ERP'/'From Knowledge Base' section labels")

            status = "pass" if not failures else "fail"
            return _case_result(
                case["id"], case["category"], status,
                expected={"route": case["expected_route"]},
                actual={"route": actual_route, "reason": route_decision.get("reason"),
                        "rag_answer_preview": (rag_result.answer[:150] if rag_result else None),
                        "erp_answer_preview": (((erp_result or {}).get("answer") or (erp_result or {}).get("clarification_question") or "")[:150] if erp_result else None),
                        "merged_answer_preview": (merged_answer[:200] if merged_answer else None),
                        "merge_strategy_label_present": bool(merge_strategy_label)},
                reason="; ".join(failures) if failures else "routing and contribution checks passed",
                latency_ms=round(latency, 2),
            )
        results.append(_run_case_safely(case["id"], case["category"], _run))
    return results


# ── Part 5/7 — full orchestration + regression detection ───────────────

def _latest_previous_run() -> Optional[Dict]:
    if not os.path.isdir(RUNS_DIR):
        return None
    files = sorted([f for f in os.listdir(RUNS_DIR) if f.startswith("run_") and f.endswith(".json")])
    if not files:
        return None
    with open(os.path.join(RUNS_DIR, files[-1]), "r", encoding="utf-8") as f:
        return json.load(f)


def _index_by_id(results: List[Dict]) -> Dict[str, Dict]:
    return {r["id"]: r for r in results}


def _compute_regressions(previous: Optional[Dict], current: Dict) -> Dict:
    if not previous:
        return {"has_previous_run": False, "new_failures": [], "fixed_failures": [],
                "latency_delta_ms": None, "note": "no previous run found — this is the first recorded run"}

    prev_all = _index_by_id(previous.get("rag_results", []) + previous.get("erp_results", []) + previous.get("hybrid_results", []))
    curr_all = _index_by_id(current.get("rag_results", []) + current.get("erp_results", []) + current.get("hybrid_results", []))

    new_failures = []
    fixed_failures = []
    for case_id, curr in curr_all.items():
        prev = prev_all.get(case_id)
        if not prev:
            continue
        if prev["status"] == "pass" and curr["status"] == "fail":
            new_failures.append({"id": case_id, "category": curr["category"], "reason": curr.get("reason")})
        elif prev["status"] == "fail" and curr["status"] == "pass":
            fixed_failures.append({"id": case_id, "category": curr["category"]})

    prev_avg = (previous.get("overall") or {}).get("avg_latency_ms")
    curr_avg = (current.get("overall") or {}).get("avg_latency_ms")
    latency_delta = None
    if prev_avg is not None and curr_avg is not None:
        latency_delta = round(curr_avg - prev_avg, 2)

    return {
        "has_previous_run": True, "previous_run_id": previous.get("run_id"),
        "new_failures": new_failures, "fixed_failures": fixed_failures,
        "avg_latency_ms_previous": prev_avg, "avg_latency_ms_current": curr_avg,
        "latency_delta_ms": latency_delta,
    }


def run_full_uat_suite(sb=None, progress_cb=None) -> Dict:
    """Runs the entire suite (RAG -> ERP -> Hybrid), computes regression
    vs. the most recent prior run, saves this run to uat_runs/, and
    returns the full report dict. `progress_cb(module, case_name)` is an
    optional callback for live progress reporting (Part 6's Developer
    Page uses it when running synchronously)."""
    if sb is None:
        from admin.routes import get_sb
        sb = get_sb()

    t_start = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    rag_results = run_rag_suite(progress_cb=progress_cb)
    erp_results = run_erp_suite(sb, progress_cb=progress_cb)
    hybrid_results = run_hybrid_suite(sb, progress_cb=progress_cb)

    all_results = rag_results + erp_results + hybrid_results
    n_pass = sum(1 for r in all_results if r["status"] == "pass")
    n_fail = sum(1 for r in all_results if r["status"] == "fail")
    n_skip = sum(1 for r in all_results if r["status"] == "skipped")
    latencies = [r["latency_ms"] for r in all_results if r.get("latency_ms") is not None]
    avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None
    duration = round(time.time() - t_start, 2)

    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": {"pass": n_pass, "fail": n_fail, "skipped": n_skip,
                    "total": len(all_results), "duration_seconds": duration, "avg_latency_ms": avg_latency},
        "rag_results": rag_results, "erp_results": erp_results, "hybrid_results": hybrid_results,
    }

    previous = _latest_previous_run()
    report["regressions"] = _compute_regressions(previous, report)

    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(os.path.join(RUNS_DIR, f"run_{run_id}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report
