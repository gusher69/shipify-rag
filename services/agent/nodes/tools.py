# -*- coding: utf-8 -*-
"""CHOOSE TOOL -> EXECUTE SAFELY -> VALIDATE.

The graph declares WHICH capability this turn needs and under what
contract; the existing Decision Engine still performs the execution —
Business Action selection, ERP call, RAG retrieval, calculator, link
conversion, Human CS escalation. Keeping execution there is what stops
this package from becoming a second decision engine (task §2/§11).
"""
from __future__ import annotations

import time
from typing import Any, Dict

from services.agent.adapters import existing_engine as engine

# actions that must never reach a side-effecting or identity-gated tool.
_PUBLIC_ONLY_ACTIONS = frozenset({"ANSWER_PUBLIC_INFO", "ANSWER_POLICY",
                                  "ASK_PRODUCT", "ASK_QUANTITY",
                                  "ASK_SHIPPING_METHOD", "ASK_WEIGHT",
                                  "CLARIFY", "CALCULATE", "CONVERT_LINK"})


def select_tool(state: Dict[str, Any]) -> Dict[str, Any]:
    """Confirm the planned tool against its declared contract.

    A mismatch is not silently corrected: the tool is downgraded to the
    safest capability that satisfies the plan, and the downgrade is
    recorded. An unauthorised private read can therefore never be reached
    by a planning mistake — only by a deterministic auth verdict.
    """
    node_path = list(state.get("node_path") or []) + ["select_tool"]
    tool = state.get("selected_tool") or "PUBLIC_RAG"
    contract = dict(engine.TOOL_CONTRACTS.get(tool) or {})
    action = state.get("planned_action")
    notes = list(state.get("notes") or [])

    if contract.get("requires_auth") and action in _PUBLIC_ONLY_ACTIONS:
        notes.append(f"tool {tool} requires auth but the planned action "
                     f"{action} is public — downgraded to PUBLIC_RAG")
        tool = "PUBLIC_RAG"
        contract = dict(engine.TOOL_CONTRACTS["PUBLIC_RAG"])
    if contract.get("requires_auth") and state.get("auth_state") == "REQUIRED_MISSING" \
            and action != "ASK_IDENTIFIER":
        notes.append(f"tool {tool} requires an identity that is missing — "
                     "the turn may only ask for the identifier")
        contract = dict(engine.TOOL_CONTRACTS["WORKFLOW"])
    return {"selected_tool": tool, "tool_class": contract.get("tool_class"),
            "grounding_requirement": contract.get("grounding_source")
            or state.get("grounding_requirement") or "GENERAL",
            "notes": notes, "node_path": node_path}


def execute_tool(state: Dict[str, Any]) -> Dict[str, Any]:
    """Run the turn through the existing validated orchestrator."""
    node_path = list(state.get("node_path") or []) + ["execute_tool"]
    started = time.time()
    # SHADOW SAFETY — when the caller has ALREADY executed this turn
    # through the engine (which is exactly what shadow mode does: the
    # customer's real answer is produced first, then the graph runs
    # beside it), reuse that result instead of executing a second time.
    # Re-executing would double every ERP call, every retrieval and every
    # Human-CS notification for an observation-only run.
    precomputed = (state.get("_decide_context") or {}).get("_precomputed_engine_result")
    if precomputed is not None:
        result = precomputed
        node_path = node_path[:-1] + ["execute_tool(reused)"]
    else:
        try:
            result = engine.execute(state.get("normalized_message")
                                    or state.get("raw_message") or "",
                                    state.get("history") or [],
                                    {k: v for k, v in (state.get("_decide_context") or {}).items()
                                     if k != "_precomputed_engine_result"})
        except Exception as exc:
            return {"node_path": node_path,
                    "tool_result": {"error": repr(exc)},
                    "handoff_required": True,
                    "errors": list(state.get("errors") or []) + [f"execute_tool: {exc!r}"]}

    routing = (result.get("routing") or {}).get("type")
    reply = (result.get("reply") or {}).get("text") or ""
    dev = result.get("developer") or {}
    return {
        "node_path": node_path,
        "routing_type": routing,
        "tool_result": {
            "routing_type": routing,
            "selection_source": dev.get("selection_source"),
            "workflow": result.get("workflow"),
            "erp_called": bool(dev.get("erp_http_status")),
            "latency_ms": round((time.time() - started) * 1000, 2),
            "error": result.get("error"),
        },
        "rag_context": dev.get("selection_source"),
        "final_response": reply,
        "handoff_required": routing == "HUMAN_HANDOFF",
        "_engine_result": result,
    }


def validate_tool_result(state: Dict[str, Any]) -> Dict[str, Any]:
    """Did execution do what the plan asked, and is the result usable?

    Divergence between the planned capability and the route the engine
    actually took is RECORDED, never hidden — it is exactly the signal
    the shadow comparison needs in order to show where the graph's
    understanding and the current engine's differ.
    """
    node_path = list(state.get("node_path") or []) + ["validate_tool_result"]
    res = state.get("tool_result") or {}
    notes = list(state.get("notes") or [])
    errors = list(state.get("errors") or [])
    routing = res.get("routing_type")
    tool = state.get("selected_tool")

    expected = {
        "PUBLIC_RAG": {"RAG", "GENERAL", "BOTH", "HYBRID"},
        "PRODUCT_POLICY": {"RAG", "GENERAL", "BOTH", "HYBRID"},
        "CONTACT_INFO": {"RAG", "GENERAL"},
        "SHIPPING_CALCULATOR": {"GENERAL", "WORKFLOW", "RAG"},
        "CBM_CALCULATOR": {"GENERAL", "WORKFLOW", "RAG"},
        "LINK_CONVERSION": {"WORKFLOW", "GENERAL", "RAG"},
        "PRIVATE_ERP": {"WORKFLOW", "HUMAN_HANDOFF", "GENERAL"},
        "WORKFLOW": {"WORKFLOW", "HUMAN_HANDOFF", "GENERAL"},
        "HUMAN_CS": {"HUMAN_HANDOFF", "WORKFLOW"},
    }.get(tool or "", set())
    if routing and expected and routing not in expected:
        notes.append(f"route divergence: planned {tool}, engine routed {routing}")
    if res.get("error"):
        errors.append(f"tool error: {res['error']}")
    if not (state.get("final_response") or "").strip():
        errors.append("empty response from tool execution")
    return {"notes": notes, "errors": errors, "node_path": node_path}
