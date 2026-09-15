# -*- coding: utf-8 -*-
"""Running the graph safely, and comparing it with the current engine.

Four responsibilities:

  * `run_agent`         — one traced graph run, returning an AgentDecision.
  * `shadow_run`        — the same, wrapped so that NOTHING it does can
                          affect the customer's turn: it never raises, and
                          a failure degrades to "no shadow decision".
  * `authoritative_run` — the graph as PRIMARY responder (owner_test /
                          production), with the current DecisionEngine as
                          the automatic TECHNICAL / SAFETY fallback. The
                          engine takes the turn only when the graph raised,
                          timed out, produced an invalid state, or its own
                          safety gate flagged the reply — never because the
                          two would merely have understood the turn
                          differently.
  * `compare`           — classify the graph's decision against the
                          current engine's for the shadow report.

Authority is decided in ONE place, `graph_is_authoritative()`, from the
explicit `config.LANGGRAPH_MODE`. There is no path by which the graph
becomes customer-facing without that setting saying so.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import config
from services.agent.graph import get_graph
from services.agent.state import AgentDecision, new_state
from services.observability import langfuse_client as lf

COMPARISON_CLASSES = ("MATCH", "LANGGRAPH_IMPROVEMENT", "CURRENT_CORRECT",
                      "LANGGRAPH_WRONG", "AMBIGUOUS")
# the dimensions on which a disagreement is a SAFETY disagreement, and
# where LANGGRAPH_WRONG must therefore be zero before any cutover.
SAFETY_DIMENSIONS = ("auth", "private_access", "action_truth",
                     "customer_isolation", "policy_truth")
# a graph error with one of these prefixes is a SAFETY verdict from the
# graph's own gate; every other error is a TECHNICAL failure. Both hand
# the turn to the engine in authoritative mode.
SAFETY_FLAG_PREFIXES = ("AUTH_VIOLATION", "PRIVATE_LEAK",
                        "FALSE_ACTION_COMPLETION", "KNOWN_SLOT_RE_ASK")

# who the graph answers in each mode. `production` is every source —
# REAL_LINE, OWNER_TEST, ADMIN_AUTO and anything else the webhook labels.
_OWNER_SOURCES = frozenset({"OWNER_TEST"})


def graph_is_authoritative(sample_source: str = "") -> bool:
    """Is the graph allowed to answer THIS customer?"""
    mode = getattr(config, "LANGGRAPH_MODE", "shadow")
    if mode == "production":
        return True
    if mode == "owner_test":
        return (sample_source or "").upper() in _OWNER_SOURCES
    return False


def graph_should_run(sample_source: str = "") -> bool:
    mode = getattr(config, "LANGGRAPH_MODE", "shadow")
    if mode == "off":
        return False
    if mode == "shadow":
        return bool(getattr(config, "LANGGRAPH_SHADOW_MODE", True))
    return True


def run_agent(message: str, history=None, context=None) -> AgentDecision:
    """One graph run, traced. Raises only if the graph itself raises —
    callers on the live path must use `shadow_run` / `authoritative_run`."""
    ctx = dict(context or {})
    started = time.time()
    state = new_state(message, history=history, context=ctx)
    authoritative = graph_is_authoritative(ctx.get("sample_source") or "")
    with lf.trace("shipify.agent.turn",
                  session_id=ctx.get("session_id") or ctx.get("external_user_id"),
                  metadata={"channel": ctx.get("channel"),
                            "sample_source": ctx.get("sample_source"),
                            "langgraph_mode": getattr(config, "LANGGRAPH_MODE", "shadow"),
                            "graph_version": getattr(config, "LANGGRAPH_VERSION", "1"),
                            "authoritative": authoritative},
                  input_payload={"message": message}) as span:
        out = get_graph().invoke(state)
        decision = AgentDecision.from_state(out)
        decision.engine_result = out.get("_engine_result")
        # Only the SHAPE of the turn is traced — entity NAMES, not values;
        # the masking layer redacts anything that slips through. The
        # normalisation block is the one place text appears, identifier-
        # masked by the normaliser itself (task §14: "what did this word
        # become before it reached intent?").
        norm = dict(out.get("normalization_trace") or {})
        span.update(output={
            "normalization": norm,
            "primary_intent": decision.primary_intent,
            "conversation_act": decision.conversation_act,
            "active_journey": decision.active_journey,
            "entity_names": sorted((decision.entities or {}).keys()),
            "known_slot_names": sorted(k for k, v in (decision.known_slots or {}).items() if v),
            "requested_slot": decision.requested_slot,
            "planned_action": decision.planned_action,
            "selected_tool": decision.selected_tool,
            "tool_class": decision.tool_class,
            "routing_type": decision.routing_type,
            "auth_required": decision.auth_required,
            "auth_state": decision.auth_state,
            "handoff_required": decision.handoff_required,
            "confidence": decision.confidence,
            "grounding": (out.get("response_plan") or {}).get("grounding_status"),
            "safety_flags": (out.get("response_plan") or {}).get("safety_flags") or [],
            "node_path": decision.node_path,
            "errors": decision.errors,
            "fallback": False,
            "latency_ms": round((time.time() - started) * 1000, 2),
        })
    return decision


def shadow_run(message: str, history=None, context=None) -> Optional[AgentDecision]:
    """Run the graph beside the live turn. Never raises, never blocks the
    customer's answer, and returns None when it could not complete."""
    ctx = dict(context or {})
    if not graph_should_run(ctx.get("sample_source") or ""):
        return None
    try:
        return run_agent(message, history=history, context=ctx)
    except Exception as exc:                      # pragma: no cover - safety net
        print(f"[agent] shadow run failed, customer turn unaffected: {exc!r}")
        return None


