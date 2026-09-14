# -*- coding: utf-8 -*-
"""The bounded LangGraph (task §5).

    START
      -> normalize_input
      -> resolve_current_turn        UNDERSTAND (once)
      -> merge_conversation_state    REMEMBER
      -> resolve_precedence
      -> resolve_auth_requirement
      -> plan_next_action            PLAN
      -> select_tool                 CHOOSE TOOL   (conditional)
      -> execute_tool                EXECUTE SAFELY
      -> validate_tool_result
      -> ground_response
      -> plan_response               ANSWER
      -> safety_check
      -> persist_state
    END

Bounded on purpose: no cycles, no agent-chooses-its-own-loop, no tool the
graph may invent. The conditional edge after `select_tool` is the only
branch, and every branch converges on the same validated execution path.
A turn therefore always terminates, and always in the same number of
steps — which is what makes the shadow comparison meaningful.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, START, END

from services.agent.state import AgentState
from services.agent.nodes import (
    semantics, context as ctx_nodes, planner, tools, grounding, response,
    safety, persistence,
)

GRAPH_NODES = (
    "normalize_input", "resolve_current_turn", "merge_conversation_state",
    "resolve_precedence", "resolve_auth_requirement", "plan_next_action",
    "select_tool", "execute_tool", "validate_tool_result", "ground_response",
    "plan_response", "safety_check", "persist_state",
)


def _route_after_select(state: Dict[str, Any]) -> str:
    """The ONE conditional edge.

    A turn that already knows it cannot execute anything — a clarification,
    or a graph-level error — skips execution and goes straight to shaping a
    response. Everything else executes through the validated engine.
    """
    if state.get("errors") and not state.get("selected_tool"):
        return "ground_response"
    return "execute_tool"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("normalize_input", semantics.normalize_input)
    g.add_node("resolve_current_turn", semantics.resolve_current_turn)
    g.add_node("merge_conversation_state", ctx_nodes.merge_conversation_state)
    g.add_node("resolve_precedence", ctx_nodes.resolve_precedence)
    g.add_node("resolve_auth_requirement", planner.resolve_auth_requirement)
    g.add_node("plan_next_action", planner.plan_next_action)
    g.add_node("select_tool", tools.select_tool)
    g.add_node("execute_tool", tools.execute_tool)
    g.add_node("validate_tool_result", tools.validate_tool_result)
    g.add_node("ground_response", grounding.ground_response)
    g.add_node("plan_response", response.plan_response)
    g.add_node("safety_check", safety.safety_check)
    g.add_node("persist_state", persistence.persist_state)

    g.add_edge(START, "normalize_input")
    g.add_edge("normalize_input", "resolve_current_turn")
    g.add_edge("resolve_current_turn", "merge_conversation_state")
    g.add_edge("merge_conversation_state", "resolve_precedence")
    g.add_edge("resolve_precedence", "resolve_auth_requirement")
    g.add_edge("resolve_auth_requirement", "plan_next_action")
    g.add_edge("plan_next_action", "select_tool")
    g.add_conditional_edges("select_tool", _route_after_select,
                            {"execute_tool": "execute_tool",
                             "ground_response": "ground_response"})
    g.add_edge("execute_tool", "validate_tool_result")
    g.add_edge("validate_tool_result", "ground_response")
    g.add_edge("ground_response", "plan_response")
    g.add_edge("plan_response", "safety_check")
    g.add_edge("safety_check", "persist_state")
    g.add_edge("persist_state", END)
    return g.compile()


_lock = threading.Lock()
_compiled = None


def get_graph():
    """One compiled graph per process. Compilation is pure and cheap, but
    doing it per turn would show up as latency on the live path."""
    global _compiled
    if _compiled is None:
        with _lock:
            if _compiled is None:
                _compiled = build_graph()
    return _compiled


def reset_graph_for_tests() -> None:
    global _compiled
    with _lock:
        _compiled = None
