# -*- coding: utf-8 -*-
"""PLAN — authority requirement, then the single next action.

The planner owns the question "WHAT SHOULD HAPPEN NEXT?" (task §13). The
response layer never decides for itself which slot is missing; it renders
the decision made here. That is what makes
ACKNOWLEDGED_SLOT_CANNOT_BE_REASKED structurally true rather than a thing
each reply template has to remember.
"""
from __future__ import annotations

from typing import Any, Dict

from services.agent.adapters import existing_engine as engine
from services.agent.state import effective_history, effective_message

_ASK_FOR_SLOT = {
    "product": "ASK_PRODUCT",
    "quantity": "ASK_QUANTITY",
    "shipping_method": "ASK_SHIPPING_METHOD",
    "weight": "ASK_WEIGHT",
}


def resolve_auth_requirement(state: Dict[str, Any]) -> Dict[str, Any]:
    """Authority stays DETERMINISTIC and stays where it already lives.

    The graph asks services/decision_engine.py for its verdict and
    records it; it never forms its own opinion about whether a turn is
    the customer's own record. An LLM anywhere in this graph is evidence,
    never authorization (task §10).
    """
    node_path = list(state.get("node_path") or []) + ["resolve_auth_requirement"]
    msg = effective_message(state)
    det, evidence = engine.private_authority(msg, effective_history(state),
                                             state.get("customer_context") or {})
    family = state.get("primary_intent") or "UNKNOWN"
    is_private = bool(det) or bool(evidence)
    # a family that is public by construction can never be pulled into an
    # identity-gated lookup, whatever else this turn contains.
    if engine.is_public_family(family):
        is_private = False

    ctx = state.get("customer_context") or {}
    verified = bool(ctx.get("cust_code"))
    auth_state = "NOT_REQUIRED"
    if is_private:
        auth_state = "REQUIRED_SATISFIED" if verified else "REQUIRED_MISSING"
    return {"private_ownership_evidence": evidence,
            "auth_required": is_private, "auth_state": auth_state,
            "node_path": node_path}


def plan_next_action(state: Dict[str, Any]) -> Dict[str, Any]:
    """Choose exactly ONE next action from the closed vocabulary."""
    node_path = list(state.get("node_path") or []) + ["plan_next_action"]
    intent = state.get("primary_intent") or "UNKNOWN"
    known = state.get("known_slots") or {}
    missing = list(state.get("missing_slots") or [])
    notes = list(state.get("notes") or [])

    # 1. authority first — a private request with no identity is an ASK,
    #    never a lookup and never a guess.
    if state.get("auth_required") and state.get("auth_state") == "REQUIRED_MISSING":
        action = "ASK_IDENTIFIER"
    elif state.get("auth_required"):
        action = "CHECK_PRIVATE_STATUS"
    # 2. an operational request is prepared for staff, never executed here.
    elif intent in ("CANCELLATION_OPERATION", "ADDRESS_CHANGE"):
        action = "PREPARE_WORKFLOW"
    elif intent == "CANCELLATION_POLICY":
        action = "ANSWER_POLICY"
    elif intent == "LINK_CONVERSION":
        action = "CONVERT_LINK"
    elif intent == "SHIPPING_ESTIMATE":
        action = "CALCULATE"
    # 3. an open journey with a genuinely missing slot -> ask for the
    #    FIRST one still missing. A slot already known is never asked
    #    for, which is the invariant the whole planner exists to hold.
    elif state.get("active_journey") and missing:
        action = _ASK_FOR_SLOT.get(missing[0], "CLARIFY")
        notes.append(f"known slots: {sorted(k for k, v in known.items() if v)}")
    elif intent in ("UNKNOWN",) and not known:
        action = "CLARIFY"
    else:
        action = "ANSWER_PUBLIC_INFO"

    handoff = state.get("handoff_required") or False
    tool = engine.tool_for_family(intent, is_private=bool(state.get("auth_required")),
                                  handoff=bool(handoff))
    contract = engine.TOOL_CONTRACTS.get(tool, {})
    return {"planned_action": action, "selected_tool": tool,
            "tool_class": contract.get("tool_class"),
            "tool_input": {"message": state.get("normalized_message"),
                           "known_slots": known, "missing_slots": missing,
                           "planned_action": action},
            "notes": notes, "node_path": node_path}
