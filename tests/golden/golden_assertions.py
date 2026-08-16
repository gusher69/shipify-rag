"""Golden Test Harness -- Assertion Engine (Phase 4).

Pure, stateless checks over a CaseResult (the recorded turn-by-turn outcome
of running one golden_cases.json case through the real
/admin/api/hybrid-playground/ask Auto-mode endpoint). This module contains
NO network calls, NO database access, and NO knowledge of any specific
golden_id -- every assertion is driven only by the `type`/parameters given
in golden_cases.json's `assertions` list, so nothing here can special-case
a case to force it to pass (Golden Runner Integrity, Phase 8).

A PASS never comes from the final answer text alone: route_equals/
action_equals compare the ACTUAL selected route/business-action the
Decision Engine used internally, so a plausible-looking answer produced via
the wrong backend action still FAILs (this is the exact defect class the
Independent Audit found in GOLDEN-024/049).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Result shapes the runner builds from real HTTP responses ────────────

@dataclass
class TurnResult:
    turn_index: int
    question: str
    actual_route: Optional[str]              # normalized upper-case: RAG | API | WEBHOOK | HYBRID | WORKFLOW | HUMAN_HANDOFF
    actual_action: Optional[str]
    actual_answer: str
    collected_parameters: Dict[str, Any] = field(default_factory=dict)
    customer_stage: Optional[str] = None
    handoff_notification: Optional[Dict[str, Any]] = None
    erp_http_status: Optional[int] = None
    session_id: Optional[str] = None


@dataclass
class CaseResult:
    golden_id: str
    turns: List[TurnResult] = field(default_factory=list)

    @property
    def last(self) -> TurnResult:
        return self.turns[-1]

    def turn(self, turn_index: Optional[int]) -> TurnResult:
        return self.last if turn_index is None else self.turns[turn_index]


@dataclass
class AssertionOutcome:
    type: str
    passed: bool
    detail: str
    turn_index: Optional[int] = None


# ── Internal helpers ──────────────────────────────────────────────────

_RAW_JSON_MARKERS = ["[{", '{"', "{'", "None", "null", "Traceback", "<class '"]
_INTERNAL_SOURCE_MARKERS = ["Source:", ".xlsx", "หน้า ", "section", "citation"]
_INTERNAL_ARCH_MARKERS = [
    "📦", "📘", "ข้อมูลเฉพาะลูกค้า (ERP)", "ข้อมูลนโยบาย", "Knowledge Base:",
    "Decision Engine", "จาก ERP", "จาก RAG", "[ERP]", "[RAG]",
]


def _find_any(text: str, markers: List[str]) -> Optional[str]:
    for m in markers:
        if m in (text or ""):
            return m
    return None


# ── One evaluator per assertion `type` ───────────────────────────────

def _route_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = t.actual_route == a["value"]
    return AssertionOutcome("route_equals", ok,
                             f"expected route={a['value']!r}, actual route={t.actual_route!r}", t.turn_index)


def _route_not_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = t.actual_route != a["value"]
    return AssertionOutcome("route_not_equals", ok,
                             f"forbidden route={a['value']!r}, actual route={t.actual_route!r}", t.turn_index)


def _action_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    actual = (t.actual_action or "").lower()
    expected = (a["value"] or "").lower()
    ok = actual == expected
    return AssertionOutcome("action_equals", ok,
                             f"expected action={a['value']!r}, actual action={t.actual_action!r}", t.turn_index)


def _action_not_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    actual = (t.actual_action or "").lower()
    forbidden = (a["value"] or "").lower()
    ok = actual != forbidden
    return AssertionOutcome("action_not_equals", ok,
                             f"forbidden action={a['value']!r}, actual action={t.actual_action!r}", t.turn_index)


def _answer_contains(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = a["value"] in (t.actual_answer or "")
    return AssertionOutcome("answer_contains", ok,
                             f"expected substring={a['value']!r} in actual_answer", t.turn_index)


def _answer_not_contains(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = a["value"] not in (t.actual_answer or "")
    return AssertionOutcome("answer_not_contains", ok,
                             f"forbidden substring={a['value']!r} in actual_answer", t.turn_index)


def _no_raw_json(a: Dict, cr: CaseResult) -> AssertionOutcome:
    targets = [cr.turn(a["turn_index"])] if a.get("turn_index") is not None else cr.turns
    for t in targets:
        hit = _find_any(t.actual_answer, _RAW_JSON_MARKERS)
        if hit:
            return AssertionOutcome("no_raw_json", False,
                                     f"turn {t.turn_index}: found raw-serialization marker {hit!r} in actual_answer",
                                     t.turn_index)
    return AssertionOutcome("no_raw_json", True, "no raw-serialization marker found in any turn's actual_answer")


def _no_internal_source(a: Dict, cr: CaseResult) -> AssertionOutcome:
    targets = [cr.turn(a["turn_index"])] if a.get("turn_index") is not None else cr.turns
    for t in targets:
        hit = _find_any(t.actual_answer, _INTERNAL_SOURCE_MARKERS)
        if hit:
            return AssertionOutcome("no_internal_source", False,
                                     f"turn {t.turn_index}: found internal-citation marker {hit!r} in actual_answer",
                                     t.turn_index)
    return AssertionOutcome("no_internal_source", True, "no internal-citation marker found in any turn's actual_answer")


def _no_internal_architecture_label(a: Dict, cr: CaseResult) -> AssertionOutcome:
    targets = [cr.turn(a["turn_index"])] if a.get("turn_index") is not None else cr.turns
    for t in targets:
        hit = _find_any(t.actual_answer, _INTERNAL_ARCH_MARKERS)
        if hit:
            return AssertionOutcome("no_internal_architecture_label", False,
                                     f"turn {t.turn_index}: found architecture label {hit!r} in actual_answer",
                                     t.turn_index)
    return AssertionOutcome("no_internal_architecture_label", True,
                             "no architecture label found in any turn's actual_answer")


def _requires_clarification(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = t.actual_route in ("WORKFLOW",)
    return AssertionOutcome("requires_clarification", ok,
                             f"expected a clarification route (WORKFLOW), actual route={t.actual_route!r}", t.turn_index)


def _context_reused(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ident = a["identifier"]
    values = [str(v) for v in (t.collected_parameters or {}).values()]
    ok = any(ident.lower() in v.lower() for v in values)
    return AssertionOutcome("context_reused", ok,
                             f"expected identifier {ident!r} present in collected_parameters={t.collected_parameters!r}",
                             t.turn_index)


def _no_fabricated_identifier(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    # A fabricated identifier would have let a real ERP call succeed with a
    # made-up value -- if no real HTTP 200 execution happened this turn,
    # nothing could have been fabricated-and-executed.
    ok = t.erp_http_status != 200
    return AssertionOutcome("no_fabricated_identifier", ok,
                             f"expected no successful ERP execution this turn (would imply a fabricated identifier was used), "
                             f"actual erp_http_status={t.erp_http_status!r}", t.turn_index)


def _customer_stage_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    ok = (t.customer_stage or "").lower() == (a["value"] or "").lower()
    return AssertionOutcome("customer_stage_equals", ok,
                             f"expected customer_stage={a['value']!r}, actual={t.customer_stage!r}", t.turn_index)


def _notification_never_real(a: Dict, cr: CaseResult) -> AssertionOutcome:
    for t in cr.turns:
        n = t.handoff_notification
        if not n:
            continue
        note = (n.get("note") or "")
        if n.get("simulated_sent") is True and "SIMULATED" not in note.upper():
            return AssertionOutcome("notification_never_real", False,
                                     f"turn {t.turn_index}: handoff_notification.simulated_sent=True but note does not "
                                     f"confirm SIMULATED ONLY: {n!r}", t.turn_index)
    return AssertionOutcome("notification_never_real", True,
                             "every handoff_notification seen (if any) was explicitly marked SIMULATED ONLY")


def _notification_simulated_sent_equals(a: Dict, cr: CaseResult) -> AssertionOutcome:
    t = cr.turn(a.get("turn_index"))
    n = t.handoff_notification or {}
    ok = n.get("simulated_sent") == a["value"]
    return AssertionOutcome("notification_simulated_sent_equals", ok,
                             f"turn {t.turn_index}: expected handoff_notification.simulated_sent={a['value']!r}, "
                             f"actual handoff_notification={n!r}", t.turn_index)


_EVALUATORS = {
    "route_equals": _route_equals,
    "route_not_equals": _route_not_equals,
    "action_equals": _action_equals,
    "action_not_equals": _action_not_equals,
    "answer_contains": _answer_contains,
    "answer_not_contains": _answer_not_contains,
    "no_raw_json": _no_raw_json,
    "no_internal_source": _no_internal_source,
    "no_internal_architecture_label": _no_internal_architecture_label,
    "requires_clarification": _requires_clarification,
    "context_reused": _context_reused,
    "no_fabricated_identifier": _no_fabricated_identifier,
    "customer_stage_equals": _customer_stage_equals,
    "notification_never_real": _notification_never_real,
    "notification_simulated_sent_equals": _notification_simulated_sent_equals,
}


def evaluate_assertions(assertions: List[Dict], case_result: CaseResult) -> List[AssertionOutcome]:
    """Runs every assertion in `assertions` against `case_result` and
    returns one AssertionOutcome per assertion, in order. An unknown
    assertion `type` is a hard error (never silently skipped -- a typo in
    golden_cases.json must not silently pass)."""
    outcomes: List[AssertionOutcome] = []
    for a in assertions:
        fn = _EVALUATORS.get(a["type"])
        if fn is None:
            raise ValueError(f"Unknown assertion type: {a['type']!r} (known: {sorted(_EVALUATORS)})")
        outcomes.append(fn(a, case_result))
    return outcomes