# ── authoritative mode ───────────────────────────────────────────────
@dataclass
class AuthoritativeOutcome:
    """What the webhook acts on when the graph is primary."""
    used: str                                   # "langgraph" | "engine_fallback"
    decision: Optional[AgentDecision] = None
    engine_result: Optional[Dict[str, Any]] = None   # the executed engine result
    fallback_reason: Optional[str] = None
    fallback_kind: Optional[str] = None         # "technical" | "safety" | None
    latency_ms: float = 0.0


def validate_decision(decision: Optional[AgentDecision]) -> Tuple[Optional[str], Optional[str]]:
    """(fallback_kind, reason) if this decision may NOT answer the
    customer, else (None, None). Technical: no result, empty reply,
    tool/graph error. Safety: the graph's own gate flagged the reply."""
    if decision is None:
        return "technical", "graph returned no decision"
    safety = [e for e in (decision.errors or [])
              if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
    if safety:
        return "safety", "; ".join(safety)
    if decision.errors:
        return "technical", "; ".join(decision.errors)
    if not (decision.final_response or "").strip():
        return "technical", "empty final response"
    if not isinstance(decision.engine_result, dict) or not decision.engine_result:
        return "technical", "no executed engine result attached"
    return None, None


def authoritative_run(message: str, history=None, context=None, *,
                      engine_fallback, timeout: Optional[float] = None) -> AuthoritativeOutcome:
    """The graph answers; the engine is the automatic fallback.

    `engine_fallback` is a zero-argument callable producing the current
    engine's full result for this turn. It is invoked ONLY when the graph
    could not validly answer — so the turn is never executed twice on a
    healthy path (the graph's execute step already IS the engine).

    Timeout: the graph runs on a worker thread and the caller waits at
    most `timeout` seconds. A run that overruns is abandoned for this
    turn; its worker finishes in the background with no effect on the
    reply. Python cannot cancel it, which is why the ceiling is generous
    and documented as a genuine-hang guard, not a latency budget.
    """
    ctx = dict(context or {})
    started = time.time()
    limit = float(timeout if timeout is not None
                  else getattr(config, "LANGGRAPH_TIMEOUT_SECONDS", 60))
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["decision"] = run_agent(message, history=history, context=ctx)
        except BaseException as exc:            # noqa: BLE001 - captured for fallback
            box["exc"] = exc

    t = threading.Thread(target=_worker, name="langgraph-authoritative", daemon=True)
    t.start()
    t.join(limit)
    kind: Optional[str]
    reason: Optional[str]
    decision: Optional[AgentDecision] = None
    if t.is_alive():
        kind, reason = "technical", f"graph exceeded {limit:.0f}s"
    elif "exc" in box:
        kind, reason = "technical", f"graph raised {box['exc']!r}"
    else:
        decision = box.get("decision")
        kind, reason = validate_decision(decision)

    if kind is None and decision is not None:
        return AuthoritativeOutcome("langgraph", decision, decision.engine_result, None, None,
                                    round((time.time() - started) * 1000, 2))

    # ── fallback: the current engine answers, and the reason is traced ──
    print(f"[agent] authoritative graph fell back to the engine ({kind}): {reason}")
    _trace_fallback(ctx, kind, reason)
    result = engine_fallback()
    return AuthoritativeOutcome("engine_fallback", decision, result, reason, kind,
                                round((time.time() - started) * 1000, 2))


def _trace_fallback(ctx: Dict[str, Any], kind: Optional[str], reason: Optional[str]) -> None:
    try:
        with lf.trace("shipify.agent.fallback",
                      session_id=ctx.get("session_id") or ctx.get("external_user_id"),
                      metadata={"sample_source": ctx.get("sample_source"),
                                "langgraph_mode": getattr(config, "LANGGRAPH_MODE", "shadow"),
                                "graph_version": getattr(config, "LANGGRAPH_VERSION", "1")}) as span:
            span.update(output={"fallback": True, "fallback_kind": kind,
                                "fallback_reason": reason})
    except Exception:                             # pragma: no cover - never fatal
        pass


# ── comparison ───────────────────────────────────────────────────────
def _slot_value(v: Any) -> Any:
    if isinstance(v, dict):
        return (v.get("value"), v.get("unit"))
    return (v, None)


def _current_view(engine_result: Dict[str, Any]) -> Dict[str, Any]:
    """The current engine's decision, in the same shape as the graph's."""
    dev = (engine_result or {}).get("developer") or {}
    sem = dev.get("semantic") or {}
    reply = ((engine_result or {}).get("reply") or {}).get("text") or ""
    return {
        "primary_intent": sem.get("intent_family") or dev.get("semantic_family") or "UNKNOWN",
        "routing_type": ((engine_result or {}).get("routing") or {}).get("type"),
        "selection_source": dev.get("selection_source"),
        "entities": sem.get("entities") or {},
        "final_response": reply,
        "handoff": ((engine_result or {}).get("routing") or {}).get("type") == "HUMAN_HANDOFF",
    }


def compare(engine_result: Dict[str, Any], decision: Optional[AgentDecision]) -> Dict[str, Any]:
    """Classify the graph's decision against the current engine's.

    The classification is deliberately conservative: a difference counts
    as LANGGRAPH_IMPROVEMENT only when the graph demonstrably PRESERVED a
    fact the current turn lost, and as LANGGRAPH_WRONG whenever the graph
    is less safe OR its own safety gate flagged the turn. Anything else
    that merely differs is AMBIGUOUS, not credit.
    """
    if decision is None:
        return {"class": None, "why": "no shadow decision"}
    cur = _current_view(engine_result)
    reasons = []

    # ── safety first ──
    if decision.errors:
        safety_flagged = [e for e in decision.errors
                          if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
        if safety_flagged:
            return {"class": "LANGGRAPH_WRONG", "dimension": "auth"
                    if any(f.startswith("AUTH") for f in safety_flagged) else "action_truth",
                    "why": "; ".join(safety_flagged)}
    # the graph must never be LESS strict about identity than the engine.
    engine_asked_identity = "รหัสลูกค้า" in (cur.get("final_response") or "")
    if engine_asked_identity and not decision.auth_required:
        return {"class": "LANGGRAPH_WRONG", "dimension": "private_access",
                "why": "engine required identity, graph did not"}

    same_route = decision.routing_type == cur.get("routing_type")
    same_intent = decision.primary_intent == cur.get("primary_intent")

    # ── fact preservation ──
    g_slots = {k: _slot_value(v) for k, v in (decision.known_slots or {}).items() if v}
    c_slots = {k: _slot_value(v) for k, v in (cur.get("entities") or {}).items() if v}
    kept = sorted(set(g_slots) - set(c_slots))
    lost = sorted(set(c_slots) - set(g_slots))

    if same_route and same_intent and not kept and not lost:
        return {"class": "MATCH", "why": "same intent, route and facts"}
    if kept and not lost:
        reasons.append(f"graph preserved {kept} the current turn did not expose")
        return {"class": "LANGGRAPH_IMPROVEMENT", "why": "; ".join(reasons)}
    if lost and not kept:
        return {"class": "CURRENT_CORRECT",
                "why": f"graph lost {lost} the current turn had"}
    if same_route and not kept and not lost:
        return {"class": "MATCH", "why": "same route and facts, different family label"}
    return {"class": "AMBIGUOUS",
            "why": f"route {cur.get('routing_type')} vs {decision.routing_type}; "
                   f"intent {cur.get('primary_intent')} vs {decision.primary_intent}; "
                   f"kept={kept} lost={lost}"}
