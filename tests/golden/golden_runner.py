"""Golden Test Harness -- Runner (Phase 2/3/5/7/8/9).

Loads tests/golden/golden_cases.json (Test Definition), drives each case's
turns through the REAL, unmodified /admin/api/hybrid-playground/ask
Auto-mode endpoint (the same production Decision Engine path the LINE
webhook uses -- see admin/routes.py's own docstring), evaluates the
generic assertion engine (golden_assertions.py) against what actually
happened, and persists one immutable row per (run_id, golden_id) into
golden_test_results (migration 041) -- never into golden_test_registry
(migration 040, which this script does not import, query, or reference
anywhere, by design: it must be structurally impossible for a run to
overwrite the audited historical registry).

Hard guarantees (Golden Runner Integrity, Phase 8):
  - Expected values are read ONLY from golden_cases.json and are never
    mutated, derived from actual results, or auto-corrected.
  - Every case gets exactly one attempt -- no retry loop of any kind, so
    there is no "hide the first failed attempt" behavior possible.
  - An exception during a case is recorded as automatic_result=ERROR and
    the run continues; ERROR is never silently coerced into PASS or
    swallowed out of the final counts.
  - Assertion evaluation (golden_assertions.py) has zero `if golden_id ==`
    branches -- nothing here can special-case one case to force a PASS.
  - No ERP/RAG call is mocked or stubbed -- decide() runs for real.
  - Assertions are exact-substring / exact-equality, never fuzzy.

Notification safety (Phase 7): before any HTTP call is made, this script
statically re-verifies that admin/routes.py contains no call to
services/human_handoff_service.py::send_handoff_notification (the ONLY
function in this codebase capable of sending a real SendLineNotiCS/NOTIFY
Business Action). If that call is found anywhere in admin/routes.py, the
run ABORTS before sending a single request -- see assert_notification_safety().
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.golden.golden_assertions import (  # noqa: E402
    CaseResult, TurnResult, evaluate_assertions,
)

_PLACEHOLDER_RE = re.compile(r"\{\{captured\.([A-Za-z0-9_]+)\}\}")
_CAPTURE_KEYS = ("OrderCode", "ShipmentCode", "Tracking", "CustCode")


# ── Phase 7: static notification-safety guard ────────────────────────

class NotificationSafetyError(RuntimeError):
    pass


def assert_notification_safety(repo_root: Path) -> None:
    """ABORTS the run (raises) unless it can positively prove
    admin/routes.py -- the only module this runner ever calls into -- has
    no call to the real send_handoff_notification(). This is a static
    source check, not a runtime mock: the guarantee is that the code path
    this runner can possibly reach is structurally incapable of sending a
    real notification, not that something intercepted it at the last
    moment."""
    routes_path = repo_root / "admin" / "routes.py"
    if not routes_path.exists():
        raise NotificationSafetyError(
            f"ABORT: cannot find {routes_path} to verify notification safety -- refusing to run.")
    text = routes_path.read_text(encoding="utf-8")
    if "send_handoff_notification(" in text:
        raise NotificationSafetyError(
            "ABORT: admin/routes.py now contains a call to send_handoff_notification(). "
            "This means the Auto-mode Playground path this runner drives could send a REAL "
            "SendLineNotiCS notification. Refusing to run any Golden case until this is resolved. "
            "(services/human_handoff_service.py::send_handoff_notification is the only function in "
            "this codebase capable of a real send -- it must never be reachable from this endpoint.)")
    print("[safety] OK -- admin/routes.py contains no call to send_handoff_notification(). Proceeding.")


# ── Small helpers ─────────────────────────────────────────────────────

def _git_sha(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root),
                              capture_output=True, text=True, check=True, timeout=10)
        return out.stdout.strip()
    except Exception as e:
        print(f"[warn] could not determine local git SHA: {e}")
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _substitute_captured(text: str, captured: Dict[str, str]) -> str:
    def _sub(m: "re.Match") -> str:
        key = m.group(1)
        if key not in captured:
            raise RuntimeError(
                f"Turn text references {{{{captured.{key}}}}} but no prior turn captured a value for {key!r} "
                f"(captured so far: {captured!r}). This is a dataset/runner bug, never silently worked around "
                f"with a fabricated value.")
        return captured[key]
    return _PLACEHOLDER_RE.sub(_sub, text)


def _update_captured(captured: Dict[str, str], collected_parameters: Dict[str, Any]) -> None:
    for key in _CAPTURE_KEYS:
        val = collected_parameters.get(key)
        if val:
            captured[key] = str(val)


# ── HTTP client over the real Admin Playground endpoint ──────────────

class PlaygroundClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        r = self.session.post(f"{self.base_url}/admin/login",
                               data={"username": username, "password": password}, timeout=30)
        if "session_token" not in self.session.cookies.get_dict():
            raise RuntimeError(
                f"Login to {self.base_url}/admin/login did not set a session_token cookie "
                f"(status={r.status_code}). Refusing to proceed without an authenticated session.")

    def ask(self, question: str, *, journey_label: str, playground_user_id: str) -> Dict[str, Any]:
        r = self.session.post(
            f"{self.base_url}/admin/api/hybrid-playground/ask",
            json={"question": question, "mode": "auto",
                  "journey_label": journey_label, "playground_user_id": playground_user_id},
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"/admin/api/hybrid-playground/ask returned ok=false: {body}")
        return body

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        r = self.session.get(f"{self.base_url}/admin/playground/sessions/{session_id}", timeout=30)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"could not fetch session {session_id}: {body}")
        return (body.get("session") or {}).get("messages") or []


# ── Running one case ──────────────────────────────────────────────────

def run_case(client: PlaygroundClient, case: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    golden_id = case["golden_id"]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{run_id}-{golden_id}").strip("-").upper()
    playground_user_id = f"playground:GOLDENRUN-{slug}"
    journey_label = f"{golden_id} (run {run_id})"

    captured: Dict[str, str] = {}
    turns: List[TurnResult] = []
    resolved_turns: List[str] = []
    session_id: Optional[str] = None

    for i, raw_turn in enumerate(case["turns"]):
        question = _substitute_captured(raw_turn, captured)
        resolved_turns.append(question)
        body = client.ask(question, journey_label=journey_label, playground_user_id=playground_user_id)
        trace = body.get("production_trace") or {}
        session_id = trace.get("session_id") or body.get("session_id") or session_id
        collected = trace.get("collected_parameters") or {}
        _update_captured(captured, collected)

        turns.append(TurnResult(
            turn_index=i, question=question,
            actual_route=trace.get("routing_type"),
            actual_action=trace.get("selected_business_action"),
            actual_answer="",  # filled below from the persisted session transcript
            collected_parameters=collected,
            customer_stage=trace.get("customer_stage"),
            handoff_notification=trace.get("handoff_notification"),
            erp_http_status=trace.get("erp_http_status"),
            session_id=session_id,
        ))

    # actual_answer is read back from the PERSISTED session transcript
    # (the same ai_session_messages the Independent Audit itself used as
    # its evidence source), never from the synchronous HTTP response body
    # alone -- this is what a human re-opening the session later will see.
    if session_id:
        messages = client.get_session_messages(session_id)
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        for t, m in zip(turns, assistant_msgs):
            t.actual_answer = m.get("content") or ""

    case_result = CaseResult(golden_id=golden_id, turns=turns)
    outcomes = evaluate_assertions(case["assertions"], case_result)
    failed = [o for o in outcomes if not o.passed]

    if failed:
        automatic_result = "FAIL"
        reason = "; ".join(f"{o.type}[turn {o.turn_index}]: {o.detail}" for o in failed)
    elif case.get("expected_route") is None:
        # Genuinely ambiguous by design (per the audit's own convention) --
        # every safety assertion passed, but a human must still judge this
        # one; never auto-PASS an intentionally-ambiguous case.
        automatic_result = "PENDING"
        reason = "No single deterministic expected_route (ambiguous by design) -- all safety assertions passed; human judgment required."
    else:
        automatic_result = "PASS"
        reason = "All assertions passed."

    return {
        "run_id": run_id, "golden_id": golden_id, "category": case["category"],
        "session_id": session_id, "user_journey": resolved_turns,
        "expected_route": case.get("expected_route"), "expected_action": case.get("expected_action"),
        "expected_behavior": case.get("expected_behavior"),
        "actual_route": turns[-1].actual_route if turns else None,
        "actual_action": turns[-1].actual_action if turns else None,
        "actual_answer": turns[-1].actual_answer if turns else None,
        "automatic_result": automatic_result, "automatic_result_reason": reason,
        "assertion_results": [asdict(o) for o in outcomes],
    }


# ── Persistence (Phase 3/9) -- INSERT only, never UPDATE/UPSERT ──────

def _get_sb():
    from config import SUPABASE_URL, SUPABASE_KEY
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def insert_run_start(sb, run_id: str, dataset_version: str, git_sha: Optional[str],
                      server_git_sha: Optional[str], total_cases: int, notes: Optional[str]) -> None:
    sb.table("golden_test_runs").insert({
        "run_id": run_id, "dataset_version": dataset_version,
        "dataset_path": "tests/golden/golden_cases.json",
        "git_sha": git_sha, "server_git_sha": server_git_sha,
        "total_cases": total_cases, "started_at": _now_iso(), "notes": notes,
    }).execute()


def insert_case_result(sb, result: Dict[str, Any]) -> None:
    # INSERT only -- UNIQUE(run_id, golden_id) makes accidental re-insertion
    # for the same run a hard DB error, never a silent overwrite.
    sb.table("golden_test_results").insert(result).execute()


def finalize_run(sb, run_id: str, counts: Dict[str, int]) -> None:
    sb.table("golden_test_runs").update({
        "completed_at": _now_iso(),
        "pass_count": counts["PASS"], "fail_count": counts["FAIL"],
        "pending_count": counts["PENDING"], "error_count": counts["ERROR"],
        "skipped_count": counts["SKIPPED"],
    }).eq("run_id", run_id).execute()


# ── Entry point ────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Golden Test Harness runner")
    ap.add_argument("--base-url", required=True, help="Base URL of the Admin app, e.g. https://host or http://localhost:8010")
    ap.add_argument("--username", default=None, help="Admin username (defaults to config.ADMIN_USERNAME)")
    ap.add_argument("--password", default=None, help="Admin password (defaults to config.ADMIN_PASSWORD)")
    ap.add_argument("--dataset", default=str(REPO_ROOT / "tests" / "golden" / "golden_cases.json"))
    ap.add_argument("--server-git-sha", default=None, help="Deployed server HEAD SHA, if known (operator supplies -- this script cannot SSH itself)")
    ap.add_argument("--run-notes", default=None)
    ap.add_argument("--only", default=None, help="Comma-separated golden_id list to run (others become SKIPPED)")
    ap.add_argument("--output", default=None, help="Optional path to also write the run summary as JSON")
    args = ap.parse_args()

    assert_notification_safety(REPO_ROOT)

    from config import ADMIN_USERNAME, ADMIN_PASSWORD
    username = args.username or ADMIN_USERNAME
    password = args.password or ADMIN_PASSWORD

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    dataset_version = dataset["dataset_version"]
    cases = dataset["cases"]
    only = set(x.strip() for x in args.only.split(",")) if args.only else None

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    git_sha = _git_sha(REPO_ROOT)
    print(f"[run] run_id={run_id} dataset_version={dataset_version} git_sha={git_sha} "
          f"server_git_sha={args.server_git_sha} total_cases={len(cases)}")

    client = PlaygroundClient(args.base_url, username, password)
    sb = _get_sb()
    insert_run_start(sb, run_id, dataset_version, git_sha, args.server_git_sha, len(cases), args.run_notes)

    counts = {"PASS": 0, "FAIL": 0, "PENDING": 0, "ERROR": 0, "SKIPPED": 0}
    results: List[Dict[str, Any]] = []

    for case in cases:
        golden_id = case["golden_id"]
        if only is not None and golden_id not in only:
            result = {
                "run_id": run_id, "golden_id": golden_id, "category": case["category"],
                "session_id": None, "user_journey": case["turns"],
                "expected_route": case.get("expected_route"), "expected_action": case.get("expected_action"),
                "expected_behavior": case.get("expected_behavior"),
                "actual_route": None, "actual_action": None, "actual_answer": None,
                "automatic_result": "SKIPPED", "automatic_result_reason": "excluded via --only filter",
                "assertion_results": [],
            }
        else:
            print(f"[run] {golden_id} ...", end=" ", flush=True)
            try:
                result = run_case(client, case, run_id)
            except Exception as e:
                # ERROR is recorded and the run continues -- never coerced
                # into PASS/FAIL, never swallowed, never retried.
                result = {
                    "run_id": run_id, "golden_id": golden_id, "category": case["category"],
                    "session_id": None, "user_journey": case["turns"],
                    "expected_route": case.get("expected_route"), "expected_action": case.get("expected_action"),
                    "expected_behavior": case.get("expected_behavior"),
                    "actual_route": None, "actual_action": None, "actual_answer": None,
                    "automatic_result": "ERROR", "automatic_result_reason": f"{type(e).__name__}: {e}",
                    "assertion_results": [],
                }
            print(result["automatic_result"])
        counts[result["automatic_result"]] += 1
        results.append(result)
        insert_case_result(sb, result)

    finalize_run(sb, run_id, counts)

    total = sum(counts.values())
    assert total == len(cases), f"internal consistency error: counted {total} results for {len(cases)} cases"

    summary = {
        "run_id": run_id, "dataset_version": dataset_version, "git_sha": git_sha,
        "server_git_sha": args.server_git_sha, "total": total, **counts,
        "failed_ids": [r["golden_id"] for r in results if r["automatic_result"] == "FAIL"],
        "pending_ids": [r["golden_id"] for r in results if r["automatic_result"] == "PENDING"],
        "error_ids": [r["golden_id"] for r in results if r["automatic_result"] == "ERROR"],
        "results": results,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[run] wrote summary to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
