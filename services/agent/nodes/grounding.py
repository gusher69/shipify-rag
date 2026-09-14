# -*- coding: utf-8 -*-
"""GROUND — record what this answer is allowed to rest on.

This node does not re-verify business facts; the executing services
already carry their own grounding rules and the live-tier evaluator
already classifies every factual claim. What it does is make the
grounding SOURCE explicit in the state, so a trace can answer
"ทำไมข้อความนี้ถึงตอบแบบนี้?" without reading source code.
"""
from __future__ import annotations

from typing import Any, Dict

from services.agent.adapters import existing_engine as engine


def ground_response(state: Dict[str, Any]) -> Dict[str, Any]:
    node_path = list(state.get("node_path") or []) + ["ground_response"]
    tool = state.get("selected_tool") or ""
    contract = engine.TOOL_CONTRACTS.get(tool) or {}
    res = state.get("tool_result") or {}

    source = contract.get("grounding_source") or "unknown"
    # what the engine ACTUALLY used this turn is more informative than
    # what the tool contract promises, when the two differ.
    actual = res.get("selection_source")
    status = "GROUNDED"
    if res.get("error"):
        status = "DEGRADED"
    elif not (state.get("final_response") or "").strip():
        status = "EMPTY"
    elif state.get("auth_state") == "REQUIRED_MISSING":
        # an identifier request states no business fact at all, so there
        # is nothing to ground — and nothing that could be invented.
        status = "NO_FACT_STATED"
    return {"node_path": node_path,
            "response_plan": {**(state.get("response_plan") or {}),
                              "grounding_source": source,
                              "grounding_actual": actual,
                              "grounding_status": status}}
